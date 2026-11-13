"""
routes/shopify_admin.py — the embedded admin page of the public Shopify app.

The app is embedded (embedded = true) and installed through Shopify managed
installation (use_legacy_install_flow = false), so Shopify opens

    GET  /shopify/app?shop=...&host=...&embedded=1&id_token=...

inside the Shopify admin. That route serves the built React page
(shopify-admin/ in this repo, built into shopify-admin/dist) and nothing else. The page
then talks to the JSON routes below, every call carrying an App Bridge
session token (shopify_session.py), which is the ONLY thing that identifies
the store and the app:

    GET  /shopify/admin/status                  status of this store; also
                                                completes the install (below)
    POST /shopify/admin/retry                   re-run a failed setup
    POST /shopify/admin/instagram/disconnect    remove the Instagram link

INSTALL WITHOUT A CALLBACK
--------------------------
Managed installation means Shopify never calls /shopify/auth/callback for
this app. The first /status after install is what creates the store: it
exchanges the session token for an expiring offline token (token exchange)
and runs the same steps as the OAuth callback — store_token, record_install,
provision_tenant (routes/shopify_oauth.py). Later loads only re-exchange when
the stored token is missing or broken, so opening the app never rebuilds the
catalog of a store that is already set up.

Two gunicorn workers and a page that may fire twice could otherwise race and
create two tenants for one shop, so the install runs under a Postgres advisory
lock per shop. A request that does not get the lock answers 202 "busy" and the
page asks again a moment later.

The custom-distribution apps are untouched: they stay non-embedded on
/shopify/install/<client_id> and the /installed page.

CONFIG (.env)
  SHOPIFY_ADMIN_DIST    built page (default: <backend>/shopify-admin/dist)
  MIRAQ_SUPPORT_EMAIL   } shown on the page when set
  MIRAQ_SUPPORT_URL     }
  MIRAQ_PRIVACY_URL     }
"""

import html
import os
import re
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, make_response, request, send_from_directory
from sqlalchemy import text

import merchant_session
from chat_logger import get_logger
from models import db, ChannelConnection, Tenant
from models.shopify_token import ShopifyToken
from routes import instagram_connect
from routes.shopify_oauth import (
    missing_scopes,
    provision_tenant,
    store_token,
    token_payload_error,
)
from shopify_apps import app_for_shop, public_app, record_install
from shopify_session import (
    SessionTokenError,
    TokenExchangeError,
    bearer_token,
    exchange_for_offline_token,
    verify_session_token,
)

logger = get_logger("miraq_chat")

shopify_admin_bp = Blueprint("shopify_admin", __name__)

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_DIST = os.getenv("SHOPIFY_ADMIN_DIST", os.path.join(_BACKEND_DIR, "shopify-admin", "dist"))
_ASSETS_DIR = "app-assets"  # must match build.assetsDir in shopify-admin/vite.config.ts

_SUPPORT = {
    "email": os.getenv("MIRAQ_SUPPORT_EMAIL", "").strip(),
    "url": os.getenv("MIRAQ_SUPPORT_URL", "").strip(),
    "privacy_url": os.getenv("MIRAQ_PRIVACY_URL", "").strip(),
}

# The storefront app embed: extensions/miraq-shopper-agent/blocks/miraq_widget.liquid
_EMBED_BLOCK_HANDLE = "miraq_widget"

_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")


# ── the page ─────────────────────────────────────────────────────────────────

