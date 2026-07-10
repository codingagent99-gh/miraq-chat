# models/usage_guard.py

from functools import wraps
from flask import request, jsonify
from datetime import date, datetime, timedelta, timezone
from models.chat_usage import ChatUsage, CustomerPlan
from models import Conversation
from handlers.chat_utils import resolve_session_id

DAILY_FREE_LIMIT = 25

_BOT_PROMPTED_STATES = {
    "awaiting_quantity",
    "awaiting_variant_selection",
    "awaiting_cart_confirmation",
    "awaiting_reorder_id",
    "awaiting_anything_else",
    "closing",
}

def enforce_daily_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Server-authoritative flow_state — NOT the client-supplied
        # user_context.flow_state, which a caller can set to any
        # _BOT_PROMPTED_STATES value to bypass the daily limit on every
        # request regardless of the conversation's real state.
        session_id = resolve_session_id()
        conversation = Conversation.query.get(session_id) if session_id else None
        flow_state = conversation.flow_state if conversation else "idle"

        if flow_state in _BOT_PROMPTED_STATES:
            return f(*args, **kwargs)

        plan = CustomerPlan.get()
        if plan and plan.is_active_premium:
            return f(*args, **kwargs)

        new_count, exceeded = ChatUsage.increment_and_check(limit=DAILY_FREE_LIMIT)
        if exceeded:
            ...  # unchanged
        return f(*args, **kwargs)
    return decorated