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
               when it renders the page (includes/class-identity.php) with the
               store's own Ed25519 site key. The backend holds only the public
               key (site_key.py), so it can check tokens but never make one.
               No token → guest.

  Shopify      logged_in_customer_id from the App Proxy query, which Shopify
               signs. Also checks the signed `shop` matches this tenant, so a
               signed request from another store running the app cannot be
               replayed against this one. No proxy signature → guest —
               except under SHOPIFY_CUSTOMER_AUTH=insecure_client_claim
               (development only, never production), where the widget's
               own user_context.customer_id is trusted.

  Channels     routes/channel.py sets CHANNEL_ENVIRON_KEY on the in-process
               request it builds. WSGI environ keys without an HTTP_ prefix
               cannot be set by an HTTP client, so this cannot be spoofed.

Token format (WooCommerce): base64url(json payload) + "." + base64url(sig)
  payload: {"v": 2, "lid": license_id, "cid": wp_user_id, "role": str,
            "email": str, "iat": unix, "exp": unix}
  sig:     Ed25519(site private key, <payload part as ASCII>)

  v1 tokens (plugin before 1.0.8, HMAC with the old WooCommerce secret) are
  no longer verified. One from a page rendered by an old plugin is answered
  as expired, so the widget fetches a fresh v2 one.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from flask import g, request

from chat_logger import get_logger

logger = get_logger("miraq_chat")

IDENTITY_HEADER = "X-MiraQ-Identity"
CHANNEL_ENVIRON_KEY = "miraq.channel_identity"

_TOKEN_V1 = 1   # pre-1.0.8, no longer verified — see the module docstring
_TOKEN_V2 = 2   # Ed25519 site key
# Tolerance for clock differences between the WordPress host and this server.
_CLOCK_SKEW_S = 120


@dataclass(frozen=True)
class Identity:
    customer_id: str = ""      # "" = guest
    role: str = "guest"
    email: str = ""
    source: str = "anonymous"  # wp_token | shopify_proxy | channel | insecure_client_claim | anonymous
    # Channel requests only: who is writing, so a guest can be sent a sign-in link.
    channel: str = ""
    channel_user_id: str = ""
    # The verified WooCommerce token itself, forwarded to the plugin's
    # shopper-data routes (/my-orders), which check it again and return only
    # this shopper's orders. Never logged or shown.
    token: str = field(default="", repr=False, compare=False)

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


def sign_woo_identity_v2(payload: dict, private_key) -> str:
    """Build a v2 token with an Ed25519 private key. The plugin does this in
    PHP; kept here for tests and so the two implementations can be checked
    against each other."""
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_b64url_encode(private_key.sign(body.encode('ascii')))}"


def verify_woo_identity(token: str, *, license_id: str, public_key=None,
                        now: Optional[float] = None) -> Identity:
    """Verified identity from a plugin-signed token.

    public_key: the store's Ed25519 site key.

    Returns GUEST for anything that fails verification (logged — a forged or
    foreign token is worth seeing). Raises IdentityExpired for a token that is
    genuine but past its lifetime, and for a pre-1.0.8 (v1) token, so the
    widget fetches a fresh one.
    """
    if not token:
        return GUEST
    try:
        body, sig = token.split(".", 1)
        # Read only to pick the algorithm; nothing in it is trusted until the
        # signature for that algorithm checks out below.
        version = json.loads(_b64url_decode(body)).get("v")
    except Exception as e:
        logger.warning(f"identity: malformed token ({type(e).__name__}) — treating as guest")
        return GUEST

    if version == _TOKEN_V2:
        from site_key import verify as _verify_site_signature
        if public_key is None:
            logger.warning("identity: v2 token but no site key registered for this store — treating as guest")
            return GUEST
        if not _verify_site_signature(public_key, body.encode("ascii"), sig):
            logger.warning("identity: token signature mismatch — treating as guest")
            return GUEST
    elif version == _TOKEN_V1:
        # From a page an old plugin rendered; the widget refreshes.
        logger.info("identity: v1 token from a pre-1.0.8 page — asking the widget to refresh")
        raise IdentityExpired()
    else:
        logger.warning(f"identity: unsupported token version {version!r} — treating as guest")
        return GUEST

    try:
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return GUEST

    # Bound to one store: a token from store A must not work on store B.
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
        token=token,
    )


