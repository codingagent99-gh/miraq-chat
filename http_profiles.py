"""
http_profiles.py — Per-tenant outbound HTTP header profiles.

Why this exists
  Store hosts run different WAFs, and they disagree about which request
  headers look legitimate:
    - cPanel / Imunify-style ModSecurity (silfradigital.com) returns 406 for a
      bare "Mozilla/5.0" User-Agent and accepts the full browser set.
    - WordPress.com Atomic has preferred the minimal set.
  No single hardcoded set works everywhere. There used to be three copies
  (app_config.BROWSER_HEADERS, store_loader/config.BROWSER_HEADERS and
  woo_client._BASE_HEADERS), and the catalog fetcher and woo_client silently
  used different ones — which is how one host accepted all-attributes and
  rejected checkout-fields in the same build.

  Every outbound call to a tenant's store now takes its headers from that
  tenant's HttpProfileState:

    1. Chosen at provisioning by probe_profiles() and stored in
       tenants.features["http_profile"].
    2. Adapted at runtime by send(): a response that is recognisably a WAF
       block (not a WordPress error) is retried with the remaining profiles;
       the first one that gets through is adopted and persisted.
    3. Overridable per tenant, by editing tenants.features directly:
         http_profile_pinned = true  → lock the profile; no probing, no switching
         http_extra_headers  = {...} → add/override individual headers

  The table itself is not code-only:
    MIRAQ_HTTP_PROFILES_FILE   JSON file {"name": {"Header": "value"}} — adds
                               profiles or overrides built-in ones
    MIRAQ_HTTP_PROFILE_ORDER   comma-separated names; first one is the default
                               for new tenants, the rest is the fallback order
    MIRAQ_IDENTIFIED_USER_AGENT  User-Agent for the "identified" profile

Headers are never set on a requests.Session: sessions are shared (WooClient
has one for every tenant), and session-level headers are merged into every
request, so one tenant's profile would leak into another's.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPBasicAuth

from chat_logger import get_logger

logger = get_logger("miraq_chat")


# ══════════════════════════════════════════════════════════════════════
# Profile table
# ══════════════════════════════════════════════════════════════════════

_BUILTIN_PROFILES: Dict[str, Dict[str, str]] = {
    # Full desktop-browser set — what the catalog fetcher has always sent.
    # Passes cPanel/Imunify ModSecurity (verified with curl on silfradigital.com).
    "browser": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    },
    # What woo_client used to send. Blocked (406) by cPanel ModSecurity.
    "minimal": {
        "User-Agent": "Mozilla/5.0",
        "Accept":     "application/json",
    },
    # Honest identification. The one to ask a host to whitelist when neither
    # of the above gets through.
    "identified": {
        "User-Agent":      os.getenv("MIRAQ_IDENTIFIED_USER_AGENT", "MiraQ-Backend/1.0"),
        "Accept":          "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    },
}
_DEFAULT_ORDER = ("browser", "minimal", "identified")


def _valid_headers(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _load_profiles():
    profiles = {name: dict(h) for name, h in _BUILTIN_PROFILES.items()}

    path = os.getenv("MIRAQ_HTTP_PROFILES_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                extra = json.load(f)
            for name, headers in (extra or {}).items():
                if _valid_headers(headers):
                    profiles[str(name)] = dict(headers)
                else:
                    logger.warning(f"http_profiles: ignoring invalid profile {name!r} in {path}")
            logger.info(f"http_profiles: loaded {path} | profiles={sorted(profiles)}")
        except Exception as e:
            logger.error(f"http_profiles: could not read MIRAQ_HTTP_PROFILES_FILE={path!r} | {e}")

    wanted = [n.strip() for n in os.getenv("MIRAQ_HTTP_PROFILE_ORDER", "").split(",") if n.strip()]
    unknown = [n for n in wanted if n not in profiles]
    if unknown:
        logger.warning(f"http_profiles: MIRAQ_HTTP_PROFILE_ORDER names unknown profiles {unknown}")
    order = [n for n in wanted if n in profiles] or [n for n in _DEFAULT_ORDER if n in profiles]
    order += [n for n in profiles if n not in order]
    return profiles, order


PROFILES, PROFILE_ORDER = _load_profiles()
DEFAULT_PROFILE = PROFILE_ORDER[0]


# ══════════════════════════════════════════════════════════════════════
# WAF-block detection
# ══════════════════════════════════════════════════════════════════════

_WAF_STATUSES = frozenset({403, 406, 415, 501})
_WAF_MARKERS = (
    "mod_security", "modsecurity", "not acceptable", "imunify",
    "bot-protection", "bot protection", "cloudflare", "attention required",
    "sucuri", "wordfence", "access denied", "request rejected", "has been blocked",
)


def is_waf_block(resp) -> bool:
    """True only when the response came from a firewall, not from WordPress.

    WordPress REST errors are always JSON, so a JSON body is never treated as
    a block — a real 403 (bad credentials, missing capability) still reaches
    the caller unchanged and never triggers a profile switch.
    """
    if resp is None or resp.status_code not in _WAF_STATUSES:
        return False
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "json" in ctype:
        return False
    try:
        body = (resp.text or "")[:4000].lower()
    except Exception:
        body = ""
    if resp.status_code == 406:
        # WordPress does not emit a non-JSON 406 under /wp-json.
        return True
    return any(marker in body for marker in _WAF_MARKERS)


def describe_block(resp) -> str:
    if resp is None:
        return "no response"
    try:
        body = (resp.text or "").lower()
    except Exception:
        body = ""
    source = next((m for m in _WAF_MARKERS if m in body), "unknown firewall")
    return f"status={resp.status_code} source={source!r}"


def _short(url: str) -> str:
    try:
        parts = urlsplit(url)
        return f"{parts.netloc}{parts.path}"
    except Exception:
        return url


def _clean_headers(value) -> Dict[str, str]:
    if not value:
        return {}
    if not _valid_headers(value):
        logger.warning("http_profiles: ignoring http_extra_headers — must be {str: str}")
        return {}
    return dict(value)


# ══════════════════════════════════════════════════════════════════════
# Per-tenant state
# ══════════════════════════════════════════════════════════════════════

class HttpProfileState:
    """Which header profile one tenant uses. Thread-safe; lives on the StoreLoader."""

    # After every profile was blocked, stop cycling for this long — otherwise
    # each call to a hard-blocked store costs len(PROFILES) requests.
    _ALL_BLOCKED_COOLDOWN_S = 300
    # A host that blocks different profiles on different endpoints would make
    # the profile flip back and forth; switch in memory every time, but write
    # to the DB at most this often.
    _PERSIST_MIN_INTERVAL_S = 600

    def __init__(self, *, tenant_id="", license_id="", profile: Optional[str] = None,
                 pinned: bool = False, extra_headers: Optional[dict] = None, app=None):
        self.tenant_id = str(tenant_id or "")
        self.license_id = license_id or ""
        self._app = app
        self._lock = threading.Lock()
        self._all_blocked_until = 0.0
        self._last_persist: Optional[float] = None  # None = never (monotonic() can be < interval after boot)
        self.pinned = bool(pinned)
        self.extra_headers = _clean_headers(extra_headers)
        if profile and profile not in PROFILES:
            logger.warning(
                f"http_profiles: stored profile {profile!r} is not defined — using "
                f"{DEFAULT_PROFILE!r} | tenant={self.tenant_id}"
            )
            profile = None
        self._name = profile or DEFAULT_PROFILE

    @classmethod
    def from_features(cls, features: Optional[dict], *, tenant_id, license_id="", app=None):
        f = features or {}
        return cls(
            tenant_id=tenant_id,
            license_id=license_id,
            profile=f.get("http_profile") or None,
            pinned=bool(f.get("http_profile_pinned")),
            extra_headers=f.get("http_extra_headers"),
            app=app,
        )

    @classmethod
    def from_tenant(cls, tenant, app=None):
        return cls.from_features(
            dict(tenant.features or {}),
            tenant_id=tenant.tenant_id,
            license_id=tenant.license_id,
            app=app,
        )

    @property
    def name(self) -> str:
        with self._lock:
            return self._name

    def headers(self, extra: Optional[dict] = None, *, profile: Optional[str] = None) -> Dict[str, str]:
        """Profile headers, then the tenant's extra_headers, then the call's own
        (auth headers such as X-Consumer-Key) — later wins."""
        name = profile or self.name
        merged = dict(PROFILES.get(name) or PROFILES[DEFAULT_PROFILE])
        merged.update(self.extra_headers)
        if extra:
            merged.update(extra)
        return merged

    def candidates(self, *, ignore_cooldown: bool = False) -> List[str]:
        with self._lock:
            current = self._name
            cooling = time.monotonic() < self._all_blocked_until
        if self.pinned or (cooling and not ignore_cooldown):
            return [current]
        return [current] + [n for n in PROFILE_ORDER if n != current]

    def adopt(self, name: str, *, reason: str, persist: bool = True, source: str = "runtime") -> bool:
        with self._lock:
            if self.pinned or name == self._name or name not in PROFILES:
                return False
            old = self._name
            self._name = name
            self._all_blocked_until = 0.0
            now = time.monotonic()
            do_persist = persist and (
                self._last_persist is None
                or now - self._last_persist >= self._PERSIST_MIN_INTERVAL_S
            )
            if do_persist:
                self._last_persist = now
        logger.warning(
            f"http_profiles: switched profile | tenant={self.tenant_id} | "
            f"{old} → {name} | reason={reason}"
        )
        if do_persist:
            persist_profile(self.tenant_id, name, app=self._app, source=source)
        return True

    def mark_all_blocked(self) -> None:
        with self._lock:
            self._all_blocked_until = time.monotonic() + self._ALL_BLOCKED_COOLDOWN_S

    def reconfigure(self, *, profile: Optional[str] = None, pinned: Optional[bool] = None,
                    extra_headers: Optional[dict] = None) -> None:
        """Apply an admin/provisioning change to a resident loader without a rebuild."""
        with self._lock:
            if profile is not None and profile in PROFILES:
                self._name = profile
            if pinned is not None:
                self.pinned = bool(pinned)
            if extra_headers is not None:
                self.extra_headers = _clean_headers(extra_headers)
            self._all_blocked_until = 0.0

    def describe(self) -> dict:
        with self._lock:
            return {
                "profile": self._name,
                "pinned": self.pinned,
                "extra_header_names": sorted(self.extra_headers),
                "cooling_down": time.monotonic() < self._all_blocked_until,
            }


def persist_profile(tenant_id: str, name: str, *, app=None, source: str = "runtime") -> None:
    """Write the adopted profile to tenants.features on a separate thread.

    A separate thread gets its own app context and therefore its own DB
    session, so this never commits (or rolls back) a caller's in-flight
    transaction. Best-effort: failure only means the next rebuild starts from
    the old profile and switches again.
    """
    if not tenant_id:
        return
    if app is None:
        try:
            from flask import current_app
            app = current_app._get_current_object()
        except Exception:
            app = None
    if app is None:
        logger.warning(f"http_profiles: no Flask app — profile {name!r} not persisted | tenant={tenant_id}")
        return

    def _write():
        from models import db, Tenant
        with app.app_context():
            try:
                tenant = Tenant.query.get(uuid.UUID(str(tenant_id)))
                if tenant is None:
                    return
                feats = dict(tenant.features or {})
                if feats.get("http_profile_pinned"):
                    return
                feats["http_profile"] = name
                feats["http_profile_source"] = source
                feats["http_profile_updated_at"] = datetime.now(timezone.utc).isoformat()
                tenant.features = feats  # reassign: plain JSONB has no in-place change tracking
                db.session.commit()
                logger.info(f"http_profiles: persisted profile={name!r} | tenant={tenant_id}")
            except Exception as e:
                db.session.rollback()
                logger.error(f"http_profiles: persist failed | tenant={tenant_id} | {e}")

    threading.Thread(target=_write, daemon=True, name=f"httpprof-{str(tenant_id)[:8]}").start()


# ══════════════════════════════════════════════════════════════════════
# Sending
# ══════════════════════════════════════════════════════════════════════

def send(session: requests.Session, method: str, url: str, *, state: HttpProfileState,
         headers: Optional[dict] = None, **kwargs) -> requests.Response:
    """One request with the tenant's profile, falling back through the other
    profiles only when the response is a recognisable WAF block.

    Retrying is safe for POST too: a WAF block is issued by the web server
    before WordPress runs, so nothing was processed. Timeouts and connection
    errors raise exactly as before, and 5xx / JSON errors are returned as
    before — none of those are retried here, so an order can never be
    created twice by this function.

    If every profile is blocked, the last blocked response is returned and
    the caller's raise_for_status() handles it as it always did.
    """
    resp = None
    blocked: List[str] = []
    for name in state.candidates():
        resp = session.request(
            method, url, headers=state.headers(headers, profile=name), **kwargs
        )
        if not is_waf_block(resp):
            if blocked:
                state.adopt(name, reason=f"{','.join(blocked)} blocked on {_short(url)}")
            return resp
        blocked.append(name)
        logger.warning(
            f"http_profiles: WAF block | tenant={state.tenant_id} | profile={name} | "
            f"{method} {_short(url)} | {describe_block(resp)}"
        )

    if len(blocked) > 1:
        state.mark_all_blocked()
        logger.error(
            f"http_profiles: every profile blocked | tenant={state.tenant_id} | "
            f"tried={blocked} | {method} {_short(url)} — the host needs to whitelist "
            f"/wp-json/ or the backend's IP; pausing fallback for "
            f"{HttpProfileState._ALL_BLOCKED_COOLDOWN_S}s"
        )
    return resp


# ══════════════════════════════════════════════════════════════════════
# Provisioning probe
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ProbeResult:
    chosen: Optional[str] = None
    results: Dict[str, str] = field(default_factory=dict)  # profile → outcome
    all_blocked: bool = False   # every tested profile hit a firewall block
    unreachable: bool = False   # every tested profile failed at network level

    def summary(self) -> str:
        return "; ".join(f"{name}: {outcome}" for name, outcome in self.results.items())


def probe_profiles(*, state: HttpProfileState, wc_base: str, custom_api_base: str,
                   consumer_key: str, consumer_secret: str,
                   timeout: int = 15, session: Optional[requests.Session] = None) -> ProbeResult:
    """Find the first profile this store's firewall lets through.

    Three cheap requests per profile, covering both auth styles and both
    methods (WAF rules often treat request bodies differently from GETs):
      GET  wc/v3/data/currencies/current          (Basic Auth)
      GET  custom-api/v1/catalog-version          (X-Consumer-* headers)
      POST custom-api/v1/products-advanced-new    (read-only search, per_page=1)

    A profile passes when none of the three is a WAF block. Any WordPress
    answer counts — a 404 from an older plugin or a 401 still proves the
    headers got past the firewall; credential problems are the build's job
    to report, not this probe's.

    Starts with the tenant's current profile. A pinned tenant tests only its
    pinned profile.
    """
    session = session or requests.Session()
    basic = HTTPBasicAuth(consumer_key, consumer_secret)
    custom_headers = {"X-Consumer-Key": consumer_key, "X-Consumer-Secret": consumer_secret}
    checks = [
        ("GET",  f"{wc_base.rstrip('/')}/data/currencies/current", {"auth": basic}, None),
        ("GET",  f"{custom_api_base.rstrip('/')}/catalog-version", {}, custom_headers),
        ("POST", f"{custom_api_base.rstrip('/')}/products-advanced-new",
         {"json": {"page": 1, "per_page": 1}}, custom_headers),
    ]

    result = ProbeResult()
    names = state.candidates(ignore_cooldown=True)
    blocked_count = 0
    error_count = 0

    for name in names:
        outcome = "ok"
        for method, url, kwargs, extra in checks:
            try:
                resp = session.request(
                    method, url, headers=state.headers(extra, profile=name),
                    timeout=timeout, **kwargs,
                )
            except Exception as e:
                outcome = f"error on {method} {_short(url)}: {type(e).__name__}"
                error_count += 1
                break
            if is_waf_block(resp):
                outcome = f"blocked on {method} {_short(url)} ({describe_block(resp)})"
                blocked_count += 1
                break
        result.results[name] = outcome
        logger.info(f"http_profiles: probe | tenant={state.tenant_id} | profile={name} | {outcome}")
        if outcome == "ok":
            result.chosen = name
            break

    if result.chosen is None:
        result.all_blocked = blocked_count == len(names)
        result.unreachable = error_count == len(names)
    return result
