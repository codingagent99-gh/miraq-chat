"""
handlers/order_stats_handler.py — "how many samples did <rep> order in <range>"
and "who ordered how many".

Backed by GET /order-stats-by-rep, which aggregates on `_billing_project_rep`
(the meta both the storefront's rep selector and MiraQ write), so counts cover
orders placed through either path.

Design notes worth keeping:
  * Every product in this store is a sample — nothing is sold for money — so
    "sample count" is simply total line-item quantity. There is no product
    filter to apply.
  * Access is gated HERE and again in the plugin. This layer decides what to
    offer; the plugin is the enforcement point.
  * A number is never rendered bare: which statuses were counted, whether the
    scan was truncated, and how many orders had no rep credited all travel
    with the total. A confidently wrong figure is worse than a caveated one.
"""

import time

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from chat_logger import get_logger
from conversation_flow import FlowState
from app_config import ORDER_REPORT_STATUSES, is_order_report_admin
from handlers.chat_utils import default_pagination

logger = get_logger("miraq_chat")


def _describe_range(date_after, date_before) -> str:
    """Human phrase for the window, or '' when unbounded."""
    if date_after and date_before:
        return f"{date_after[:10]} to {date_before[:10]}"
    if date_after:
        return f"since {date_after[:10]}"
    if date_before:
        return f"up to {date_before[:10]}"
    return ""


def handle_order_stats(
    entities, role, customer_id, conversation, page, start_time,
):
    """Answer an order/sample-count question. Returns a Flask response."""

    def _respond(message, suggestions=None, metadata=None):
        elapsed = round((time.time() - start_time) * 1000)
        meta = {"response_time_ms": elapsed}
        meta.update(metadata or {})
        return jsonify({
            "success": True,
            "bot_message": message,
            "intent": "order_stats_by_rep",
            "products": [],
            "suggestions": suggestions or ["Browse Products", "View my orders"],
            "session_id": str(conversation.id),
            "metadata": meta,
            "pagination": default_pagination(page),
        }), 200

    # ── Access: administrators only ─────────────────────────────────────────
    # Refuse explicitly. An unauthorized user must not get a zeroed report
    # that reads like "nobody ordered anything".
    if not is_order_report_admin(role):
        logger.info(f"order_stats | refused for role={role!r}")
        return _respond(
            "Order reporting is only available to administrators."
        )

    requested_rep = getattr(entities, "target_rep_name", None)

    date_after  = getattr(entities, "date_after", None)
    date_before = getattr(entities, "date_before", None)

    call = endpoints.order_stats_by_rep(
        requesting_customer_id=customer_id,
        date_after=date_after,
        date_before=date_before,
        rep=requested_rep or None,
        statuses=list(ORDER_REPORT_STATUSES),
        description="Order/sample counts by rep",
    )
    result = woo_client.execute(call)
    data = result.get("data") or {}

    if not result.get("success") or not isinstance(data, dict):
        # woo_client surfaces the plugin's own error code; use it rather than
        # collapsing every failure into "try again", which sends the user to
        # retry a query that will never succeed.
        code = result.get("error_code")
        if code == "rep_not_found":
            # The plugin distinguishes "no user by that name" from "the user
            # exists but has no cs_rep role" — the latter is a role-assignment
            # problem, and reporting it as a spelling issue sends the admin
            # hunting for a typo that isn't there.
            _msg = result.get("error_message") or ""
            if "cs_rep role" in _msg:
                return _respond(
                    f"**{requested_rep}** has a user account, but it isn't "
                    "assigned the sales rep role, so no orders are credited to "
                    "them. An administrator can add that role in WordPress.",
                    metadata={"error_code": code, "reason": "not_a_rep",
                              "requested_name": requested_rep},
                )
            return _respond(
                f"I couldn't find a rep called **{requested_rep}**. "
                "Check the spelling, or try their full name as it appears in "
                "WordPress.",
                metadata={"error_code": code, "reason": "no_such_name",
                          "requested_name": requested_rep},
            )
        if code == "forbidden":
            return _respond(
                "Your account doesn't have permission to run that report."
            )
        logger.warning(
            f"order_stats | lookup failed: code={code!r} err={result.get('error')}"
        )
        return _respond(
            "I couldn't pull the order figures just now. Please try again in a moment."
        )

    total_orders  = data.get("total_orders", 0)

    # ── Several cs_reps match the name — let the admin choose ───────────────
    if data.get("ambiguous_rep"):
        matches = data.get("matches") or []
        asked   = data.get("requested_name") or requested_rep or "that name"
        options = [
            f"{m.get('label')} — {m.get('email')}" if m.get("label") and m.get("email")
            else (m.get("label") or m.get("email") or "")
            for m in matches
        ]
        user_context = conversation.context_data or {}
        user_context["pending_rep_choice"] = {
            "candidates":  [{"label": m.get("label"), "email": m.get("email")} for m in matches],
            "date_after":  date_after,
            "date_before": date_before,
            "requested":   asked,
        }
        conversation.flow_state = FlowState.AWAITING_REP_CHOICE.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _respond(
            f"**{len(matches)}** sales reps match **{asked}**. Which one did you mean?",
            suggestions=options[:8] + ["Cancel"],
            metadata={"flow_state": FlowState.AWAITING_REP_CHOICE.value,
                      "candidates": options, "requested_name": asked},
        )

    total_items = data.get("total_items", 0)
    truncated   = bool(data.get("truncated"))
    name        = data.get("rep_filter_label") or requested_rep or "That rep"
    window      = _describe_range(data.get("date_after"), data.get("date_before"))
    scope       = f" ({window})" if window else " (all time)"

    if total_orders == 0:
        return _respond(
            f"**{name}** has no orders{scope}.",
            metadata={"total_orders": 0},
        )

    msg = (
        f"**{name}** — **{total_orders} order{'s' if total_orders != 1 else ''}**, "
        f"**{total_items} sample{'s' if total_items != 1 else ''}**{scope}."
    )
    # Only surfaced when it actually fires: without it a rep with more orders
    # than we scan would silently report the cap as their total.
    if truncated:
        msg += (
            f"\n\n⚠️ _Only the most recent {data.get('max_orders_scanned')} orders "
            f"were scanned — this is a minimum. Narrow the date range for an exact count._"
        )

    return _respond(msg, metadata={
        "total_orders": total_orders,
        "total_items": total_items,
        "truncated": truncated,
    })


