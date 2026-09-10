# models/usage_guard.py

from functools import wraps
from flask import request, jsonify
from datetime import date, datetime, timedelta, timezone
from models.chat_usage import ChatUsage, CustomerPlan

DAILY_FREE_LIMIT = 25

# States where the user is replying to a bot-initiated prompt —
# derived directly from FlowState enum in conversation_flow.py
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
        body       = request.get_json(silent=True) or {}
        user_ctx   = body.get("user_context", {})
        flow_state = user_ctx.get("flow_state", "idle")

        # Bot-prompted replies are always free
        if flow_state in _BOT_PROMPTED_STATES:
            return f(*args, **kwargs)

        # Premium bypass — store-level
        plan = CustomerPlan.get()
        if plan and plan.is_active_premium:
            return f(*args, **kwargs)

        # Increment store's daily counter and check.
        # Timed: this is an INSERT ... ON CONFLICT against ONE shared row
        # (today's date) followed by a commit, so every concurrent request
        # takes a Postgres row lock on the SAME row and they serialize here.
        # If usage_guard dominates the timing breakdown, that lock is the
        # bottleneck (premium stores skip this path entirely).
        import timing_logger
        with timing_logger.stage("usage_guard"):
            new_count, exceeded = ChatUsage.increment_and_check(limit=DAILY_FREE_LIMIT)
        if exceeded:
            reset_at = datetime.combine(
                date.today() + timedelta(days=1),
                datetime.min.time(),
            ).replace(tzinfo=timezone.utc)

            return jsonify({
                "success": False,
                "error": {
                    "code":     "DAILY_LIMIT_REACHED",
                    "message":  f"This store has used all {DAILY_FREE_LIMIT} free questions for today.",
                    "limit":    DAILY_FREE_LIMIT,
                    "used":     new_count,
                    "reset_at": reset_at.isoformat(),
                }
            }), 429

        return f(*args, **kwargs)
    return decorated