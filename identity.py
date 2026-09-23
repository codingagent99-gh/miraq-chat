"""
identity.py — who is making this request, verified.

The widget used to send customer_id / role / email in the request body, taken
from the page's JavaScript config, and the backend trusted them. Anyone could
edit that config (or call the API directly with the store's public licence id)
and read another customer's orders and addresses — the backend fetches them
with the store's admin keys — or claim an admin role for store-wide reports.
The plugin's own role checks could not help: they check the real WordPress
roles of whichever customer_id the backend passes, so a spoofed id is enough.

Identity now comes only from sources a browser cannot forge:

  WooCommerce  X-MiraQ-Identity header — a token the WordPress plugin signs
               when it renders the page (includes/class-identity.php). Signing
               key: HMAC-SHA256(consumer_secret, "miraq-identity-v1"), i.e.
               derived from the store's Woo consumer secret, which only the
               plugin and this backend hold. No token → guest.

  Shopify      logged_in_customer_id from the App Proxy query, which Shopify
               signs. Also checks the signed `shop` matches this tenant, so a
               signed request from another store running the app cannot be
               replayed against this one. No proxy signature → guest.

  Channels     routes/channel.py sets CHANNEL_ENVIRON_KEY on the in-process
               request it builds. WSGI environ keys without an HTTP_ prefix
               cannot be set by an HTTP client, so this cannot be spoofed.

Token format (WooCommerce): base64url(json payload) + "." + base64url(sig)
  payload: {"v": 1, "lid": license_id, "cid": wp_user_id, "role": str,
            "email": str, "iat": unix, "exp": unix}
  sig:     HMAC-SHA256(signing_key, <payload part as ASCII>)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Optional

from flask import g, request

from chat_logger import get_logger

logger = get_logger("miraq_chat")

IDENTITY_HEADER = "X-MiraQ-Identity"
CHANNEL_ENVIRON_KEY = "miraq.channel_identity"

_KEY_CONTEXT = b"miraq-identity-v1"
_TOKEN_VERSION = 1
# Tolerance for clock differences between the WordPress host and this server.
_CLOCK_SKEW_S = 120


@dataclass(frozen=True)
class Identity:
    customer_id: str = ""      # "" = guest
    role: str = "guest"
    email: str = ""
    source: str = "anonymous"  # wp_token | shopify_proxy | channel | anonymous

    @property
    def is_guest(self) -> bool:
        return not self.customer_id


GUEST = Identity()


class IdentityExpired(Exception):
    """A correctly signed token whose lifetime has passed — the widget should
    fetch a fresh one (GET custom-api/v1/identity) and retry."""


# ── token primitives ─────────────────────────────────────────────────────────

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signing_key(consumer_secret: str) -> bytes:
    # Matches PHP: hash_hmac('sha256', 'miraq-identity-v1', $secret, true)
    return hmac.new(consumer_secret.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()


def sign_woo_identity(payload: dict, consumer_secret: str) -> str:
    """Build a token. The plugin does this in PHP; kept here for tests and so
    the two implementations can be checked against each other."""
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_signing_key(consumer_secret), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_woo_identity(token: str, *, consumer_secret: str, license_id: str,
                        now: Optional[float] = None) -> Identity:
    """Verified identity from a plugin-signed token.

    Returns GUEST for anything that fails verification (logged — a forged or
    foreign token is worth seeing). Raises IdentityExpired only for a token
    that is genuine but past its lifetime, so the widget can refresh it.
    """
    if not token or not consumer_secret:
        return GUEST
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(_signing_key(consumer_secret), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            logger.warning("identity: token signature mismatch — treating as guest")
            return GUEST
        payload = json.loads(_b64url_decode(body))
    except Exception as e:
        logger.warning(f"identity: malformed token ({type(e).__name__}) — treating as guest")
        return GUEST

    if payload.get("v") != _TOKEN_VERSION:
        logger.warning(f"identity: unsupported token version {payload.get('v')!r} — treating as guest")
        return GUEST
    # Bound to one store: a token from store A must not work on store B, even
    # if both happened to share a secret.
    if str(payload.get("lid") or "") != str(license_id or ""):
        logger.warning("identity: token issued for a different store — treating as guest")
        return GUEST

    now = time.time() if now is None else now
    try:
        iat, exp = int(payload.get("iat", 0)), int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return GUEST
    if iat > now + _CLOCK_SKEW_S:
        logger.warning("identity: token issued in the future — treating as guest")
        return GUEST
    if exp + _CLOCK_SKEW_S < now:
        raise IdentityExpired()

    cid = payload.get("cid")
    cid = str(cid) if cid not in (None, "", 0, "0") else ""
    if not cid:
        return GUEST
    return Identity(
        customer_id=cid,
        role=str(payload.get("role") or "customer"),
        email=str(payload.get("email") or ""),
        source="wp_token",
    )


# ── per-request resolution ───────────────────────────────────────────────────

def _woo_consumer_secret(tenant) -> str:
    loader = g.__dict__.get("store_loader")
    secret = getattr(loader, "consumer_secret", "") if loader is not None else ""
    if secret:
        return secret
    from tenant_crypto import decrypt_secret
    try:
        return decrypt_secret(tenant.woo_secret_encrypted or "")
    except Exception:
        logger.error(f"identity: cannot decrypt consumer secret | license_id={tenant.license_id!r}")
        return ""


def _shopify_identity(tenant) -> Identity:
    args = request.args
    if not args.get("signature"):
        return GUEST  # not proxied (e.g. a direct call with the licence header)

    shop = (args.get("shop") or "").strip().lower()
    if shop != (tenant.shopify_domain or "").strip().lower():
        logger.warning(
            f"identity: signed proxy request for shop={shop!r} used against "
            f"tenant {tenant.shopify_domain!r} — treating as guest"
        )
        return GUEST

    from app_config import SHOPIFY_CLIENT_SECRET, SHOPIFY_CUSTOMER_AUTH, SHOPIFY_PROXY_MAX_AGE
    from ecommerce.shopify_proxy import resolve_shopify_customer_id
    claimed = ((request.get_json(silent=True) or {}).get("user_context") or {}).get("customer_id")
    customer_id, error = resolve_shopify_customer_id(
        args,
        mode=SHOPIFY_CUSTOMER_AUTH,
        client_secret=SHOPIFY_CLIENT_SECRET,
        claimed_customer_id=claimed,  # only honoured in the dev-only insecure mode
        max_age_seconds=SHOPIFY_PROXY_MAX_AGE or None,
    )
    if error or not customer_id:
        return GUEST
    return Identity(customer_id=str(customer_id), role="customer", source="shopify_proxy")


def resolve_identity(tenant) -> Identity:
    """Verified identity for this request. Raises IdentityExpired (WooCommerce only)."""
    channel = request.environ.get(CHANNEL_ENVIRON_KEY)
    if isinstance(channel, dict):
        cid = str(channel.get("customer_id") or "")
        return Identity(customer_id=cid, role="customer" if cid else "guest", source="channel")

    if (tenant.ecommerce_backend or "woocommerce") == "shopify":
        return _shopify_identity(tenant)

    token = request.headers.get(IDENTITY_HEADER, "").strip()
    if not token:
        return GUEST
    return verify_woo_identity(
        token, consumer_secret=_woo_consumer_secret(tenant), license_id=tenant.license_id,
    )


def current_identity() -> Identity:
    """The identity bound by store_registry for this request; GUEST if none."""
    try:
        return g.__dict__.get("identity") or GUEST
    except RuntimeError:
        return GUEST
