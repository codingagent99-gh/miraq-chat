"""
routes/welcome.py — GET /chat/welcome, the data behind the widget's welcome
screen for logged-in shoppers (requirements: MiraQ Welcome Screen).

One call, no LLM, not counted against the free plan's daily questions.
Tenant, store loader and VERIFIED identity are bound by
store_registry.register_before_request like every other /chat route; an
expired WooCommerce identity token is answered with 401 there, before this
runs, so the widget refreshes it exactly as it does for /chat.

Each section is built independently: one failing hides that section only,
never the screen (FR-7.2). Sections: whats_new, last_conversation, buy_again.

buy_again is the only section that calls the store (one orders request), so
it runs in a worker thread with a time budget: started first, it overlaps the
in-memory/DB sections, and if the store is slow the section is simply left
out (null) rather than holding the screen past the widget's own timeout.

Sessions: Continue sends last_conversation.id as X-MiraQ-Session and loads
/chat/history; New chat drops the stored id and /chat mints a new one. Neither
needs an endpoint here.
"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from flask import Blueprint, current_app, g, jsonify

from chat_logger import get_logger
from identity import current_identity
from services.buy_again import buy_again
from services.last_conversation import last_conversation
from services.whats_new import record_visit, select_whats_new
from store_registry import get_store_loader

logger = get_logger("miraq_chat")

welcome_bp = Blueprint("welcome", __name__)

# Seconds the welcome screen waits for the store's orders. The widget gives
# the whole call 8s; this keeps the screen well inside that.
BUY_AGAIN_BUDGET_S = 3.0
# Shared, bounded pool: a store that hangs ties up at most this many threads
# (until the store client's own timeout frees them); later welcome calls
# then just time out their buy_again and show the rest of the screen.
_buy_again_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="welcome-buy-again")


def _start_buy_again(loader, customer_id: str):
    """Run buy_again() off the request thread. Returns a Future.

    A worker thread has no request context, and copy_current_request_context
    would give it a fresh, empty g. So it gets its own app context with only
    what the order fetch reads bound explicitly — the same approach
    woo_client.execute_all() takes for its workers. The Shopify token row is
    control-plane, which the routing session reads from the default bind.
    """
    app = current_app._get_current_object()

    def run():
        with app.app_context():
            g.store_loader = loader
            return buy_again(loader, customer_id)

    return _buy_again_pool.submit(run)


@welcome_bp.route("/chat/welcome", methods=["GET"])
def get_welcome():
    ident = current_identity()
    if ident.is_guest:
        # Guests keep the locked screen (FR-1.4). 403, not 401: 401 is the
        # widget's "refresh your identity token and retry" signal.
        return jsonify({"success": False, "error": "login_required"}), 403

    started = time.monotonic()
    tenant = g.__dict__.get("tenant")
    loader = get_store_loader()

    # Started first so the store round trip overlaps the sections below.
    buy_again_future = None
    if loader is not None:
        try:
            buy_again_future = _start_buy_again(loader, ident.customer_id)
        except Exception as e:
            logger.error(f"/chat/welcome: buy_again not started — section hidden | {e}")

    whats_new, mode = [], None
    try:
        since = record_visit(tenant, ident.customer_id)
        whats_new, mode = select_whats_new(loader, since)
    except Exception as e:
        logger.error(f"/chat/welcome: whats_new failed — section hidden | {e}", exc_info=True)

    last_conv = None
    try:
        last_conv = last_conversation(ident.customer_id)
    except Exception as e:
        from models import db
        db.session.rollback()
        logger.error(f"/chat/welcome: last_conversation failed — section hidden | {e}", exc_info=True)

    # [] = no orders / nothing still available (hide, FR-5.5);
    # None = could not find out in time or it failed (also hidden).
    buy_again_items = None
    if buy_again_future is not None:
        remaining = BUY_AGAIN_BUDGET_S - (time.monotonic() - started)
        try:
            buy_again_items = buy_again_future.result(timeout=max(0.0, remaining))
        except FutureTimeout:
            logger.warning(f"/chat/welcome: buy_again over {BUY_AGAIN_BUDGET_S}s budget — section hidden")
        except Exception as e:
            logger.error(f"/chat/welcome: buy_again failed — section hidden | {e}", exc_info=True)

    logger.info(
        f"/chat/welcome | whats_new={len(whats_new)} mode={mode} "
        f"| last_conversation={'yes' if last_conv else 'no'} "
        f"| buy_again={'timeout/error' if buy_again_items is None else len(buy_again_items)} "
        f"| {int((time.monotonic() - started) * 1000)}ms"
    )
    return jsonify({
        "success":           True,
        "whats_new":         whats_new,
        "whats_new_mode":    mode,
        "last_conversation": last_conv,
        "buy_again":         buy_again_items or [],
    }), 200