@shopify_admin_bp.route("/shopify/app", methods=["GET"])
def admin_page():
    """
    The built index.html, with the public app's client id filled in for App
    Bridge (<meta name="shopify-api-key">) and a CSP that lets only this
    store's admin frame it — required for embedded apps.
    """
    index_path = os.path.join(ADMIN_DIST, "index.html")
    try:
        with open(index_path, encoding="utf-8") as f:
            page = f.read()
    except OSError:
        logger.error(f"shopify admin: page not built — {index_path} is missing")
        return jsonify({"error": "admin page is not deployed"}), 503

    client_id = public_app().client_id
    if not client_id:
        logger.error("shopify admin: SHOPIFY_CLIENT_ID is not set")
        return jsonify({"error": "app not configured"}), 500
    page = page.replace("%SHOPIFY_API_KEY%", html.escape(client_id, quote=True))

    # `shop` is unsigned here, but it only widens WHO may frame the page, and
    # only to a myshopify.com host; the page itself holds nothing until a
    # session token proves the store.
    shop = (request.args.get("shop") or "").strip().lower()
    ancestors = "https://admin.shopify.com"
    if _SHOP_RE.match(shop):
        ancestors = f"https://{shop} {ancestors}"

    resp = make_response(page, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors};"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@shopify_admin_bp.route(f"/shopify/{_ASSETS_DIR}/<path:filename>", methods=["GET"])
def admin_assets(filename):
    """JS/CSS of the page. File names carry a content hash, so cache them for good."""
    directory = os.path.join(ADMIN_DIST, _ASSETS_DIR)
    if not os.path.isdir(directory):
        abort(404)
    resp = send_from_directory(directory, filename, max_age=31536000)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ── auth ─────────────────────────────────────────────────────────────────────

def _session():
    """(shop, app, raw token) from the Authorization header, or (None, None, error response)."""
    try:
        token = bearer_token(request.headers.get("Authorization", ""))
        shop, app = verify_session_token(token)
    except SessionTokenError as e:
        logger.warning(f"shopify admin: rejected session token | {e} | path={request.path}")
        resp = jsonify({"error": "invalid_session"})
        # App Bridge's fetch retries once with a fresh token when it sees this.
        resp.headers["X-Shopify-Retry-Invalid-Session-Request"] = "1"
        return None, None, (resp, 401)
    return shop, app, token


@contextmanager
def _install_lock(shop: str):
    """
    Per-shop Postgres advisory lock, held on its own connection. Yields True
    if acquired. Non-blocking: a second request for the same shop gets False
    straight away instead of tying up a worker thread.
    """
    key = f"miraq-shopify-install:{shop}"
    conn = db.engine.connect()
    got = False
    try:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}).scalar())
        yield got
    finally:
        try:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
        finally:
            conn.close()


def _busy():
    return jsonify({"state": "busy"}), 202


# ── install state ────────────────────────────────────────────────────────────

def _token_problem(shop: str):
    """Why the stored Admin API token can't be relied on, or None if it can."""
    row = db.session.get(ShopifyToken, shop)
    if row is None:
        return "no token"
    if not row.refresh_token:
        return "token has no refresh token"
    if row.last_error:
        return "last refresh failed"
    refresh_expires = row.refresh_token_expires_at
    if refresh_expires is not None:
        if refresh_expires.tzinfo is None:
            refresh_expires = refresh_expires.replace(tzinfo=timezone.utc)
        if refresh_expires <= datetime.now(timezone.utc):
            return "refresh token expired"
    if missing_scopes(row.scope or ""):
        return "scopes missing"
    return None


def _tenant_problem(shop: str, app):
    """Why the store needs the full install, or None if it is set up for `app`."""
    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    if tenant is None:
        return "no tenant"
    if tenant.status == "archived":
        return "tenant archived"
    if app_for_shop(shop).client_id != app.client_id:
        return "store is bound to another app"
    return None


