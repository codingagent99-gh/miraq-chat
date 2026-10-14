"""
routes/welcome.py — GET /chat/welcome, the data behind the widget's welcome
screen for logged-in shoppers (requirements: MiraQ Welcome Screen).

One call, no LLM, not counted against the free plan's daily questions.
Tenant, store loader and VERIFIED identity are bound by
store_registry.register_before_request like every other /chat route; an
expired WooCommerce identity token is answered with 401 there, before this
runs, so the widget refreshes it exactly as it does for /chat.

Sections are added one at a time. This version serves What's new; first_name,
last_conversation and buy_again join the same response in later steps.
"""

import time

from flask import Blueprint, g, jsonify

from chat_logger import get_logger
from identity import current_identity
from services.whats_new import record_visit, select_whats_new
from store_registry import get_store_loader

logger = get_logger("miraq_chat")

welcome_bp = Blueprint("welcome", __name__)


@welcome_bp.route("/chat/welcome", methods=["GET"])
def get_welcome():
    ident = current_identity()
    if ident.is_guest:
        # Guests keep the locked screen (FR-1.4). 403, not 401: 401 is the
        # widget's "refresh your identity token and retry" signal.
        return jsonify({"success": False, "error": "login_required"}), 403

    started = time.monotonic()
    tenant = g.__dict__.get("tenant")

    whats_new, mode = [], None
    try:
        since = record_visit(tenant, ident.customer_id)
        whats_new, mode = select_whats_new(get_store_loader(), since)
    except Exception as e:
        # A broken section is hidden, never a broken screen (FR-7.2).
        logger.error(f"/chat/welcome: whats_new failed — section hidden | {e}", exc_info=True)

    logger.info(
        f"/chat/welcome | whats_new={len(whats_new)} mode={mode} "
        f"| {int((time.monotonic() - started) * 1000)}ms"
    )
    return jsonify({
        "success":        True,
        "whats_new":      whats_new,
        "whats_new_mode": mode,
    }), 200
