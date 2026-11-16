"""
routes/shopify_admin.py — the embedded admin page of the public Shopify app.

The app is embedded (embedded = true) and installed through Shopify managed
installation (use_legacy_install_flow = false), so Shopify opens

    GET  /shopify/app?shop=...&host=...&embedded=1&id_token=...

inside the Shopify admin. That route serves the built React page
(built from the separate shopify-admin source into shopify-admin/dist) and nothing else. The page
then talks to the JSON routes below, every call carrying an App Bridge
session token (shopify_session.py), which is the ONLY thing that identifies
the store and the app:

    GET  /shopify/admin/status                  status of this store; also
                                                completes the install (below)
    POST /shopify/admin/retry                   re-run a failed setup
    POST /shopify/admin/instagram/disconnect    remove the Instagram link
    GET  /shopify/admin/conversations           recent shopper chats (read-only)
    GET  /shopify/admin/conversations/<id>      one chat's transcript

CONVERSATIONS (App Store requirement 5.1.5)
-------------------------------------------
Chats shoppers have on the storefront are shown back to the merchant here.
Read-only: deletion is the privacy webhooks (shopify_compliance.py) and the
nightly purge (purge_chat_history.py, 30 days). Each store's chats live in
that store's own database, bound from the session token's shop, so a merchant
can only ever see their own store's chats. Only what MiraQ already stores is
returned: the Shopify customer id (or none for guests) and the message text.
No customer names or emails are fetched.

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
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, make_response, request, send_from_directory
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import SQLAlchemyError

import merchant_session
from chat_logger import get_logger
from models import db, ChannelConnection, Conversation, Message, Tenant
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


# ── conversations (read-only) ────────────────────────────────────────────────

_CONV_PAGE_DEFAULT = 25
_CONV_PAGE_MAX = 50
_PREVIEW_CHARS = 120
_TRANSCRIPT_MAX_MESSAGES = 500


def _bind_store_db(shop: str):
    """
    (tenant, None) with per-store models (Conversation, Message) pointed at
    this store's database; (None, None) when the store has no database name
    yet; (None, response) when the store is unknown or uninstalled (its
    database is dropped after uninstall).

    No database-exists check: tenant_db_provisioner.list_existing_databases
    answers "none" when it can't connect, which would show a merchant an empty
    list instead of an error. A missing database fails the query instead; see
    _query_failed for what the route answers.
    """
    tenant = Tenant.query.filter_by(shopify_domain=shop).first()
    if tenant is None or tenant.status == "archived":
        return None, (jsonify({"error": "store not found"}), 404)
    if not tenant.db_name:
        return None, None
    # Imported here, as in shopify_compliance._bind, to keep module load light.
    from store_registry import bind_tenant_db
    bind_tenant_db(tenant)
    return tenant, None


def _query_failed(shop: str, tenant, e: Exception, empty):
    """
    A store whose setup failed may never have got its database: that is "no
    chats", not an error. For any other store a failed query is an outage, so
    the page shows an error rather than an empty list.
    """
    db.session.rollback()
    if tenant.status == "provision_failed":
        logger.info(f"shopify admin: no chats for a store whose setup failed | shop={shop} | {type(e).__name__}")
        return empty
    logger.error(f"shopify admin: conversations unavailable | shop={shop} | {type(e).__name__}: {e}")
    return jsonify({"error": "conversations_unavailable"}), 503


def _preview(text_value: str) -> str:
    """First question, whitespace collapsed and cut to _PREVIEW_CHARS."""
    flat = " ".join((text_value or "").split())
    if len(flat) <= _PREVIEW_CHARS:
        return flat
    return flat[: _PREVIEW_CHARS - 1].rstrip() + "…"


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)


def _encode_cursor(conv) -> str:
    """
    "<microseconds since 1970>|<conversation id>": exact (Postgres keeps
    microseconds) and URL-safe — an ISO time's "+00:00" turns into a space in
    a query string.
    """
    at = conv.updated_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return f"{(at - _EPOCH) // _MICROSECOND}|{conv.id}"


def _decode_cursor(raw: str):
    """(updated_at, id) from a cursor, or None if it isn't one."""
    micros, sep, conv_id = (raw or "").partition("|")
    if not sep or not micros.isdigit():
        return None
    try:
        return _EPOCH + int(micros) * _MICROSECOND, uuid.UUID(conv_id)
    except (ValueError, OverflowError):
        return None