def _ensure_installed(shop: str, app, session_token: str):
    """
    None when the store is set up (or was just set up), else an error response.
    Must run under _install_lock.
    """
    needs_tenant = _tenant_problem(shop, app)
    needs_token = _token_problem(shop)
    if not needs_tenant and not needs_token:
        return None

    logger.info(
        f"shopify admin: token exchange | shop={shop} "
        f"tenant={needs_tenant or 'ok'} token={needs_token or 'ok'}"
    )
    try:
        payload = exchange_for_offline_token(shop, app, session_token)
    except TokenExchangeError as e:
        if e.retryable:
            resp = jsonify({"error": "invalid_session"})
            resp.headers["X-Shopify-Retry-Invalid-Session-Request"] = "1"
            return resp, 401
        return jsonify({"error": "token_exchange_failed"}), 502

    error = token_payload_error(payload, shop)
    if error:
        return error
    store_token(shop, payload)

    if needs_tenant:
        record_install(shop, app)
        error = provision_tenant(shop)
        if error:
            return error
    return None


# ── status ───────────────────────────────────────────────────────────────────

def _setup_state(tenant) -> str:
    if tenant is None:
        return "failed"
    if tenant.status == "active":
        return "ready"
    if tenant.status == "provision_failed":
        return "failed"
    return "indexing"  # warming, or anything transitional


def _instagram(shop: str, tenant):
    if tenant is None or tenant.status == "archived" or not instagram_connect.is_configured():
        return {"available": False}
    row = (ChannelConnection.query
           .filter_by(tenant_id=tenant.tenant_id, channel="instagram")
           .order_by(ChannelConnection.updated_at.desc())
           .first())
    # The connect flow is Facebook Login, which cannot run inside the admin
    # iframe; the page opens this URL in a new tab. The owner token proves the
    # browser is the store owner's (merchant_session.py), as on /installed.
    connect_url = instagram_connect._url(
        "instagram/connect?" + urllib.parse.urlencode({"shop": shop, "t": merchant_session.issue(shop)})
    )
    connected = row is not None and row.is_active
    return {
        "available": True,
        "connected": connected,
        "account": (row.display_name or row.external_account_id) if connected else None,
        "connect_url": connect_url,
    }


def _status_payload(shop: str, app):
    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    return {
        "shop": shop,
        "setup": _setup_state(tenant),
        "embed_url": (
            f"https://{shop}/admin/themes/current/editor?context=apps"
            f"&activateAppId={app.client_id}/{_EMBED_BLOCK_HANDLE}"
        ),
        "instagram": _instagram(shop, tenant),
        "support": {k: v for k, v in _SUPPORT.items() if v},
    }


@shopify_admin_bp.route("/shopify/admin/status", methods=["GET"])
def admin_status():
    shop, app, token = _session()
    if shop is None:
        return token  # _session() returns the 401 response in the token slot

    with _install_lock(shop) as locked:
        if not locked:
            return _busy()
        error = _ensure_installed(shop, app, token)
        if error:
            return error
        return jsonify(_status_payload(shop, app)), 200


@shopify_admin_bp.route("/shopify/admin/retry", methods=["POST"])
def admin_retry():
    """Re-run setup for a store whose database setup or catalog build failed."""
    shop, app, token = _session()
    if shop is None:
        return token

    with _install_lock(shop) as locked:
        if not locked:
            return _busy()
        error = _ensure_installed(shop, app, token)
        if error:
            return error
        tenant = Tenant.query.filter_by(shopify_domain=shop).first()
        if tenant is not None and tenant.status == "provision_failed":
            logger.info(f"shopify admin: retrying setup | shop={shop}")
            error = provision_tenant(shop)
            if error:
                return error
        return jsonify(_status_payload(shop, app)), 200


@shopify_admin_bp.route("/shopify/admin/instagram/disconnect", methods=["POST"])
def admin_instagram_disconnect():
    shop, app, token = _session()
    if shop is None:
        return token

    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    if tenant is None or tenant.status == "archived":
        return jsonify({"error": "store not found"}), 404
    removed = (ChannelConnection.query
               .filter_by(tenant_id=tenant.tenant_id, channel="instagram")
               .delete(synchronize_session=False))
    db.session.commit()
    logger.info(f"shopify admin: instagram disconnected | shop={shop} removed={removed}")
    return jsonify(_status_payload(shop, app)), 200
