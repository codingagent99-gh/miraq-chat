"""
routes/channel.py — chat API for messaging channels (WhatsApp, Instagram),
multi-tenant.

Two surfaces, both server-to-server, both authenticated with the shared
secret in X-MiraQ-Channel-Key (app_config.CHANNEL_API_KEY):

  POST   /chat/channel          run one chat turn, return Send-API messages
  GET    /channel-connections   list a store's connected accounts
  POST   /channel-connections   link a WhatsApp number / IG account to a store
  DELETE /channel-connections   unlink one

TENANT RESOLUTION
    Both paths are exempt from store_registry's X-MiraQ-License-Id resolution.
    /chat/channel resolves the store from the account the customer messaged:

        (channel, channel_account_id) -> channel_connections -> tenants

    then binds it with bind_tenant_db(), exactly as the Shopify webhooks do.
    The caller never names a tenant, so it cannot aim a turn at a store that
    has not connected that account.

    Tenant status is decided by the inner /chat turn, which goes through the
    normal resolver: a warming store answers "still setting up", an expired
    licence is downgraded to free there. Only a store that is outright
    inactive is stopped here, with no messages — its customers should hear
    nothing rather than an error.

HOW THE TURN IS RUN
    chat() reads flask.request throughout, so it is dispatched in-process
    against a synthetic request inside a FRESH app context: its own flask.g
    and its own SQLAlchemy session, as if it had arrived over HTTP. The
    synthetic request carries the tenant's licence id, so store_registry
    resolves it like any widget request — store loader, per-tenant database
    and daily quota all apply unchanged.

SESSIONS
    One conversation per (channel, user_id). The id needs no tenant in it:
    conversations live in each tenant's own database, so the same customer
    messaging two stores lands in two different databases.

FEATURE GATE
    If tenant.features has a "channels" list, only the channels in it are
    served (e.g. ["whatsapp"]). With no "channels" key, all are allowed.
"""

import hmac
import time
import uuid

from flask import Blueprint, current_app, g, jsonify, request

from app_config import CHANNEL_API_KEY, CHANNEL_MAX_PRODUCT_CARDS
from channels import RENDERERS, SUPPORTED_CHANNELS, decode_reply_id
from channels.common import build_turn
from chat_logger import get_logger
from models import db, ChannelConnection, Conversation, Message, Tenant
from store_registry import bind_tenant_db, get_tenant_registry
from identity import CHANNEL_ENVIRON_KEY
import channel_link

logger = get_logger("miraq_chat")

channel_bp = Blueprint("channel", __name__)

# Fixed namespace: changing it orphans every existing channel conversation.
_SESSION_NAMESPACE = uuid.UUID("6f1c2a9e-3b7d-4c55-9a2e-6d0f5b8e41c7")

# No identity is accepted from the caller: not role/email (they unlock rep and
# admin flows) and, since channel sign-in, not customer_id either. Who a
# channel user is comes only from channel_links — a verified, one-time sign-in
# with the store's Shopify login (channel_link.py).
_ALLOWED_CONTEXT_KEYS = ()

UNLINKED_TEXT = (
    "You're signed out of this chat. I can still help you find products; "
    "to see your orders again, just ask and I'll send a sign-in link."
)
NOT_LINKED_TEXT = "You're not signed in on this chat, so there's nothing to sign out of."

# Statuses the inner /chat turn knows how to answer (see store_registry).
_SERVABLE_STATUSES = frozenset({"active", "warming", "provision_failed"})

LIMIT_REACHED_TEXT = (
    "Sorry, we can't answer more questions right now. Please try again tomorrow."
)
ERROR_TEXT = "Oops! Something went wrong. Please try again in a moment."


# ── helpers ──────────────────────────────────────────────────────────────────

def _error(status: int, code: str, message: str):
    return jsonify({"success": False, "error": {"code": code, "message": message}}), status


def _authorized() -> bool:
    if not CHANNEL_API_KEY:
        return False
    supplied = request.headers.get("X-MiraQ-Channel-Key", "")
    return hmac.compare_digest(supplied.encode(), CHANNEL_API_KEY.encode())


