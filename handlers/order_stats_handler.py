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
from handlers.chat_utils import default_pagination, format_order_for_frontend

# Order rows per page in list mode ("show me orders by <rep>"). Matches
# ADMIN_ORDER_PER_PAGE in api_builder — the same kind of admin report, so the
# same page size — but kept as its own constant to avoid importing api_builder
# into a handler. A year-long window for a busy rep is hundreds of orders;
# without this the whole set was serialised into one chat payload.
ORDER_LIST_PER_PAGE = 50

logger = get_logger("miraq_chat")


# Sentinel prefix for a structured pick from the date-range card. Mirrors the
# __BULK_ADDR__ convention: the payload is JSON, so it must never be routed
# through the NLP path that would try to read it as a sentence.
DATE_RANGE_SENTINEL = "__DATE_RANGE__"

# How many unparseable replies to absorb before giving up on the picker. Two,
# then a hard reset to IDLE — re-prompting forever is how a stuck user ends up
# unable to do anything else in the widget.
_MAX_DATE_RANGE_ATTEMPTS = 2


def _describe_range(date_after, date_before) -> str:
    """Human phrase for the window, or '' when unbounded."""
    if date_after and date_before:
        return f"{date_after[:10]} to {date_before[:10]}"
    if date_after:
        return f"since {date_after[:10]}"
    if date_before:
        return f"up to {date_before[:10]}"
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


def _join_names(names) -> str:
    """'a', 'a and b', 'a, b and c' — for reading, not for the API."""
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _rep_breakdown(data) -> str:
    """Per-rep counts, plus the overlap note when the arithmetic needs one.

    The per-rep numbers deliberately do NOT sum to the combined total when an
    order is credited to one rep and placed by another: it belongs to both,
    and each would say so. Zeroing it out of one to make the columns add up
    would be the worse error, so the discrepancy is explained rather than
    hidden.
    """
    reps = data.get("reps") or []
    if len(reps) < 2:
        return ""

    lines = []
    for r in reps:
        _n = r.get("rep_name") or r.get("rep_email") or "Unknown"
        _o = int(r.get("order_count") or 0)
        _i = int(r.get("item_count") or 0)
        lines.append(
            f"- **{_n}** — {_o} order{'s' if _o != 1 else ''}, "
            f"{_i} sample{'s' if _i != 1 else ''}"
        )

    out = "\n" + "\n".join(lines)

    overlap = int(data.get("overlap_orders") or 0)
    if overlap:
        out += (
            f"\n\n_{overlap} order{'s' if overlap != 1 else ''} "
            f"{'are' if overlap != 1 else 'is'} credited to one rep and placed "
            f"by another, so {'they appear' if overlap != 1 else 'it appears'} "
            f"under both above but count{'' if overlap != 1 else 's'} once in "
            f"the total._"
        )
    return out


def _unresolved_note(data) -> str:
    """Name every rep that could not be looked up.

    A partial report that doesn't say what it left out is worse than a
    failure: the admin reads the total as covering everyone they asked for.
    """
    missing = data.get("unresolved_reps") or []
    if not missing:
        return ""

    parts = []
    for m in missing:
        _name = m.get("requested") or "that name"
        if m.get("reason") == "ambiguous_rep":
            _cands = [
                c.get("label") or c.get("email") or ""
                for c in (m.get("matches") or [])
            ]
            _cands = [c for c in _cands if c]
            _hint = f" (matches {_join_names(_cands[:4])})" if _cands else ""
            parts.append(f"**{_name}** matches more than one rep{_hint}")
        else:
            parts.append(f"**{_name}** didn't match any sales rep")

    return (
        "\n\n⚠️ _Not included: "
        + "; ".join(parts)
        + ". Retype "
        + ("those names" if len(parts) > 1 else "that name")
        + " to add "
        + ("them" if len(parts) > 1 else "it")
        + "._"
    )


