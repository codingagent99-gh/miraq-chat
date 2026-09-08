"""
handlers/handoff_handler.py — human handoff to WhatsApp.

v1 is deliberately dumb. When the shopper explicitly asks for a person we hand
them a click-to-chat link and stop. Nothing is sent from our side, so there is
no Meta app, no access token and no webhook — api.whatsapp.com/send is a URL
format, not an API. Because the shopper sends the first message, their reply
opens WhatsApp's 24-hour customer service window on the rep's side, and inbound
messages plus free-form replies inside that window are not billed.

Deliberately NOT done here:
  - flow state is left untouched. The conversation moves to WhatsApp, but if the
    shopper comes back to the widget mid-bulk-order their pending lines are
    still there. Clearing state would punish them for asking a question.
  - no rep routing. One support number, from config. Routing needs a rep
    identity resolved at link-build time and is a separate change.

Session management (db.session.commit) is the caller's responsibility in
chat.py, matching the other handlers.
"""

import re
import time
from urllib.parse import quote

from flask import jsonify

from config.store_config import (
    SUPPORT_WHATSAPP_ENABLED,
    SUPPORT_WHATSAPP_PHONE,
    SUPPORT_WHATSAPP_PREFILL,
)
from handlers.chat_utils import default_pagination


# Sentinel posted by a widget button, if one is ever added. Matched exactly so
# it can never collide with typed text.
HUMAN_HANDOFF_SENTINEL = "__TALK_TO_HUMAN__"


# Explicit asks only. No low-confidence auto-escalation: a shopper who is one
# turn away from an answer should not be pushed off the widget, and an
# auto-trigger fires hardest exactly when classification is already unreliable.
#
# "rep" is included even though reps are a domain concept here — a shopper
# saying "talk to a rep" wants a human either way.
_HUMAN_HANDOFF_RE = re.compile(
    r"(?i)\b(?:talk|speak|chat|connect)\s+(?:to|with)\s+"
    r"(?:a|an|the)?\s*"
    r"(?:human|person|real\s+person|agent|someone|somebody|rep|representative|"
    r"customer\s+(?:service|support|care))\b"
)


def is_human_handoff_request(message: str) -> bool:
    """True when the shopper explicitly asked for a person."""
    if not message:
        return False
    text = message.strip()
    if text == HUMAN_HANDOFF_SENTINEL:
        return True
    return bool(_HUMAN_HANDOFF_RE.search(text))

# Anchored to the exact phrase. "Continue anyway" (bulk company confirmation)
# and "Continue shopping" (post-add-to-cart) are existing chips, and this
# intercept sits above the flow state machine — an unanchored match would
# hijack both.
_RESUME_RE = re.compile(r"(?i)^\s*continue\s+here\s*$")

RESUME_SENTINEL = "__RESUME_AFTER_HANDOFF__"


def is_resume_after_handoff(message: str) -> bool:
    """True when the shopper tapped the resume chip on the handoff card."""
    if not message:
        return False
    text = message.strip()
    return text == RESUME_SENTINEL or bool(_RESUME_RE.match(text))

def build_whatsapp_link(phone: str = None, prefill: str = None) -> str:
    """
    Click-to-chat URL: https://api.whatsapp.com/send?phone=<digits>&text=<encoded>

    The phone number is international format with no '+', no spaces and no
    leading zeros — anything else silently opens a dead chat, so it is stripped
    to digits here rather than trusted from config.

    quote(safe="") is used rather than urlencode so spaces come out as %20.
    WhatsApp accepts '+' too, but %20 is what the documented format uses and it
    survives being copied into places that treat '+' literally.
    """
    digits = re.sub(r"\D", "", phone or SUPPORT_WHATSAPP_PHONE)
    text = prefill if prefill is not None else SUPPORT_WHATSAPP_PREFILL
    url = f"https://api.whatsapp.com/send?phone={digits}"
    if text:
        url += f"&text={quote(text, safe='')}"
    return url


def handle_human_handoff(conversation, page, start_time):
    """
    Return the WhatsApp handoff card. Never raises: if the number is missing or
    the feature is switched off, the shopper gets a plain apology instead of a
    broken link, which is the fail-safe that matters here — a dead wa.me link
    shows an error page with no way back.
    """
    elapsed = round((time.time() - start_time) * 1000)
    digits = re.sub(r"\D", "", SUPPORT_WHATSAPP_PHONE or "")

    if not SUPPORT_WHATSAPP_ENABLED or not digits:
        return jsonify({
            "success": True,
            "bot_message": (
                "I'm not able to connect you to a person from here right now. "
                "If you can tell me a bit more about what you're after, I'll try again."
            ),
            "intent": "human_handoff",
            "products": [],
            "suggestions": ["Browse Products", "Track my order"],
            "session_id": str(conversation.id),
            "metadata": {
                "handoff": "unavailable",
                "response_time_ms": elapsed,
            },
            "pagination": default_pagination(page),
        }), 200

    link = build_whatsapp_link()

    return jsonify({
        "success": True,
        "bot_message": (
            "Sure — you can talk to our team on WhatsApp here:\n\n"
            f"[Chat with us on WhatsApp]({link})\n\n"
            "Send the message that opens up and someone will pick it up. "
            "I'll keep everything here as it is in case you want to come back."
        ),
        "intent": "human_handoff",
        "products": [],
        # Additive: an unknown action type is ignored by the widget today, and
        # this is what a proper button binds to later without a backend change.
        "actions": [{
            "type": "SHOW_HUMAN_HANDOFF",
            "payload": {
                "channel": "whatsapp",
                "url": link,
                "label": "Chat with us on WhatsApp",
            },
        }],
        "suggestions": ["Continue here"],
        "session_id": str(conversation.id),
        "metadata": {
            "handoff": "whatsapp",
            "response_time_ms": elapsed,
        },
        "pagination": default_pagination(page),
    }), 200
    
def handle_resume_after_handoff(conversation, page, start_time):
    """
    Shopper tapped "Continue here" instead of leaving for WhatsApp.

    Handled as its own intercept rather than letting the chip text fall
    through to the classifier: "Continue here" carries no catalog signal, so
    it would reach the LLM fallback and cost a Mistral call plus a
    nondeterministic reply for what is a two-word acknowledgement.

    Flow state is untouched here for the same reason it was untouched on the
    way out — whatever the shopper was mid-way through is still valid.
    """
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "No problem — I'm still here. What would you like to do?",
        "intent": "human_handoff",
        "products": [],
        "suggestions": ["Browse Products", "Track my order"],
        "session_id": str(conversation.id),
        "metadata": {
            "handoff": "resumed",
            "response_time_ms": elapsed,
        },
        "pagination": default_pagination(page),
    }), 200