def _session_id_for(channel: str, user_id: str) -> uuid.UUID:
    return uuid.uuid5(_SESSION_NAMESPACE, f"{channel}:{user_id}")


def _norm_channel(value) -> str:
    return str(value or "").strip().lower()


def _last_user_message(session_id: uuid.UUID) -> str:
    msg = (
        Message.query
        .filter_by(conversation_id=session_id, role="user")
        .order_by(Message.id.desc())
        .first()
    )
    return msg.content if msg else ""


def _dispatch_chat(body: dict, session_id: uuid.UUID, license_id: str,
                   channel: str, channel_user_id: str, customer_id: str):
    """Run POST /chat in-process for this tenant. Returns (status_code, json_dict).

    /chat ignores identity in the body (identity.py). The verified customer
    (from channel_links, or "" for a guest) and the channel user travel on the
    in-process request's environ, which an HTTP client cannot set.
    """
    app = current_app._get_current_object()
    remote_addr = request.remote_addr or ""
    with app.app_context():
        with app.test_request_context(
            "/chat",
            method="POST",
            json=body,
            headers={
                "X-MiraQ-Session": str(session_id),
                "X-MiraQ-License-Id": license_id,
            },
            environ_base={
                "REMOTE_ADDR": remote_addr,
                CHANNEL_ENVIRON_KEY: {
                    "customer_id": str(customer_id or ""),
                    "channel": channel,
                    "channel_user_id": channel_user_id,
                },
            },
        ):
            resp = app.full_dispatch_request()
            return resp.status_code, (resp.get_json(silent=True) or {})


def _bind_resident_loader(tenant) -> None:
    """
    Expose the tenant's StoreLoader on the OUTER request so the renderers get
    this store's currency symbol. Resident-only: the inner turn has just
    built or touched it, so this is a cache hit and never triggers a rebuild.
    """
    registry = get_tenant_registry()
    if registry is None:
        g.store_loader = None
        return
    g.store_loader = dict(registry.resident_loaders()).get(str(tenant.tenant_id))


# ── POST /chat/channel ───────────────────────────────────────────────────────

