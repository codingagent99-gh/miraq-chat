"""
shopify_session.py — Shopify session tokens (App Bridge ID tokens) and the
token exchange that turns one into an offline Admin API token.

The embedded admin page (routes/shopify_admin.py) runs inside the Shopify
admin. Every request it makes carries

    Authorization: Bearer <session token>

which App Bridge mints for the merchant's current admin session. It is a JWT
signed HS256 with the secret of the app it was issued for:

    aud   the app's client id     -> which app (public or a custom one)
    dest  https://<shop>          -> which store
    iss   https://<shop>/admin
    exp / nbf                     -> about one minute of validity

verify_session_token() checks all of that and returns (shop, app). Nothing
from the request other than the token itself is trusted: the shop comes from
the signed `dest`, never from a query parameter.

exchange_for_offline_token() is Shopify's token exchange grant. With the app
installed through Shopify managed installation (use_legacy_install_flow =
false in the toml) there is no OAuth redirect and no /auth/callback; the
first time the embedded page loads after install, the backend swaps the
session token for an EXPIRING offline token plus refresh token — the same
pair the authorization code grant returns, stored the same way
(routes/shopify_oauth.store_token).

No PyJWT dependency: HS256 is one HMAC, and doing it here keeps the checks
explicit (algorithm pinned, audience pinned, no "none" algorithm path).
"""

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Optional, Tuple

import requests as http_requests

from chat_logger import get_logger
from shopify_apps import AppCredentials, app_for_install, is_embedded, public_app

logger = get_logger("miraq_chat")

# Session tokens live about 60 s. A little leeway covers clock skew between
# Shopify and this server; anything more would just widen the replay window.
_LEEWAY_SECONDS = 10

_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")

_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
_OFFLINE_TOKEN_TYPE = "urn:shopify:params:oauth:token-type:offline-access-token"


class SessionTokenError(Exception):
    """The request's session token is missing, malformed, forged or expired."""


class TokenExchangeError(Exception):
    """Shopify refused the token exchange. `retryable` = a fresh session token may work."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _host(url: str) -> str:
    """'https://x.myshopify.com/admin' -> 'x.myshopify.com' (lower-case)."""
    m = re.match(r"^https://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _app_for_audience(aud: str) -> Optional[AppCredentials]:
    """
    The app a token was issued for. Only an EMBEDDED app's tokens are accepted:
    session tokens come from App Bridge, and custom-distribution apps are not
    embedded (shopify_apps.is_embedded), so a token naming one is refused.
    Accepting them let anyone holding a registered custom app's secret sign a
    token for any store.
    """
    if not aud:
        return None
    pub = public_app()
    if pub.client_id and hmac.compare_digest(pub.client_id, aud):
        return pub
    app = app_for_install(aud)  # None for an unknown client id
    return app if app is not None and is_embedded(app) else None


def bearer_token(authorization_header: str) -> str:
    scheme, _, token = (authorization_header or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise SessionTokenError("missing bearer token")
    return token.strip()


def verify_session_token(token: str, now: Optional[float] = None) -> Tuple[str, AppCredentials]:
    """(shop, app) for a valid session token; raises SessionTokenError otherwise."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise SessionTokenError(f"malformed token: {e}") from None

    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise SessionTokenError("malformed token")
    if header.get("alg") != "HS256":
        raise SessionTokenError(f"unexpected alg {header.get('alg')!r}")

    # Which app — and so which secret — before trusting anything else in it.
    aud = claims.get("aud")
    if isinstance(aud, list):
        aud = aud[0] if len(aud) == 1 else None
    app = _app_for_audience(aud if isinstance(aud, str) else "")
    if app is None or not app.configured:
        raise SessionTokenError(f"token is for an unknown app (aud={aud!r})")

    expected = hmac.new(
        app.client_secret.encode("utf-8"),
        f"{header_b64}.{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise SessionTokenError("bad signature")

    now = time.time() if now is None else now
    try:
        exp = float(claims["exp"])
        nbf = float(claims.get("nbf", claims.get("iat", 0)))
    except (KeyError, TypeError, ValueError):
        raise SessionTokenError("missing exp/nbf") from None
    if now > exp + _LEEWAY_SECONDS:
        raise SessionTokenError("token expired")
    if now < nbf - _LEEWAY_SECONDS:
        raise SessionTokenError("token not yet valid")

    shop = _host(claims.get("dest", ""))
    if not _SHOP_RE.match(shop):
        raise SessionTokenError(f"bad dest {claims.get('dest')!r}")
    if _host(claims.get("iss", "")) != shop:
        raise SessionTokenError("iss and dest disagree")

    return shop, app


def exchange_for_offline_token(shop: str, app: AppCredentials, session_token: str) -> dict:
    """
    Token exchange: session token -> expiring offline token. Returns Shopify's
    JSON (access_token, scope, expires_in, refresh_token, refresh_token_expires_in).

    Call only with a token verify_session_token() has accepted for this shop.
    Shopify asks that acquiring and refreshing never run at the same time for
    one store; the caller holds the per-shop install lock for that reason.
    """
    try:
        resp = http_requests.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": app.client_id,
                "client_secret": app.client_secret,
                "grant_type": _TOKEN_EXCHANGE_GRANT,
                "subject_token": session_token,
                "subject_token_type": _ID_TOKEN_TYPE,
                "requested_token_type": _OFFLINE_TOKEN_TYPE,
                # Expiring offline token + refresh token. Public apps may not
                # use non-expiring ones (required for apps created after
                # 2026-04-01, for all public apps from 2027-01-01).
                "expiring": "1",
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except Exception as e:
        raise TokenExchangeError(f"request failed: {e}", retryable=True) from None

    if resp.status_code >= 400:
        body = resp.text[:500]
        logger.error(f"shopify token exchange: HTTP {resp.status_code} | shop={shop} | body={body!r}")
        # 400 invalid_subject_token: the session token expired in transit or was
        # already used; a fresh one from App Bridge usually works.
        raise TokenExchangeError(
            f"HTTP {resp.status_code}", retryable=resp.status_code in (400, 401, 429) or resp.status_code >= 500
        )
    try:
        return resp.json()
    except ValueError:
        raise TokenExchangeError("non-JSON response", retryable=True) from None
