"""
merchant_session.py — short-lived proof that a browser belongs to a store's
owner, for the merchant pages this backend renders itself (the app page at
/installed and the Instagram connect flow in routes/instagram_connect.py).

The Shopify app is not embedded, so there is no Shopify session token to lean
on. Instead, a page is only treated as the owner's once one of these has been
seen:

  * a Shopify-signed app open (/shopify/install/... with a valid hmac and a
    fresh timestamp), or
  * a completed install (the OAuth callback),

and both then hand the browser a signed token for that shop:

    <timestamp>.<hmac(secret, "<purpose>:<timestamp>:<shop>")>

signed with the secret of the app the store installed through, the same key
and shape as the install `state` in routes/shopify_oauth.py. The purpose
string keeps tokens from one use (say an Instagram OAuth state) from being
replayed as another (the page token), and vice versa. Stateless on purpose:
no session store, nothing for a multi-worker deployment to lose.
"""

import base64
import hashlib
import hmac
import time

from shopify_apps import app_for_shop

# Purposes. Each has its own lifetime.
PAGE = "merchant-page"        # carried on the app page's links and forms
IG_STATE = "ig-connect-state"  # Facebook Login `state` round trip

_MAX_AGE = {
    PAGE: 60 * 60,      # an app page left open for an hour still works
    IG_STATE: 10 * 60,  # same window as the Shopify install state
}


def _sign(purpose: str, ts: str, shop: str) -> str:
    secret = app_for_shop(shop).client_secret or ""
    if not secret:
        raise RuntimeError("merchant_session: no app secret configured for this shop")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{purpose}:{ts}:{shop}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{ts}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def issue(shop: str, purpose: str = PAGE) -> str:
    return _sign(purpose, str(int(time.time())), shop)


def verify(token: str, shop: str, purpose: str = PAGE) -> bool:
    try:
        ts, _ = (token or "").split(".", 1)
        age = time.time() - int(ts)
    except (ValueError, AttributeError, TypeError):
        return False
    if age > _MAX_AGE[purpose] or age < -60:
        return False
    try:
        expected = _sign(purpose, ts, shop)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, token)