@channel_bp.route("/chat/channel", methods=["POST"])
def channel_chat():
    start_time = time.time()

    if not _authorized():
        logger.warning("POST /chat/channel | unauthorized")
        return _error(401, "UNAUTHORIZED", "Missing or invalid X-MiraQ-Channel-Key.")

    body = request.get_json(silent=True) or {}
    channel = _norm_channel(body.get("channel"))
    account_id = str(body.get("channel_account_id") or "").strip()
    user_id = str(body.get("user_id") or "").strip()

    if channel not in SUPPORTED_CHANNELS:
        return _error(400, "INVALID_CHANNEL", f"channel must be one of: {', '.join(sorted(SUPPORTED_CHANNELS))}")
    if not account_id:
        return _error(400, "MISSING_CHANNEL_ACCOUNT_ID", "channel_account_id is required.")
    if not user_id:
        return _error(400, "MISSING_USER_ID", "user_id is required.")

    # ── Resolve the store from the account that was messaged ──
    connection = ChannelConnection.query.filter_by(
        channel=channel, external_account_id=account_id
    ).first()
    if connection is None or not connection.is_active:
        logger.warning(f"POST /chat/channel | unknown or disabled account | {channel}:{account_id}")
        return _error(404, "UNKNOWN_CHANNEL_ACCOUNT", "This account is not connected to any store.")

    tenant = db.session.get(Tenant, connection.tenant_id)
    if tenant is None or tenant.status not in _SERVABLE_STATUSES:
        logger.warning(
            f"POST /chat/channel | store not servable | {channel}:{account_id} | "
            f"status={getattr(tenant, 'status', None)}"
        )
        return _error(403, "TENANT_INACTIVE", "The store for this account is not active.")
    if not tenant.license_id:
        return _error(409, "TENANT_NOT_ADDRESSABLE", "The store for this account has no licence id.")

    enabled = (tenant.features or {}).get("channels")
    if isinstance(enabled, list) and channel not in enabled:
        return _error(403, "CHANNEL_NOT_ENABLED", f"{channel} is not enabled for this store.")

    # Per-tenant models (Conversation, Message) now read that store's database.
    bind_tenant_db(tenant)
    channel_link.ensure_tables(tenant)

    session_id = _session_id_for(channel, user_id)

    # ── Resolve the message: a tapped reply wins over the echoed title text ──
    message = str(body.get("message") or "").strip()
    page = 1
    reply_message, reply_page = decode_reply_id(str(body.get("reply_id") or ""))
    if reply_page is not None:
        message = _last_user_message(session_id)
        page = reply_page
        if not message:
            return _error(409, "NOTHING_TO_PAGINATE", "No previous query for this user.")
    elif reply_message:
        message = reply_message

    if not message:
        return _error(400, "EMPTY_MESSAGE", "message or a known reply_id is required.")

    # ── "unlink" / "log out": handled here, never sent to the chat engine ──
    if message.strip().lower() in channel_link.UNLINK_COMMANDS:
        was_linked = channel_link.unlink(channel, user_id)
        conv = db.session.get(Conversation, session_id)
        if conv is not None and was_linked:
            # Clear the account state the signed-in turns built up.
            conv.customer_id = None
            conv.context_data = {}
            conv.flow_state = "idle"
            db.session.commit()
        _bind_resident_loader(tenant)
        text = UNLINKED_TEXT if was_linked else NOT_LINKED_TEXT
        messages = RENDERERS[channel](build_turn({"bot_message": text}, max_products=0), user_id)
        logger.info(f"POST /chat/channel | unlink | tenant={tenant.tenant_id} | {channel} | linked={was_linked}")
        return jsonify({"success": True, "channel": channel, "user_id": user_id,
                        "session_id": str(session_id), "intent": "unlink", "messages": messages}), 200

    # Who is writing: the verified link for this channel user, or a guest.
    link = channel_link.find_link(channel, user_id)

    # ── Build the widget-equivalent request ──
    conversation = db.session.get(Conversation, session_id)
    raw_ctx = body.get("user_context") if isinstance(body.get("user_context"), dict) else {}
    user_context = {k: raw_ctx[k] for k in _ALLOWED_CONTEXT_KEYS if raw_ctx.get(k)}
    # The widget echoes flow_state; /chat and the usage guard both read it.
    user_context["flow_state"] = (conversation.flow_state if conversation else None) or "idle"

    chat_body = {"message": message, "page": page, "user_context": user_context}

    status, data = _dispatch_chat(chat_body, session_id, tenant.license_id,
                                  channel, user_id, link.customer_id if link else "")

    # First message after signing in: confirm who they're signed in as, once.
    if link is not None and link.notice_pending and status < 400:
        notice = (f"✅ Signed in as {link.email_display or 'your account'}. "
                  "Not you? Reply unlink.")
        data["bot_message"] = f"{notice}\n\n{data.get('bot_message') or ''}".strip()
        link.notice_pending = False
        db.session.commit()

    # ── Non-turn failures from /chat ──
    if status == 429:
        data = {"bot_message": LIMIT_REACHED_TEXT, "intent": "daily_limit_reached"}
    elif not data.get("bot_message") and status >= 400:
        data = {"bot_message": ERROR_TEXT, "intent": "error"}

    _bind_resident_loader(tenant)
    turn = build_turn(data, max_products=CHANNEL_MAX_PRODUCT_CARDS)
    messages = RENDERERS[channel](turn, user_id)

    elapsed = round((time.time() - start_time) * 1000)
    logger.info(
        f"POST /chat/channel | tenant={tenant.tenant_id} | {channel}:{account_id} | "
        f"session={session_id} | status={status} | intent={data.get('intent')} | "
        f"messages={len(messages)} | {elapsed}ms"
    )

    return jsonify({
        "success":    bool(data.get("success", status < 400)),
        "channel":    channel,
        "user_id":    user_id,
        "session_id": str(session_id),
        "intent":     data.get("intent", ""),
        "messages":   messages,
    }), status


