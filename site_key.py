"""
site_key.py — the store's own Ed25519 key, and how the backend checks that a
request or a shopper's identity token really comes from that store's
WordPress plugin.

WHY
  The plugin used to create a WooCommerce API key and send it here, and the
  backend used it as a shared password: to verify identity tokens, branding
  pushes and uninstall calls. Now the plugin generates its own keypair
  (includes/class-site-key.php) and keeps the private half; the backend only
  ever holds the PUBLIC key, in tenants.features["site_key"]. Nothing stored
  here can sign anything, so the backend's database cannot be used to
  impersonate a store.

SIGNED REQUESTS (plugin → backend)
  X-MiraQ-Timestamp: unix seconds
  X-MiraQ-Signature: base64url(Ed25519 signature of the message below)
      message = "miraq-request-v1\n<purpose>\n<timestamp>\n<sha256 hex of the raw body>"
  The purpose is fixed per endpoint ("deactivate-tenant", "branding-push",
  "register-site-key"), so a signature for one action is useless for another.
  Must match MiraQ_Site_Key::signed_headers() in the plugin exactly.

SITE KEY CHECK AT ACTIVATION
  check_site_key_matches() asks the site itself for its public key
  (GET <wp_base_url>/wp-json/miraq/v1/site-key) and refuses an activation
  whose key differs from the one the site serves. An unreachable site is
  NOT refused (some hosts block inbound calls) — that is logged instead.

LEGACY BRIDGE
  Stores set up before plugin 1.0.8 have a WooCommerce key/secret on record
  and no site key. verify_legacy_credentials() lets them authenticate with it
  until they register their site key (/register-site-key), which wipes the
  stored key. Remove the bridge, and the woo_key / woo_secret_encrypted
  columns, once every store has registered.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from chat_logger import get_logger

logger = get_logger("miraq_chat")

TIMESTAMP_HEADER = "X-MiraQ-Timestamp"
SIGNATURE_HEADER = "X-MiraQ-Signature"
REQUEST_CONTEXT = "miraq-request-v1"
# How old (or how far in the future) a signed request may be.
REQUEST_SKEW_S = 300
SITE_KEY_ROUTE = "/wp-json/miraq/v1/site-key"


# ── encoding ─────────────────────────────────────────────────────────────────

def _b64decode_any(text: str) -> Optional[bytes]:
    """Standard or URL-safe base64, padded or not. None if it isn't base64."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip().replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def parse_public_key(b64: str) -> Optional[Ed25519PublicKey]:
    raw = _b64decode_any(b64)
    if raw is None or len(raw) != 32:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        return None


def normalize_public_key(b64: str) -> str:
    """The key as standard base64, or "" if it is not a valid Ed25519 key."""
    raw = _b64decode_any(b64)
    if raw is None or len(raw) != 32 or parse_public_key(b64) is None:
        return ""
    return base64.b64encode(raw).decode("ascii")


def fingerprint(b64: str) -> str:
    """Short fingerprint of a public key, the same one the plugin's log prints
    (MiraQ_Log::fp), so plugin and backend log lines can be matched."""
    return hashlib.sha256((b64 or "").encode("ascii")).hexdigest()[:12] if b64 else "-"


# ── stored key ───────────────────────────────────────────────────────────────

def public_key_b64(tenant) -> str:
    entry = (getattr(tenant, "features", None) or {}).get("site_key") or {}
    return str(entry.get("public_key") or "") if isinstance(entry, dict) else ""


def public_key_for(tenant) -> Optional[Ed25519PublicKey]:
    b64 = public_key_b64(tenant)
    return parse_public_key(b64) if b64 else None


