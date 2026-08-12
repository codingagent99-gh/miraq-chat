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
  * A question that names no period does NOT quietly become an all-time
    report. It parks in AWAITING_DATE_RANGE and asks. All-time is still
    available — it is on the picker as an explicit choice — but it has to be
    chosen, because a total covering a wider window than the asker imagined
    is a wrong answer wearing the costume of a right one.
"""

import json
import time
import uuid
from datetime import datetime

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from chat_logger import get_logger
from conversation_flow import FlowState
from app_config import ORDER_REPORT_STATUSES, is_order_report_admin
from handlers.chat_utils import default_pagination

logger = get_logger("miraq_chat")


# Sentinel prefix for a structured pick from the date-range card. Mirrors the
# __BULK_ADDR__ convention: the payload is JSON, so it must never be routed
# through the NLP path that would try to read it as a sentence.
DATE_RANGE_SENTINEL = "__DATE_RANGE__"

# How many unparseable replies to absorb before giving up on the picker. Two,
# then a hard reset to IDLE — re-prompting forever is how a stuck user ends up
# unable to do anything else in the widget.
_MAX_DATE_RANGE_ATTEMPTS = 2

def _fmt_date_mdy(iso_date: str) -> str:
    """'2026-01-02' -> '01/02/2026'. Falls back to the raw ISO slice if the
    string isn't a parseable calendar date (defensive — callers only ever
    pass the first 10 chars of an already-validated ISO timestamp)."""
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return iso_date[:10]

def _describe_range(date_after, date_before) -> str:
    """Human phrase for the window, or '' when unbounded."""
    if date_after and date_before:
        return f"{_fmt_date_mdy(date_after)} to {_fmt_date_mdy(date_before)}"
    if date_after:
        return f"since {_fmt_date_mdy(date_after)}"
    if date_before:
        return f"up to {_fmt_date_mdy(date_before)}"
    return ""


def _clear_stats_pending(user_context):
    """Drop both order-report parking slots.

    conversation.flow_state holds ONE value, so only one of these can be the
    live prompt. Leaving the other behind means a later turn can find stale
    candidates (or a stale picker token) and answer against a question the
    user has already moved on from.
    """
    user_context.pop("pending_rep_choice", None)
    user_context.pop("pending_order_stats", None)


def _park_date_range_prompt(conversation, user_context, rep, role, attempts=0):
    """Park the report and ask for a window. Returns the action payload.

    The token is the defence against a replayed card: /history re-renders
    stored actions verbatim, so a picker from a finished conversation comes
    back live on reload. A submission carrying a token that no longer matches
    the parked one is refused rather than silently starting a new report.
    """
    token = uuid.uuid4().hex
    _clear_stats_pending(user_context)
    user_context["pending_order_stats"] = {
        "rep": rep,
        "role": role,
        "token": token,
        "attempts": attempts,
    }
    conversation.flow_state = FlowState.AWAITING_DATE_RANGE.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    return {
        "type": "SHOW_DATE_RANGE_PICKER",
        "payload": {
            "token": token,
            "rep_name": rep or None,
            "quick_options": ["This week", "This month", "This quarter", "This year"],
        },
    }


def handle_order_stats(
    entities, role, customer_id, conversation, page, start_time,
    user_context=None,
):
    """Answer an order/sample-count question. Returns a Flask response.

    `user_context` is keyword-with-default on purpose: callers in the wild
    pass it and older ones do not, and a required positional would turn that
    into a TypeError on every reporting turn.
    """
    if user_context is None:
        user_context = conversation.context_data or {}

    def _respond(message, suggestions=None, metadata=None, actions=None):
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
            "actions": actions or [],
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

    # ── No window named: ask, do not assume ─────────────────────────────────
    # `date_range_resolved` is what separates "the user chose all time" from
    # "the user said nothing" — both leave the bounds None, and only the second
    # is a question. Without the flag an all-time pick re-prompts forever.
    if not date_after and not date_before and not getattr(entities, "date_range_resolved", False):
        action = _park_date_range_prompt(conversation, user_context, requested_rep, role)
        who = f" for **{requested_rep}**" if requested_rep else ""
        logger.info(f"order_stats | no date range given — prompting | rep={requested_rep!r}")
        return _respond(
            f"Which period should I cover{who}?",
            suggestions=["This week", "This month", "This quarter", "This year", "All time", "Cancel"],
            metadata={"flow_state": FlowState.AWAITING_DATE_RANGE.value},
            actions=[action],
        )

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
        _clear_stats_pending(user_context)
        user_context["pending_rep_choice"] = {
            "candidates":  [{"label": m.get("label"), "email": m.get("email")} for m in matches],
            "date_after":  date_after,
            "date_before": date_before,
            # The window was already settled to get this far. Recording that
            # explicitly means the re-run after the admin picks a rep cannot
            # fall back into the date prompt and discard the choice they just
            # made — which is exactly what happens if all-time (both bounds
            # None) is mistaken for an unanswered question.
            "date_resolved": True,
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
    # Default True: anything parked by an older build predates this key, and
    # for those the window HAD been settled before the rep prompt appeared.
    entities.date_range_resolved = pending.get("date_resolved", True)

    logger.info(
        f"rep_choice | resolved to {picked.get('email')} "
        f"({picked.get('label')}) — re-running report"
    )

    role = user_context.get("role") or user_context.get("user_role")
    # customer_id comes from the request, not context — context may not carry it.
    _cid = customer_id or user_context.get("customer_id")
    return handle_order_stats(
        entities, role, _cid, conversation, page, start_time,
        user_context=user_context,
    )


def handle_date_range_reply(
    message, conversation, user_context, page, start_time, customer_id=None,
):
    """The admin answered the "which period?" prompt — re-run the report.

    Two shapes arrive here:
      * "__DATE_RANGE__<json>" from the picker card — already unambiguous
        calendar dates, so they are used verbatim with no re-parsing. Sending
        them back through the NLP layer would reintroduce exactly the
        day/month ambiguity the picker exists to eliminate.
      * anything else the user typed ("last quarter", "01/02/2026 to
        03/15/2026"), which goes through the normal extractor so typing a
        window works as well as clicking one.
    """
    pending = (user_context or {}).get("pending_order_stats") or {}

    def _respond(msg, suggestions=None, metadata=None, actions=None):
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
            "actions": actions or [],
        }), 200

    def _reset_idle():
        conversation.flow_state = FlowState.IDLE.value
        _clear_stats_pending(user_context)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

    if not pending:
        # Context lost, or a card replayed from history after the flow closed.
        _reset_idle()
        return _respond(
            "That date picker has expired. Ask for the report again and I'll "
            "pull fresh figures."
        )

    raw = (message or "").strip()
    date_after = date_before = None
    resolved = False

    if raw.startswith(DATE_RANGE_SENTINEL):
        try:
            payload = json.loads(raw[len(DATE_RANGE_SENTINEL):] or "{}")
        except (ValueError, TypeError):
            payload = None

        if not isinstance(payload, dict):
            return _retry_date_range(
                conversation, user_context, pending, _respond,
                "I couldn't read that date selection.",
            )

        # Stale card guard. /history replays stored actions verbatim, so a
        # picker from a finished report renders again on reload; without this
        # a click on it would silently start a report nobody asked for.
        if payload.get("token") != pending.get("token"):
            _reset_idle()
            logger.info("date_range | token mismatch — stale picker refused")
            return _respond(
                "That date picker belongs to an earlier question. Ask for the "
                "report again and I'll pull fresh figures."
            )

        if payload.get("all_time"):
            resolved = True          # deliberate choice; bounds stay None
        else:
            after  = (payload.get("after")  or "").strip()
            before = (payload.get("before") or "").strip()
            # The card sends bare YYYY-MM-DD strings, never a serialised Date,
            # so no timezone shift has occurred. The day bounds are attached
            # here, matching what extract_time_range() produces.
            if _is_iso_date(after) and _is_iso_date(before):
                if after > before:
                    return _retry_date_range(
                        conversation, user_context, pending, _respond,
                        "That range starts after it ends.",
                    )
                date_after  = f"{after}T00:00:00"
                date_before = f"{before}T23:59:59.999999"
                resolved = True

        if not resolved:
            return _retry_date_range(
                conversation, user_context, pending, _respond,
                "I couldn't read that date selection.",
            )
    else:
        # Typed window — reuse the one extractor so phrases and explicit
        # ranges behave identically here and on the first turn.
        from models import ExtractedEntities
        from classifier.extractors import extract_time_range

        probe = ExtractedEntities()
        extract_time_range(raw.lower(), probe)
        if probe.date_after or probe.date_before:
            date_after  = probe.date_after
            date_before = probe.date_before
            resolved = True
        elif raw.lower().strip(" .!") in ("all time", "all-time", "alltime", "everything", "ever"):
            resolved = True
        else:
            return _retry_date_range(
                conversation, user_context, pending, _respond,
                "I couldn't work out a date range from that.",
            )

    # Resolved — clear the prompt and run the report.
    rep  = pending.get("rep")
    role = pending.get("role") or (user_context or {}).get("role") or (user_context or {}).get("user_role")
    _reset_idle()

    class _E:
        pass
    entities = _E()
    entities.target_rep_name    = rep
    entities.date_after         = date_after
    entities.date_before        = date_before
    entities.date_range_resolved = True

    logger.info(
        f"date_range | resolved rep={rep!r} "
        f"window={_describe_range(date_after, date_before) or 'all time'}"
    )

    _cid = customer_id or (user_context or {}).get("customer_id")
    return handle_order_stats(
        entities, role, _cid, conversation, page, start_time,
        user_context=user_context,
    )


def _is_iso_date(value) -> bool:
    """True for a bare YYYY-MM-DD string that is a real calendar date."""
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _retry_date_range(conversation, user_context, pending, _respond, reason):
    """Re-ask for a window, or give up after too many unreadable replies."""
    attempts = int(pending.get("attempts") or 0) + 1
    if attempts >= _MAX_DATE_RANGE_ATTEMPTS:
        conversation.flow_state = FlowState.IDLE.value
        _clear_stats_pending(user_context)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        logger.info("date_range | giving up after repeated unreadable replies")
        return _respond(
            f"{reason} Let's start over — ask for the report again and include "
            "a period, like *\"how many samples did Ram order last quarter\"*."
        )

    action = _park_date_range_prompt(
        conversation, user_context,
        pending.get("rep"), pending.get("role"), attempts=attempts,
    )
    return _respond(
        f"{reason} Pick a period below, or type one like *\"last quarter\"* "
        "or *\"01/02/2026 to 03/15/2026\"*.",
        suggestions=["This week", "This month", "This quarter", "This year", "All time", "Cancel"],
        metadata={"flow_state": FlowState.AWAITING_DATE_RANGE.value},
        actions=[action],
    )