def handle_rep_choice_reply(
    message, conversation, user_context, page, start_time, customer_id=None,
):
    """Admin picked one of several reps matching a name — re-run the report.

    Matches the reply against the parked candidates by EMAIL first (unique),
    then by exact label, then by a unique partial. An unmatched or ambiguous
    reply re-asks rather than guessing: the whole point of this step is that
    the name alone was not enough to identify the person.
    """
    pending = (user_context or {}).get("pending_rep_choice") or {}
    candidates = pending.get("candidates") or []

    def _respond(msg, suggestions=None, metadata=None):
        elapsed = round((time.time() - start_time) * 1000)
        meta = {"response_time_ms": elapsed}
        meta.update(metadata or {})
        return jsonify({
            "success": True,
            "bot_message": msg,
            "intent": "order_stats_by_rep",
            "products": [],
            "suggestions": suggestions or ["Browse Products"],
            "session_id": str(conversation.id),
            "metadata": meta,
            "pagination": default_pagination(page),
        }), 200

    if not candidates:
        # Context lost (expired session, partial write). Ask again rather than
        # reporting on an arbitrary rep.
        conversation.flow_state = FlowState.IDLE.value
        user_context.pop("pending_rep_choice", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _respond(
            "I lost track of which reps you were choosing between. "
            "Could you ask again with the rep's name?"
        )

    reply = (message or "").strip().lower()

    picked = None
    # 1. email appears in the reply (the suggestion label embeds it)
    for c in candidates:
        email = (c.get("email") or "").lower()
        if email and email in reply:
            picked = c
            break
    # 2. exact display name
    if not picked:
        exact = [c for c in candidates if (c.get("label") or "").lower() == reply]
        if len(exact) == 1:
            picked = exact[0]
    # 3. unique partial — only when it identifies ONE candidate
    if not picked and reply:
        partial = [c for c in candidates if reply in (c.get("label") or "").lower()]
        if len(partial) == 1:
            picked = partial[0]

    if not picked:
        options = [
            f"{c.get('label')} — {c.get('email')}" for c in candidates
        ]
        return _respond(
            "I couldn't tell which rep you meant. Please pick one of these:",
            suggestions=options[:8] + ["Cancel"],
            metadata={"flow_state": FlowState.AWAITING_REP_CHOICE.value,
                      "candidates": options},
        )

    # Resolved — clear the flow and re-run using the unambiguous EMAIL.
    conversation.flow_state = FlowState.IDLE.value
    user_context.pop("pending_rep_choice", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    class _E:
        pass
    entities = _E()
    entities.target_rep_name = picked.get("email")
    entities.date_after  = pending.get("date_after")
    entities.date_before = pending.get("date_before")

    logger.info(
        f"rep_choice | resolved to {picked.get('email')} "
        f"({picked.get('label')}) — re-running report"
    )

    role = user_context.get("role") or user_context.get("user_role")
    # customer_id comes from the request, not context — context may not carry it.
    _cid = customer_id or user_context.get("customer_id")
    return handle_order_stats(
        entities, role, _cid, conversation, page, start_time,
    )