def store_public_key(tenant, b64: str) -> None:
    """Record the store's public key and drop its old WooCommerce key.
    The caller commits."""
    features = dict(getattr(tenant, "features", None) or {})
    features["site_key"] = {
        "public_key": normalize_public_key(b64),
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    tenant.features = features  # reassign: plain JSONB has no in-place change tracking
    had_old_key = bool(getattr(tenant, "woo_key", None) or getattr(tenant, "woo_secret_encrypted", None))
    tenant.woo_key = None
    tenant.woo_secret_encrypted = None
    logger.info(
        f"site_key: stored | tenant_id={getattr(tenant, 'tenant_id', '')} | "
        f"site_key={fingerprint(features['site_key']['public_key'])} | "
        f"old_woo_key={'wiped' if had_old_key else 'none'}"
    )


def clear_public_key(tenant) -> None:
    features = dict(getattr(tenant, "features", None) or {})
    entry = features.pop("site_key", None)
    if entry is not None:
        tenant.features = features
        logger.info(
            f"site_key: cleared | tenant_id={getattr(tenant, 'tenant_id', '')} | "
            f"site_key={fingerprint((entry or {}).get('public_key', '') if isinstance(entry, dict) else '')}"
        )


# ── signatures ───────────────────────────────────────────────────────────────

def verify(public_key: Optional[Ed25519PublicKey], message: bytes, signature_b64: str) -> bool:
    if public_key is None:
        return False
    sig = _b64decode_any(signature_b64)
    if sig is None or len(sig) != 64:
        return False
    try:
        public_key.verify(sig, message)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def request_message(purpose: str, timestamp: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{REQUEST_CONTEXT}\n{purpose}\n{timestamp}\n{digest}".encode("utf-8")


def sign_request(private_key, purpose: str, body: bytes, timestamp: Optional[int] = None) -> dict:
    """Headers for a signed request. The plugin does this in PHP; kept here
    for tests and dev tools."""
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    sig = private_key.sign(request_message(purpose, ts, body))
    return {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii"),
    }


def verify_signed_request(tenant, purpose: str, *, public_key_b64_override: str = "",
                          now: Optional[float] = None) -> bool:
    """True when the current Flask request is signed by this tenant's site
    key (or by public_key_b64_override, for the register call) for `purpose`."""
    from flask import request

    ts = request.headers.get(TIMESTAMP_HEADER, "").strip()
    sig = request.headers.get(SIGNATURE_HEADER, "").strip()
    if not ts or not sig:
        return False
    try:
        ts_i = int(ts)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - ts_i) > REQUEST_SKEW_S:
        logger.warning(f"site_key: signed request outside the time window | purpose={purpose}")
        return False

    key = parse_public_key(public_key_b64_override) if public_key_b64_override else public_key_for(tenant)
    ok = verify(key, request_message(purpose, ts, request.get_data(cache=True)), sig)
    if not ok and key is not None:
        logger.warning(f"site_key: bad request signature | purpose={purpose}")
    return ok


# ── the site's own copy of the key ───────────────────────────────────────────

def fetch_site_public_key(wp_base_url: str, *, timeout: int = 10, session=None) -> Tuple[str, str]:
    """Ask the site which public key it serves.

    Returns (outcome, key): outcome is "ok" (key is the site's key),
    "missing" (the site answered without one), or "unreachable".
    """
    import requests
    from http_profiles import HttpProfileState, send

    base = (wp_base_url or "").rstrip("/")
    if not base:
        return "unreachable", ""
    try:
        resp = send(session or requests.Session(), "GET", f"{base}{SITE_KEY_ROUTE}",
                    state=HttpProfileState(), timeout=timeout)
    except Exception as e:
        logger.warning(f"site_key: site key fetch failed | base={base} | {type(e).__name__}: {e}")
        return "unreachable", ""
    if resp.status_code != 200:
        logger.warning(f"site_key: site key fetch returned HTTP {resp.status_code} | base={base}")
        return ("missing" if resp.status_code == 404 else "unreachable"), ""
    try:
        key = normalize_public_key(str((resp.json() or {}).get("public_key") or ""))
    except Exception:
        key = ""
    return ("ok", key) if key else ("missing", "")


def check_site_key_matches(wp_base_url: str, submitted_b64: str, *, label: str = "",
                           session=None) -> Tuple[bool, str]:
    """(allowed, reason). Refuses only when the site serves a DIFFERENT key:
    an unreachable site is let through and logged (see the module docstring)."""
    submitted = normalize_public_key(submitted_b64)
    outcome, served = fetch_site_public_key(wp_base_url, session=session)
    if outcome == "ok":
        if hmac.compare_digest(served, submitted):
            logger.info(f"site_key: activation key matches the site's key | {label} | site_key={fingerprint(submitted)}")
            return True, "verified"
        logger.warning(
            f"site_key: activation key does not match the site's key | {label} | base={wp_base_url} | "
            f"submitted={fingerprint(submitted)} served={fingerprint(served)}"
        )
        return False, "mismatch"
    logger.warning(
        f"site_key: could not confirm the site key with the site ({outcome}) — "
        f"accepting | {label} | base={wp_base_url}"
    )
    return True, outcome


# ── legacy bridge ────────────────────────────────────────────────────────────

def legacy_consumer_secret(tenant) -> str:
    """The pre-1.0.8 WooCommerce secret, while the store still has one."""
    if not getattr(tenant, "woo_secret_encrypted", None):
        return ""
    from tenant_crypto import decrypt_secret
    try:
        return decrypt_secret(tenant.woo_secret_encrypted or "")
    except Exception as e:
        logger.error(f"site_key: cannot decrypt legacy secret | tenant_id={getattr(tenant, 'tenant_id', '')} | {e}")
        return ""


def verify_legacy_credentials(tenant) -> bool:
    """Timing-safe X-Consumer-Key / X-Consumer-Secret check against the
    tenant's stored pre-1.0.8 WooCommerce key. False once it is wiped."""
    from flask import request

    key = request.headers.get("X-Consumer-Key", "")
    secret = request.headers.get("X-Consumer-Secret", "")
    if not key or not secret:
        return False
    expected_key = getattr(tenant, "woo_key", None) or ""
    expected_secret = legacy_consumer_secret(tenant)
    if not expected_key or not expected_secret:
        return False
    # Both comparisons always run, so timing cannot reveal which half matched.
    key_ok = hmac.compare_digest(key, expected_key)
    secret_ok = hmac.compare_digest(secret, expected_secret)
    return key_ok and secret_ok