def _page_size(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _CONV_PAGE_DEFAULT
    return max(1, min(n, _CONV_PAGE_MAX))


def _list_conversations(cursor, limit: int):
    """
    One page of conversations, most recent activity first, plus has_more.
    Only conversations where the shopper actually asked something. Must run
    after _bind_store_db.
    """
    q = Conversation.query.filter(Conversation.messages.any(Message.role == "user"))
    if cursor is not None:
        at, conv_id = cursor
        q = q.filter(or_(
            Conversation.updated_at < at,
            and_(Conversation.updated_at == at, Conversation.id < conv_id),
        ))
    rows = (q.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
             .limit(limit + 1).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    ids = [c.id for c in rows]
    if not ids:
        return [], False

    counts = dict(
        db.session.query(Message.conversation_id, func.count(Message.id))
        .filter(Message.conversation_id.in_(ids))
        .group_by(Message.conversation_id)
        .all()
    )
    ranked = (
        db.session.query(
            Message.conversation_id,
            Message.content,
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=(Message.created_at, Message.id),
            ).label("n"),
        )
        .filter(Message.conversation_id.in_(ids), Message.role == "user")
        .subquery()
    )
    firsts = dict(
        db.session.query(ranked.c.conversation_id, ranked.c.content)
        .filter(ranked.c.n == 1)
        .all()
    )
    return [
        {
            "id": str(c.id),
            "started_at": _iso(c.created_at),
            "last_active_at": _iso(c.updated_at),
            "customer_id": c.customer_id or None,
            "message_count": int(counts.get(c.id, 0)),
            "first_question": _preview(firsts.get(c.id, "")),
            "cursor": _encode_cursor(c),
        }
        for c in rows
    ], has_more


def _transcript(conv_id):
    """The conversation and its messages, or None. Must run after _bind_store_db."""
    conv = db.session.get(Conversation, conv_id)
    if conv is None:
        return None
    msgs = (Message.query
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at, Message.id)
            .limit(_TRANSCRIPT_MAX_MESSAGES + 1)
            .all())
    truncated = len(msgs) > _TRANSCRIPT_MAX_MESSAGES
    out = []
    for msg in msgs[:_TRANSCRIPT_MAX_MESSAGES]:
        meta = msg.metadata_json if isinstance(msg.metadata_json, dict) else {}
        products = meta.get("products")
        out.append({
            "from": "shopper" if msg.role == "user" else "miraq",
            "text": msg.content or "",
            "sent_at": _iso(msg.created_at),
            # Product cards aren't sent, only how many the shopper saw.
            "products_shown": len(products) if isinstance(products, list) else 0,
        })
    return {
        "id": str(conv.id),
        "started_at": _iso(conv.created_at),
        "customer_id": conv.customer_id or None,
        "messages": out,
        "truncated": truncated,
    }


@shopify_admin_bp.route("/shopify/admin/conversations", methods=["GET"])
def admin_conversations():
    """?before=<cursor of the last row shown>&limit=<1..50>"""
    shop, _app, token = _session()
    if shop is None:
        return token

    raw_before = request.args.get("before")
    cursor = _decode_cursor(raw_before) if raw_before else None
    if raw_before and cursor is None:
        return jsonify({"error": "invalid cursor"}), 400

    tenant, error = _bind_store_db(shop)
    if error:
        return error
    if tenant is None:
        return jsonify({"conversations": [], "has_more": False}), 200

    try:
        rows, has_more = _list_conversations(cursor, _page_size(request.args.get("limit")))
    except SQLAlchemyError as e:
        return _query_failed(shop, tenant, e, (jsonify({"conversations": [], "has_more": False}), 200))
    resp = jsonify({"conversations": rows, "has_more": has_more})
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


@shopify_admin_bp.route("/shopify/admin/conversations/<uuid:conversation_id>", methods=["GET"])
def admin_conversation(conversation_id):
    shop, _app, token = _session()
    if shop is None:
        return token

    tenant, error = _bind_store_db(shop)
    if error:
        return error
    try:
        found = _transcript(conversation_id) if tenant is not None else None
    except SQLAlchemyError as e:
        return _query_failed(shop, tenant, e, (jsonify({"error": "conversation not found"}), 404))
    if found is None:
        return jsonify({"error": "conversation not found"}), 404
    resp = jsonify(found)
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200