def _park_date_range_prompt(conversation, user_context, rep, role, attempts=0,
                            kind="stats", mode=None, scope=None, reps=None):
    """Park the report and ask for a window. Returns the action payload.

    `kind` records WHICH report is waiting on the window — "stats" for the
    aggregate rep count, "order_list" for the admin's all-orders list. Both
    park in the same flow state and reuse the same picker, so without the
    discriminator the resume path would run the stats report for a user who
    asked to see the order list.

    `mode` ("count"/"list") and `scope` ("self"/"person"/"all") ride along
    the same way `kind` does, for the same reason: the date picker is a
    detour through a different flow state, and anything not parked here is
    lost by the time the admin answers it — see handle_date_range_reply and
    handle_rep_choice_reply, which restore both on resume.

    `reps` is the FULL list of named reps. Parking only `rep` would turn a
    three-rep question into a one-rep answer the moment the admin picked a
    window — the same silent narrowing the name extraction used to do, just
    one step later.

    The token is the defence against a replayed card: /history re-renders
    stored actions verbatim, so a picker from a finished conversation comes
    back live on reload. A submission carrying a token that no longer matches
    the parked one is refused rather than silently starting a new report.
    """
    token = uuid.uuid4().hex
    _clear_stats_pending(user_context)
    user_context["pending_order_stats"] = {
        "rep": rep,
        "reps": list(reps) if reps else ([rep] if rep else []),
        "role": role,
        "token": token,
        "attempts": attempts,
        "kind": kind,
        "mode": mode,
        "scope": scope,
    }
    conversation.flow_state = FlowState.AWAITING_DATE_RANGE.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    return {
        "type": "SHOW_DATE_RANGE_PICKER",
        "payload": {
            "token": token,
            # The card labels itself with who the report is for. Sending only
            # the first name told an admin who asked about three reps that
            # the pending report was about one — the answer would have been
            # right and the card wrong, which is the harder kind to notice.
            # Existing single-rep behaviour is unchanged: one name in, the
            # same one name out.
            "rep_name": _join_names(reps) if reps else (rep or None),
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

    def _respond(message, suggestions=None, metadata=None, actions=None,
                 orders=None, order_pagination=None, flow_state=None):
        elapsed = round((time.time() - start_time) * 1000)
        meta = {"response_time_ms": elapsed}
        meta.update(metadata or {})
        payload = {
            "success": True,
            "bot_message": message,
            "intent": "order_stats_by_rep",
            "products": [],
            "suggestions": suggestions or ["Browse Products", "View my orders"],
            "session_id": str(conversation.id),
            "metadata": meta,
            "pagination": default_pagination(page),
            "actions": actions or [],
        }
        # List mode only: same fields _build_final_response attaches for the
        # self/all order-history path (§8 — identical serialization, so the
        # frontend needs no changes to render either).
        if orders is not None:
            payload["orders"] = orders
            payload["order_pagination"] = order_pagination or default_pagination(page)
        if flow_state is not None:
            payload["flow_state"] = flow_state
        return jsonify(payload), 200

    # ── Access: administrators only ─────────────────────────────────────────
    # Refuse explicitly. An unauthorized user must not get a zeroed report
    # that reads like "nobody ordered anything".
    if not is_order_report_admin(role):
        logger.info(f"order_stats | refused for role={role!r}")
        return _respond(
            "Order reporting is only available to administrators."
        )

    requested_rep = getattr(entities, "target_rep_name", None)
    # Every rep named. target_rep_name is the first of these; both are set
    # from one extraction so they cannot disagree.
    requested_reps = list(getattr(entities, "target_rep_names", None) or [])
    if not requested_reps and requested_rep:
        requested_reps = [requested_rep]

    # "list" only means anything when a rep is named — the no-rep branch is
    # a SQL GROUP BY with no order objects behind it (see get_order_stats_by_rep
    # in the plugin), so there is nothing to list. Falling back to "count"
    # here is a safety net, not the expected path: OrderStatsEvaluator only
    # sets mode="list" together with target_rep_name in the first place.
    mode = getattr(entities, "mode", None) or "count"
    if mode == "list" and not requested_rep:
        mode = "count"

    date_after  = getattr(entities, "date_after", None)
    date_before = getattr(entities, "date_before", None)

    # ── No window named: ask, do not assume ─────────────────────────────────
    # `date_range_resolved` is what separates "the user chose all time" from
    # "the user said nothing" — both leave the bounds None, and only the second
    # is a question. Without the flag an all-time pick re-prompts forever.
    if not date_after and not date_before and not getattr(entities, "date_range_resolved", False):
        action = _park_date_range_prompt(
            conversation, user_context, requested_rep, role, mode=mode,
            reps=requested_reps,
        )
        who = f" for **{_join_names(requested_reps)}**" if requested_reps else ""
        logger.info(
            f"order_stats | no date range given — prompting | "
            f"reps={requested_reps!r} mode={mode!r}"
        )
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
        # Every rep named in the query. target_rep_name is the first of these,
        # kept for the paths that predate the list; sending the list is what
        # makes "orders for cs_rep 1, cs_rep 2, cs_rep 3" cover all three
        # rather than silently reporting on the first.
        rep=(getattr(entities, "target_rep_names", None) or requested_rep or None),
        statuses=list(ORDER_REPORT_STATUSES),
        # Only meaningful with a rep named — see the mode fallback above.
        # The plugin returns order rows alongside the totals when set, using
        # the SAME merged (credited + self-placed) query as the count, so
        # "how many did Jennifer order" and "show me orders by Jennifer"
        # never disagree about which orders are hers.
        include_orders=(mode == "list"),
        page=page,
        per_page=ORDER_LIST_PER_PAGE if mode == "list" else None,
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
            # Same reasoning as date_resolved: without parking mode too, a
            # list-mode request that hits the rep-disambiguation step loses
            # "list" and comes back as a count once a rep is picked.
            "mode": mode,
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
    # Prefer the labels the plugin resolved — they are the real names, while
    # the request may have carried logins or partial spellings. Falls back to
    # what was asked for when the plugin is an older build with no `reps`.
    _resolved_labels = [
        r.get("rep_name") or r.get("rep_email")
        for r in (data.get("reps") or [])
        if (r.get("rep_name") or r.get("rep_email"))
    ]
    if _resolved_labels:
        name = _join_names(_resolved_labels)
    else:
        name = data.get("rep_filter_label") or _join_names(requested_reps) or "That rep"
    window      = _describe_range(data.get("date_after"), data.get("date_before"))
    window_str  = f" ({window})" if window else " (all time)"

    if total_orders == 0:
        # Still name what was left out — otherwise "no orders" reads as a
        # settled answer about everyone asked for, when it may only be an
        # answer about the names that resolved.
        _none = f"**{name}** has no orders{window_str}." if name else \
                f"No orders found{window_str}."
        if len(_resolved_labels) > 1:
            _none = f"No orders for {name}{window_str}."
        return _respond(
            _none + _unresolved_note(data),
            metadata={"total_orders": 0},
        )

    # ── List mode: order cards, not a count ─────────────────────────────────
    if mode == "list":
        raw_orders = data.get("orders") or []
        orders_list = [format_order_for_frontend(o) for o in raw_orders]
        # Paging metadata comes from the plugin, which knows the full match
        # count; falling back to a single page keeps an older plugin (one that
        # returns rows but no page keys) rendering correctly instead of
        # claiming a page 2 that does not exist.
        _page       = int(data.get("page") or page or 1)
        _per_page   = int(data.get("per_page") or ORDER_LIST_PER_PAGE)
        _total_pages = int(data.get("total_pages") or 1)
        msg = (
            f"**{name}** — **{total_orders} order{'s' if total_orders != 1 else ''}**"
            f"{window_str}."
        )
        # Per-rep split before the paging line: an admin who asked about three
        # reps wants the split first, and the combined list below is what the
        # page number refers to.
        msg += _rep_breakdown(data)
        if _total_pages > 1:
            msg += f"\n\nShowing page {_page} of {_total_pages}."
        # Say WHICH question this answers. These are orders credited to the
        # rep plus orders they placed themselves — so the billing name on a
        # card is often someone else entirely (bulk orders bill the rep's
        # customer and ship to a third party). Without this line an admin
        # sees "Jennifer — 1 order" over a card that names a different
        # person and reasonably concludes the filter is broken.
        msg += (
            "\n\n_Includes orders credited to them as sales rep and orders "
            "they placed themselves — so the billing name on an order may "
            "differ._"
        )
        if truncated:
            msg += (
                f"\n\n⚠️ _Only the most recent {data.get('max_orders_scanned')} orders "
                f"were scanned — this list is a minimum. Narrow the date range to see all of them._"
            )
        msg += _unresolved_note(data)
        return _respond(
            msg,
            metadata={
                "total_orders": total_orders,
                "total_items": total_items,
                "truncated": truncated,
                "reps": data.get("reps") or [],
                "unresolved_reps": data.get("unresolved_reps") or [],
                "overlap_orders": int(data.get("overlap_orders") or 0),
                "allow_order_download": is_order_report_admin(role),
            },
            orders=orders_list,
            order_pagination={
                "page": _page,
                "per_page": _per_page,
                "total_items": total_orders,
                "total_pages": _total_pages,
                "has_more": _page < _total_pages,
            },
            # Matches requirement 2's order-history response: tapping a card
            # re-enters as "show me order #N" through the normal pipeline,
            # not AWAITING_ORDER_DETAIL (that flow_state belongs to
            # handle_order_status's own multi-match picker, a different flow).
            flow_state=FlowState.IDLE.value,
        )

    msg = (
        f"**{name}** — **{total_orders} order{'s' if total_orders != 1 else ''}**, "
        f"**{total_items} sample{'s' if total_items != 1 else ''}**{window_str}."
    )
    msg += _rep_breakdown(data)
    # Only surfaced when it actually fires: without it a rep with more orders
    # than we scan would silently report the cap as their total.
    if truncated:
        msg += (
            f"\n\n⚠️ _Only the most recent {data.get('max_orders_scanned')} orders "
            f"were scanned — this is a minimum. Narrow the date range for an exact count._"
        )
    msg += _unresolved_note(data)

    return _respond(msg, metadata={
        "total_orders": total_orders,
        "total_items": total_items,
        "truncated": truncated,
        "reps": data.get("reps") or [],
        "unresolved_reps": data.get("unresolved_reps") or [],
        "overlap_orders": int(data.get("overlap_orders") or 0),
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
    entities.target_rep_names = [picked.get("email")] if picked.get("email") else []
    entities.date_after  = pending.get("date_after")
    entities.date_before = pending.get("date_before")
    # Default True: anything parked by an older build predates this key, and
    # for those the window HAD been settled before the rep prompt appeared.
    entities.date_range_resolved = pending.get("date_resolved", True)
    # Same failure the date_resolved key was added to prevent: without this,
    # "show orders by Jennifer" -> "2 reps match?" -> pick -> counts come
    # back instead of the order cards that were actually asked for.
    entities.mode = pending.get("mode") or "count"

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


def prompt_for_order_list_range(conversation, user_context, role, start_time, page=1, scope=None):
    """Ask which period the admin's all-orders list should cover.

    Separate entry point from handle_order_stats' own prompt so the order-list
    flow can park without pretending to be a stats query, but the card, the
    flow state, and the reply handler are shared.

    `scope` ("self"/"all"/None) is the admin's original wording ("my orders"
    vs "all orders") — parked here so the resume path in routes/chat.py can
    restore it after the date picker's message-rewrite would otherwise
    silently collapse it to "all" (see the comment at that rewrite site).
    """
    action = _park_date_range_prompt(
        conversation, user_context, None, role, kind="order_list", scope=scope,
    )
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Which period should the order list cover?",
        "intent": "order_history",
        "products": [],
        "orders": [],
        "suggestions": ["This week", "This month", "This quarter", "This year", "All time", "Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_DATE_RANGE.value,
            "response_time_ms": elapsed,
        },
        "pagination": default_pagination(page),
        "actions": [action],
        "flow_state": FlowState.AWAITING_DATE_RANGE.value,
    }), 200


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
    reps = pending.get("reps") or ([rep] if rep else [])
    role = pending.get("role") or (user_context or {}).get("role") or (user_context or {}).get("user_role")
    kind = pending.get("kind", "stats")
    _reset_idle()

    if kind == "order_list":
        # Hand the resolved window back to the caller rather than running the
        # report here: the order list is answered through the normal API-call
        # pipeline (build -> execute -> handle_order_status), which lives in
        # routes/chat.py. Re-running it from inside this module would mean
        # duplicating that pipeline.
        return {
            "resume": "order_list",
            "date_after": date_after,
            "date_before": date_before,
            "role": role,
            "scope": pending.get("scope"),
        }

    class _E:
        pass
    entities = _E()
    entities.target_rep_name    = rep
    # Restored for the same reason kind/mode/date_resolved are: this resume
    # rebuilds entities from scratch, so anything not read back from
    # `pending` here is silently lost — and losing this one turns a three-rep
    # question into a one-rep answer.
    entities.target_rep_names   = list(reps)
    entities.date_after         = date_after
    entities.date_before        = date_before
    entities.date_range_resolved = True
    # Same reason kind/date_resolved are parked: this resume rebuilds
    # entities from scratch, so anything not read from `pending` here is
    # silently lost — see handle_rep_choice_reply for the matching fix.
    entities.mode = pending.get("mode") or "count"

    logger.info(
        f"date_range | resolved reps={reps!r} "
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
        kind=pending.get("kind", "stats"),
        mode=pending.get("mode"), scope=pending.get("scope"),
        # Re-parking rebuilds the pending record from scratch, so the rep
        # list has to be carried explicitly here too — otherwise one
        # unreadable reply ("last quater") narrows a three-rep question to
        # one rep, and the report that eventually runs answers something the
        # admin never asked.
        reps=pending.get("reps"),
    )
    return _respond(
        f"{reason} Pick a period below, or type one like *\"last quarter\"* "
        "or *\"01/02/2026 to 03/15/2026\"*.",
        suggestions=["This week", "This month", "This quarter", "This year", "All time", "Cancel"],
        metadata={"flow_state": FlowState.AWAITING_DATE_RANGE.value},
        actions=[action],
    )