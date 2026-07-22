# models/usage_guard.py

from functools import wraps
from flask import request, jsonify
from datetime import date, datetime, timedelta, timezone
from models.chat_usage import ChatUsage, CustomerPlan

DAILY_FREE_LIMIT = 50

def enforce_daily_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Server-authoritative flow_state — NOT the client-supplied
        # user_context.flow_state, which a caller can set to any
        # _BOT_PROMPTED_STATES value to bypass the daily limit on every
        # request regardless of the conversation's real state.

        plan = CustomerPlan.get()
        if plan and plan.is_active_premium:
            return f(*args, **kwargs)

        new_count, exceeded = ChatUsage.increment_and_check(limit=DAILY_FREE_LIMIT)
        if exceeded:
            ...  # unchanged
        return f(*args, **kwargs)
    return decorated