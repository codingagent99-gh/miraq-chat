"""
services/last_conversation.py — the welcome screen's "Continue where you left
off" (requirements FR-4).

The shopper's most recent WEBSITE conversation that they typed in, on any
device, with a one-line preview of the last thing they typed. Ownership is the
same rule /chat/history and history search use: Conversation.customer_id must
equal the verified customer. Switching to it needs no endpoint of its own —
the widget sends its id as X-MiraQ-Session and loads /chat/history.
"""

from typing import Optional

import sqlalchemy as sa

from chat_logger import get_logger

logger = get_logger("miraq_chat")

PREVIEW_MAX_CHARS = 80
# How far back in the chosen conversation to look for a typed (non-card)
# message before giving up on a preview.
_PREVIEW_SCAN = 20

# WhatsApp/Instagram conversations carry the customer_id too once the shopper
# links their account, but resuming one in the website widget would interleave
# the two threads. Their ids are uuid5 (routes/channel._session_id_for);
# website ids are uuid4 (resolve_session_id / the widget). The version is
# character 15 of the canonical text form: xxxxxxxx-xxxx-Vxxx-...
_CHANNEL_UUID_VERSION = "5"


def _uuid_version(column):
    return sa.func.substr(sa.cast(column, sa.Text), 15, 1)


def _preview(text: str) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= PREVIEW_MAX_CHARS:
        return flat
    return flat[: PREVIEW_MAX_CHARS - 1].rstrip() + "…"


def last_conversation(customer_id: str) -> Optional[dict]:
    """{id, preview, updated_at} or None (FR-4.4: hide Continue)."""
    if not customer_id:
        return None
    from models import db, Conversation, Message
    # Same text the transcript shows: the shopper's original language for a
    # translated turn, and friendly text for card payloads.
    from routes.chat import _history_display_text

    latest = (
        db.session.query(Message.conversation_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(
            Conversation.customer_id == customer_id,
            Message.role == "user",
            _uuid_version(Conversation.id) != _CHANNEL_UUID_VERSION,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .first()
    )
    if latest is None:
        return None
    conv_id = latest[0]
    conversation = db.session.get(Conversation, conv_id)

    recent = (
        Message.query
        .filter(Message.conversation_id == conv_id, Message.role == "user")
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(_PREVIEW_SCAN)
        .all()
    )
    # Prefer something the shopper typed over a card pick ("__SENTINEL__…"),
    # which would preview as "📅 All time" and the like.
    typed = next((m for m in recent if not (m.content or "").startswith("__")), None)
    chosen = typed or (recent[0] if recent else None)
    preview = ""
    if chosen is not None:
        i18n = (chosen.metadata_json or {}).get("i18n") or {}
        preview = _preview(_history_display_text(chosen, i18n))

    updated = conversation.updated_at if conversation is not None else None
    return {
        "id":         str(conv_id),
        "preview":    preview,
        "updated_at": updated.isoformat() if updated else None,
    }
