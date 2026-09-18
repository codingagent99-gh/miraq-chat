"""
routes/channel.py — chat API for messaging channels (WhatsApp, Instagram).

POST /chat/channel is called server-to-server by the service that owns the
Meta webhook. It runs exactly the same turn as POST /chat and returns the
reply as native Send-API message objects that the caller POSTs, in order.

REQUEST
    Header  X-MiraQ-Channel-Key: <CHANNEL_API_KEY>
    {
      "channel":      "whatsapp" | "instagram",
      "user_id":      "<wa_id / phone>" | "<IGSID>",
      "message":      "typed text",                 # optional if reply_id given
      "reply_id":     "<button id / quick-reply or postback payload>",
      "user_context": {"customer_id": 123}          # optional, if the caller
    }                                               # has linked this user

RESPONSE (status mirrors the underlying /chat turn)
    {
      "success":    true,
      "channel":    "whatsapp",
      "user_id":    "...",
      "session_id": "...",
      "intent":     "...",
      "messages":   [ <message object>, ... ]
    }

SESSIONS
    One conversation per (channel, user_id), derived deterministically, so the
    caller keeps no state. The same flow_state / context_data machinery as the
    widget applies.

HOW THE TURN IS RUN
    chat() reads flask.request throughout (body, X-MiraQ-Session header,
    the usage-guard decorator), so it is dispatched in-process against a
    synthetic request inside a FRESH app context. The fresh app context gives
    the inner turn its own flask.g (timing buckets) and its own SQLAlchemy
    session, exactly as if it had arrived over HTTP. Extracting chat()'s body
    into a request-free function would remove this indirection; that is a
    larger refactor of routes/chat.py and is deliberately not done here.
"""

import hmac
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

from app_config import CHANNEL_API_KEY, CHANNEL_MAX_PRODUCT_CARDS
from channels import RENDERERS, SUPPORTED_CHANNELS, decode_reply_id
from channels.common import build_turn
from chat_logger import get_logger
from models import Conversation, Message

logger = get_logger("miraq_chat")

channel_bp = Blueprint("channel", __name__)

# Fixed namespace: changing it orphans every existing channel conversation.
_SESSION_NAMESPACE = uuid.UUID("6f1c2a9e-3b7d-4c55-9a2e-6d0f5b8e41c7")

# Only these user_context keys are accepted from the caller. role/email unlock
# rep and admin flows in /chat, which rely on the widget's own login — a
# channel user never gets them.
_ALLOWED_CONTEXT_KEYS = ("customer_id",)

LIMIT_REACHED_TEXT = (
    "Sorry, we can't answer more questions right now. Please try again tomorrow."
)
ERROR_TEXT = "Oops! Something went wrong. Please try again in a moment."


def _session_id_for(channel: str, user_id: str) -> uuid.UUID:
    return uuid.uuid5(_SESSION_NAMESPACE, f"{channel}:{user_id}")


def _authorized() -> bool:
    if not CHANNEL_API_KEY:
        return False
    supplied = request.headers.get("X-MiraQ-Channel-Key", "")
    return hmac.compare_digest(supplied.encode(), CHANNEL_API_KEY.encode())


def _last_user_message(session_id: uuid.UUID) -> str:
    msg = (
        Message.query
        .filter_by(conversation_id=session_id, role="user")
        .order_by(Message.id.desc())
        .first()
    )
    return msg.content if msg else ""


def _dispatch_chat(body: dict, session_id: uuid.UUID):
    """Run POST /chat in-process. Returns (status_code, json_dict)."""
    app = current_app._get_current_object()
    remote_addr = request.remote_addr or ""
    with app.app_context():
        with app.test_request_context(
            "/chat",
            method="POST",
            json=body,
            headers={"X-MiraQ-Session": str(session_id)},
            environ_base={"REMOTE_ADDR": remote_addr},
        ):
            resp = app.full_dispatch_request()
            return resp.status_code, (resp.get_json(silent=True) or {})


def _error(status: int, code: str, message: str):
    return jsonify({"success": False, "error": {"code": code, "message": message}}), status


@channel_bp.route("/chat/channel", methods=["POST"])
def channel_chat():
    start_time = time.time()

    if not _authorized():
        logger.warning("POST /chat/channel | unauthorized")
        return _error(401, "UNAUTHORIZED", "Missing or invalid X-MiraQ-Channel-Key.")

    body = request.get_json(silent=True) or {}
    channel = str(body.get("channel") or "").strip().lower()
    user_id = str(body.get("user_id") or "").strip()

    if channel not in SUPPORTED_CHANNELS:
        return _error(400, "INVALID_CHANNEL", f"channel must be one of: {', '.join(sorted(SUPPORTED_CHANNELS))}")
    if not user_id:
        return _error(400, "MISSING_USER_ID", "user_id is required.")

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

    # ── Build the widget-equivalent request ──
    conversation = Conversation.query.get(session_id)
    user_context = {
        k: body["user_context"][k]
        for k in _ALLOWED_CONTEXT_KEYS
        if isinstance(body.get("user_context"), dict) and body["user_context"].get(k)
    }
    # The widget echoes flow_state; /chat and the usage guard both read it.
    user_context["flow_state"] = (conversation.flow_state if conversation else None) or "idle"

    chat_body = {"message": message, "page": page, "user_context": user_context}

    status, data = _dispatch_chat(chat_body, session_id)

    # ── Non-turn failures from /chat ──
    if status == 429:
        data = {"bot_message": LIMIT_REACHED_TEXT, "intent": "daily_limit_reached"}
    elif not data.get("bot_message") and status >= 400:
        data = {"bot_message": ERROR_TEXT, "intent": "error"}

    turn = build_turn(data, max_products=CHANNEL_MAX_PRODUCT_CARDS)
    messages = RENDERERS[channel](turn, user_id)

    elapsed = round((time.time() - start_time) * 1000)
    logger.info(
        f"POST /chat/channel | channel={channel} | session={session_id} | "
        f"status={status} | intent={data.get('intent')} | messages={len(messages)} | {elapsed}ms"
    )

    return jsonify({
        "success":    bool(data.get("success", status < 400)),
        "channel":    channel,
        "user_id":    user_id,
        "session_id": str(session_id),
        "intent":     data.get("intent", ""),
        "messages":   messages,
    }), status