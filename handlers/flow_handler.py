"""
handlers/flow_handler.py — Step 0: Multi-turn conversation flow handling.

Handles all FlowState branches that are not consumed directly by
the cart-confirmation intercept in routes/chat.py.
Returns a Flask response if the flow consumed the message, or None to fall through.
"""

import time

from flask import jsonify

from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination

logger = get_logger("miraq_chat")


def handle_flow(
    flow_result: dict,
    user_context: dict,
    session_id: str,
    customer_id,
    page: int,
    start_time: float,
):
    """
    Process the result of handle_flow_state().
    Returns a Flask response tuple if the flow consumed this turn, else None.
    """
    if not flow_result:
        return None

    # ── Flow handler consumed the message entirely ──
    if not flow_result.get("pass_through"):
        logger.info(f"Step 0: Flow handler consumed message | new_state={flow_result.get('flow_state', 'idle')}")
        flow_metadata: dict = {
            "flow_state": flow_result.get("flow_state", "idle"),
            "response_time_ms": round((time.time() - start_time) * 1000),
            "provider": "conversation_flow",
        }
        for ctx_key in ("pending_product_name", "pending_product_id", "pending_quantity", "pending_variation_id"):
            if flow_result.get(ctx_key) is not None:
                flow_metadata[ctx_key] = flow_result[ctx_key]
            elif user_context.get(ctx_key) is not None:
                flow_metadata[ctx_key] = user_context[ctx_key]
        return jsonify({
            "success": True,
            "bot_message": flow_result["bot_message"],
            "intent": "guided_flow",
            "products": [],
            "suggestions": flow_result.get("suggestions", []),
            "session_id": session_id,
            "metadata": flow_metadata,
            "flow_state": flow_result.get("flow_state", "idle"),
            "pagination": default_pagination(page),
        }), 200

    return None