# ── /channel-connections ─────────────────────────────────────────────────────

def _tenant_by_license(license_id: str):
    license_id = (license_id or "").strip()
    return Tenant.query.filter_by(license_id=license_id).first() if license_id else None


@channel_bp.route("/channel-connections", methods=["GET"])
def list_connections():
    if not _authorized():
        return _error(401, "UNAUTHORIZED", "Missing or invalid X-MiraQ-Channel-Key.")

    tenant = _tenant_by_license(request.args.get("license_id"))
    if tenant is None:
        return _error(404, "UNKNOWN_TENANT", "No store with that license_id.")

    rows = (ChannelConnection.query
            .filter_by(tenant_id=tenant.tenant_id)
            .order_by(ChannelConnection.channel, ChannelConnection.created_at)
            .all())
    return jsonify({"success": True, "connections": [r.to_dict() for r in rows]})


@channel_bp.route("/channel-connections", methods=["POST"])
def upsert_connection():
    """
    Link an account to a store, or update its status / display_name.
    An account already linked to a DIFFERENT store is refused (409): moving it
    must be a deliberate DELETE then POST, never a silent reassignment.
    """
    if not _authorized():
        return _error(401, "UNAUTHORIZED", "Missing or invalid X-MiraQ-Channel-Key.")

    body = request.get_json(silent=True) or {}
    channel = _norm_channel(body.get("channel"))
    account_id = str(body.get("external_account_id") or "").strip()
    status = str(body.get("status") or "active").strip().lower()

    if channel not in SUPPORTED_CHANNELS:
        return _error(400, "INVALID_CHANNEL", f"channel must be one of: {', '.join(sorted(SUPPORTED_CHANNELS))}")
    if not account_id:
        return _error(400, "MISSING_EXTERNAL_ACCOUNT_ID", "external_account_id is required.")
    if status not in ("active", "disabled"):
        return _error(400, "INVALID_STATUS", "status must be 'active' or 'disabled'.")

    tenant = _tenant_by_license(body.get("license_id"))
    if tenant is None:
        return _error(404, "UNKNOWN_TENANT", "No store with that license_id.")

    row = ChannelConnection.query.filter_by(channel=channel, external_account_id=account_id).first()
    if row is not None and row.tenant_id != tenant.tenant_id:
        return _error(409, "ACCOUNT_LINKED_ELSEWHERE",
                      "This account is connected to another store. Delete that connection first.")

    created = row is None
    if created:
        row = ChannelConnection(tenant_id=tenant.tenant_id, channel=channel, external_account_id=account_id)
        db.session.add(row)
    row.status = status
    if "display_name" in body:
        row.display_name = (str(body.get("display_name") or "").strip() or None)
    db.session.commit()

    logger.info(
        f"channel-connections: {'created' if created else 'updated'} | "
        f"tenant={tenant.tenant_id} | {channel}:{account_id} | status={status}"
    )
    return jsonify({"success": True, "created": created, "connection": row.to_dict()}), (201 if created else 200)


@channel_bp.route("/channel-connections", methods=["DELETE"])
def delete_connection():
    if not _authorized():
        return _error(401, "UNAUTHORIZED", "Missing or invalid X-MiraQ-Channel-Key.")

    body = request.get_json(silent=True) or {}
    channel = _norm_channel(body.get("channel"))
    account_id = str(body.get("external_account_id") or "").strip()

    row = ChannelConnection.query.filter_by(channel=channel, external_account_id=account_id).first()
    if row is None:
        return _error(404, "UNKNOWN_CHANNEL_ACCOUNT", "No such connection.")

    tenant_id = row.tenant_id
    db.session.delete(row)
    db.session.commit()
    logger.info(f"channel-connections: deleted | tenant={tenant_id} | {channel}:{account_id}")
    return jsonify({"success": True})