# ── per-request resolution ───────────────────────────────────────────────────

def _claimed_customer_id() -> str:
    """The customer id the widget says it is (POST /chat's user_context)."""
    body = request.get_json(silent=True)
    ctx = (body.get("user_context") if isinstance(body, dict) else None) or {}
    return str(ctx.get("customer_id") or "").strip() if isinstance(ctx, dict) else ""


def _shopify_identity(tenant) -> Identity:
    args = request.args
    if not args.get("signature"):
        # Not proxied: a direct call with the licence header, i.e. a widget
        # served from somewhere other than the storefront (local dev). In
        # production (app_proxy) that is always a guest. Only the development
        # mode chosen explicitly in .env trusts the browser here — without
        # this branch it could never apply to /chat, because the check below
        # needs a signature first. That mode's own purpose is developing the
        # widget before the app proxy is set up, which is exactly this case.
        from app_config import SHOPIFY_CUSTOMER_AUTH
        from ecommerce.shopify_proxy import MODE_INSECURE_CLIENT_CLAIM, resolve_shopify_customer_id
        if SHOPIFY_CUSTOMER_AUTH != MODE_INSECURE_CLIENT_CLAIM:
            return GUEST
        claimed = _claimed_customer_id() or (args.get("customer_id") or "").strip()
        if not claimed:
            return GUEST
        # Same helper as the signed path, so the per-request "NEVER use this
        # in production" warning is logged here too.
        customer_id, _err = resolve_shopify_customer_id(
            args, mode=SHOPIFY_CUSTOMER_AUTH, client_secret="", claimed_customer_id=claimed,
        )
        if not customer_id:
            return GUEST
        return Identity(customer_id=str(customer_id), role="customer", source="insecure_client_claim")

    shop = (args.get("shop") or "").strip().lower()
    if shop != (tenant.shopify_domain or "").strip().lower():
        logger.warning(
            f"identity: signed proxy request for shop={shop!r} used against "
            f"tenant {tenant.shopify_domain!r} — treating as guest"
        )
        return GUEST

    from app_config import SHOPIFY_CUSTOMER_AUTH, SHOPIFY_PROXY_MAX_AGE
    from ecommerce.shopify_proxy import resolve_shopify_customer_id
    from shopify_apps import app_for_shop
    claimed = ((request.get_json(silent=True) or {}).get("user_context") or {}).get("customer_id")
    customer_id, error = resolve_shopify_customer_id(
        args,
        mode=SHOPIFY_CUSTOMER_AUTH,
        client_secret=app_for_shop(shop).client_secret,  # public or this store's custom app
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
        # customer_id here comes from channel_links (a verified Shopify
        # sign-in), looked up by routes/channel.py — never from the caller.
        cid = str(channel.get("customer_id") or "")
        return Identity(customer_id=cid, role="customer" if cid else "guest", source="channel",
                        channel=str(channel.get("channel") or ""),
                        channel_user_id=str(channel.get("channel_user_id") or ""))

    if (tenant.ecommerce_backend or "woocommerce") == "shopify":
        return _shopify_identity(tenant)

    token = request.headers.get(IDENTITY_HEADER, "").strip()
    if not token:
        return GUEST
    import site_key
    return verify_woo_identity(
        token,
        license_id=tenant.license_id,
        public_key=site_key.public_key_for(tenant),
    )


def current_identity() -> Identity:
    """The identity bound by store_registry for this request; GUEST if none."""
    try:
        return g.__dict__.get("identity") or GUEST
    except RuntimeError:
        return GUEST