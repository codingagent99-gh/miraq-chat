"""
handlers/sales_rep_handler.py — Sales rep conversation flow handlers.

Feature 1: Order-for flow (order on behalf of a customer, by NAME + COMPANY).
  handle_order_for_prompt()        — entry point, sets AWAITING_ORDER_FOR_CUSTOMER
  handle_order_for_email_reply()   — resolves customer by name + company
  _resolve_order_for_customer()    — private: pins the target, resumes the flow
  _fetch_and_show_last_5_orders()  — private: fetches orders + reorder hints

Customers are identified by COMPANY + NAME, matching how the bulk flow and the
rest of the product resolve people. The email path was removed outright rather
than kept as a fallback: two routes to the same customer can disagree about
which record wins, and company identity is the one the data is organised
around.

The flow-state name and its persisted VALUE were both changed to match —
"awaiting_order_for_customer" — since nothing about this flow uses email
anymore. Sessions that already have the old string "awaiting_order_for_email"
sitting in a live conversation row are not orphaned: routes/chat.py aliases
that string onto this enum member at the point a request is parsed, so a
rep mid-flow when this shipped continues normally rather than landing back
at the top level. New conversations never write the old string.
"""

import re
import time

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from utils.reorder_analysis import analyse_reorder_patterns
from app_config import BULK_ORDER_ROLES

logger = get_logger("miraq_chat")

# Chip label for "place this on my own account" at the order-for prompt.
# Matched case-insensitively in handle_order_for_email_reply BEFORE the
# name+company parse, since it isn't one. Keep it distinct from anything a
# rep might type as a real customer name.
ORDER_FOR_SELF_CHIP = "For myself"

# How many contact names are offered as chips when a company has to be
# narrowed down. Matches the bulk flow's recipient picker so the two prompts
# behave the same way.
_ORDER_FOR_CHIP_LIMIT = 8

# "Kiki at Gensler" / "Kiki, Gensler" / "Kiki from Gensler" / "Kiki - Gensler".
# Company identity is what resolves a customer now, not their email address —
# the same rule the bulk flow follows. "at" is listed first so it wins inside
# a company name that itself contains a comma ("Kiki at Beck, Inc").
_NAME_COMPANY_RE = re.compile(
    r'^\s*(?P<name>.+?)\s*(?:\bat\b|\bfrom\b|,|\s-\s)\s*(?P<company>.+?)\s*$',
    re.I,
)


def _company_roster(company: str, requesting_customer_id, max_pages: int = 5) -> list:
    """Every customer filed under a company name.

    Pages until a short page comes back: the plugin caps per_page at 20, so a
    single call silently truncates any company with more contacts — which used
    to report a real person as "not found" purely for sitting 21st.
    """
    rows: list = []
    for _page in range(1, max_pages + 1):
        call = endpoints.search_customers_by_company(
            company_name=company,
            per_page=20,
            page=_page,
            requesting_customer_id=requesting_customer_id,
            description=f"Order-for company lookup: '{company}' p{_page}",
        )
        result = woo_client.execute(call)
        if not result.get("success"):
            break
        data = result.get("data")
        page_rows = data if isinstance(data, list) else (data or {}).get("data", [])
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 20:
            break
    return rows


def _display_name(customer: dict) -> str:
    full = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    return full or customer.get("email") or f"Customer #{customer.get('id')}"


def _match_by_name(name: str, roster: list) -> list:
    """Roster entries matching a person's name. Never guesses.

    Returns every match, so the caller can tell "one" from "several" — two
    people at one company can share a name, and picking the first would place
    the order against the wrong account with nothing to show it happened.
    """
    needle = re.sub(r'[^a-z0-9]+', ' ', (name or "").lower()).strip()
    if not needle or not roster:
        return []
    out = []
    for c in roster:
        first = str(c.get("first_name", "") or "").lower()
        last = str(c.get("last_name", "") or "").lower()
        full = f"{first} {last}".strip()
        email_local = str(c.get("email", "") or "").split("@")[0].lower()
        hays = {h for h in (first, last, full, email_local) if h}
        if any(needle == h for h in hays) or any(needle in h.split() for h in hays):
            out.append(c)
    return out


# ══════════════════════════════════════════════════════════════
# ── Function 1: handle_order_for_prompt ──
# ══════════════════════════════════════════════════════════════

def handle_order_for_prompt(conversation, page, start_time):
    """
    Entry point for the order-for flow.
    Sets flow state to AWAITING_ORDER_FOR_CUSTOMER and asks who the order is for.
    Returns a Flask response.

    "For myself" is offered alongside the name answer: a rep placing an order
    on their own account previously had no way through this prompt except to
    cancel, because Cancel was the only other suggestion.
    """
    conversation.flow_state = FlowState.AWAITING_ORDER_FOR_CUSTOMER.value

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success":     True,
        "bot_message": (
            "Who would you like to place this order for?\n\n"
            "Give me the customer's **name** and **company** "
            "(e.g. *Kiki at Gensler*), or choose "
            f"**{ORDER_FOR_SELF_CHIP}** to order on your own account."
        ),
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": [ORDER_FOR_SELF_CHIP, "Cancel"],
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":       FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
            "response_time_ms": elapsed,
        },
        "flow_state":  FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
        "pagination":  default_pagination(page),
    }), 200


def _resolve_order_for_customer(customer, user_context, conversation, page, start_time):
    """Pin the order-for target and hand control back to the ordinary flow.

    One place for both the unique-name match and a pick from the chips, so
    the two cannot drift — whichever route got here, the same keys are set
    and the same Step 8.5 gate in routes/chat.py stops re-asking.

    Delegates to _fetch_and_show_last_5_orders, which pins the target AND
    shows the customer's recent orders with reorder hints. That listing used
    to be reachable only through the removed email lookup; routing name+company
    resolution through the same function keeps the feature rather than
    quietly dropping it along with the email path.
    """
    for _k in ("order_for_candidates", "order_for_pending_name",
               "order_for_pending_company"):
        user_context.pop(_k, None)
    user_context["order_for_is_self"] = False

    logger.info(
        f"order_for | resolved to {_display_name(customer)} "
        f"(id={customer.get('id')})"
    )
    return _fetch_and_show_last_5_orders(
        customer, conversation, user_context, page, start_time
    )


# ══════════════════════════════════════════════════════════════
# ── Function 2: handle_order_for_email_reply ──
# ══════════════════════════════════════════════════════════════

def handle_order_for_email_reply(message, conversation, user_context, page, start_time,
                                 customer_id=None):
    """
    Handles the rep's reply during AWAITING_ORDER_FOR_CUSTOMER.
    Resolves the customer from a NAME + COMPANY, or places the order on the
    caller's own account when they choose ORDER_FOR_SELF_CHIP.

    `customer_id` is the LOGGED-IN user's id, passed explicitly by the caller.
    It is deliberately not read from user_context: that dict is the persisted
    conversation context and never carries customer_id — the id arrives on
    each request and is threaded through as its own argument everywhere else
    in this module. Reading it from user_context returned None and made "For
    myself" claim it couldn't tell who was signed in.
    """
    import re

    # ── "For myself" — resolve to the logged-in user, no lookup ─────────────
    # Checked before the name parse, since this reply deliberately isn't a name.
    # Sets order_for_customer_id to the rep's own id so the Step 8.5 gate in
    # routes/chat.py stops re-asking, and the order proceeds through the
    # ordinary single-product path against their own account.
    if (message or "").strip().lower() == ORDER_FOR_SELF_CHIP.lower():
        _self_id = customer_id or user_context.get("customer_id")
        if not _self_id:
            # No account behind this session — asking again would loop, so
            # say what's wrong instead.
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success":     True,
                "bot_message": (
                    "I can't tell which account you're signed in as, so I "
                    "can't place this on your own account. Tell me the "
                    "customer's **name** and **company** instead "
                    "(e.g. *Kiki at Gensler*)."
                ),
                "intent":      "guided_flow",
                "products":    [],
                "suggestions": ["Cancel"],
                "session_id":  str(conversation.id),
                "metadata": {
                    "flow_state":       FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                    "response_time_ms": elapsed,
                },
                "flow_state":  FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                "pagination":  default_pagination(page),
            }), 200

        user_context["order_for_customer_id"]  = _self_id
        user_context["order_for_display_name"] = "you"
        user_context["order_for_is_self"]      = True
        user_context.pop("order_for_candidates", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        conversation.flow_state = FlowState.IDLE.value

        logger.info(
            f"order_for | self-order chosen | customer_id={_self_id}"
        )

        _pending = user_context.get("pending_product_name")
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": (
                f"Got it — ordering on your own account. "
                + (
                    f"Let's continue with **{_pending}**."
                    if _pending else "What would you like to order?"
                )
            ),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": (
                [f"Order {_pending}"] if _pending else ["Browse Products"]
            ) + ["Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":            FlowState.IDLE.value,
                "order_for_customer_id": _self_id,
                "order_for_is_self":     True,
                "response_time_ms":      elapsed,
            },
            "flow_state":  FlowState.IDLE.value,
            "pagination":  default_pagination(page),
        }), 200

    # ── Candidate pick from a previous ambiguous prompt ─────────────────────
    # Checked before parsing: the reply here is a bare NAME chosen from chips,
    # which would otherwise be re-parsed as a fresh "name and company" and
    # come back asking for a company the rep already gave.
    _pending = user_context.get("order_for_candidates") or []
    if _pending:
        _picked = next(
            (c for c in _pending
             if _display_name(c).strip().lower() == (message or "").strip().lower()),
            None,
        )
        if _picked:
            return _resolve_order_for_customer(
                _picked, user_context, conversation, page, start_time
            )
        # Not one of the chips — fall through and treat it as a new answer
        # rather than rejecting it, so the rep can correct a wrong company
        # without cancelling.
        user_context.pop("order_for_candidates", None)

    # ── Name + company — the ONLY way to identify a customer here ──────────
    # Email lookup was removed deliberately: company identity is what the rest
    # of the product resolves on, and keeping an email path alive meant two
    # ways to reach the same customer that could disagree about which record
    # won. Anything that isn't "For myself" or a chip pick is read as a name
    # and company.
    _raw = (message or "").strip()
    if _raw:
        _m = _NAME_COMPANY_RE.match(_raw)
        _pending_company = user_context.get("order_for_pending_company") or ""
        _pending_name = user_context.get("order_for_pending_name") or ""

        if _m:
            _name, _company = _m.group("name").strip(), _m.group("company").strip()
        elif _pending_company:
            # Rep supplied the company last turn; this is the name.
            _name, _company = _raw, _pending_company
        elif _pending_name:
            # Rep supplied the name last turn; this is the company.
            _name, _company = _pending_name, _raw
        else:
            # One bare token and nothing parked — ask for the missing half
            # rather than guessing which one they gave.
            user_context["order_for_pending_name"] = _raw
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success":     True,
                "bot_message": (
                    f"Which company is **{_raw}** at?"
                ),
                "intent":      "guided_flow",
                "products":    [],
                "suggestions": [ORDER_FOR_SELF_CHIP, "Cancel"],
                "session_id":  str(conversation.id),
                "metadata": {
                    "flow_state":       FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                    "response_time_ms": elapsed,
                },
                "flow_state":  FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                "pagination":  default_pagination(page),
            }), 200

        roster = _company_roster(_company, customer_id)
        logger.info(
            f"order_for | company '{_company}' → {len(roster)} customer(s) | "
            f"name='{_name}'"
        )

        if not roster:
            user_context.pop("order_for_pending_name", None)
            user_context.pop("order_for_pending_company", None)
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success":     True,
                "bot_message": (
                    f"I couldn't find any customers at **{_company}**. "
                    "Check the company name and try again "
                    "(e.g. *Kiki at Gensler*)."
                ),
                "intent":      "guided_flow",
                "products":    [],
                "suggestions": [ORDER_FOR_SELF_CHIP, "Cancel"],
                "session_id":  str(conversation.id),
                "metadata": {
                    "flow_state":       FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                    "response_time_ms": elapsed,
                },
                "flow_state":  FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                "pagination":  default_pagination(page),
            }), 200

        matches = _match_by_name(_name, roster)
        if len(matches) == 1:
            user_context.pop("order_for_pending_name", None)
            user_context.pop("order_for_pending_company", None)
            return _resolve_order_for_customer(
                matches[0], user_context, conversation, page, start_time
            )

        # Zero or several — offer the roster rather than guessing. Several
        # means two people genuinely share that name at this company.
        _choices = matches if len(matches) > 1 else roster
        user_context["order_for_candidates"] = _choices
        user_context["order_for_pending_company"] = _company
        user_context.pop("order_for_pending_name", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        _names = [_display_name(c) for c in _choices]
        if len(matches) > 1:
            _msg = (
                f"There are {len(matches)} people called **{_name}** at "
                f"**{_company}**. Which one?"
            )
        else:
            _shown = min(len(_names), _ORDER_FOR_CHIP_LIMIT)
            _msg = (
                f"I couldn't match **{_name}** at **{_company}**. "
                f"{_company} has {len(_names)} contact(s)"
                + (f" — showing the first {_shown}." if len(_names) > _shown else ".")
                + " Who should this go to?"
            )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": _msg,
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": _names[:_ORDER_FOR_CHIP_LIMIT] + ["Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
                "candidates":       _names,
                "company":          _company,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_ORDER_FOR_CUSTOMER.value,
            "pagination":  default_pagination(page),
        }), 200



# ══════════════════════════════════════════════════════════════
# ── Function 3: _fetch_and_show_last_5_orders (private) ──
# ══════════════════════════════════════════════════════════════

def _fetch_and_show_last_5_orders(customer_data, conversation, user_context, page, start_time):
    """
    Fetches the last 5 orders for the resolved customer via the rep endpoint,
    annotates them with reorder hints, and returns a Flask response.
    """
    customer_id_target = str(customer_data["id"])
    display_name = (
        customer_data.get("display")
        or customer_data.get("billing", {}).get("company")
        or f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()
        or customer_data.get("email", "")
        or f"Customer #{customer_id_target}"
    )

    call   = endpoints.list_rep_orders(
        body={"customer_id": customer_id_target, "per_page": 5},
        description=f"Fetch last 5 orders for {display_name}",
    )
    result = woo_client.execute(call)

    orders = []
    if result.get("success"):
        data = result.get("data", [])
        if isinstance(data, list):
            orders = data
        elif isinstance(data, dict):
            orders = data.get("orders", data.get("data", []))

    patterns = analyse_reorder_patterns(orders)

    user_context["order_for_customer_id"]  = customer_id_target
    user_context["order_for_display_name"] = display_name
    user_context.pop("order_for_candidates", None)

    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value

    if not orders:
        bot_message = (
            f"I couldn't find any recent orders for **{display_name}**. "
            "You can place a new order for them now."
        )
        suggestions = ["Place new order", "Cancel"]
    else:
        bot_message = f"Here are the last {len(orders)} orders for **{display_name}**:\n\n"
        for order in orders:
            order_num  = order.get("number") or order.get("id")
            order_date = (order.get("date_created") or "")[:10]
            line_items = order.get("line_items", [])
            items_text = ", ".join(
                f"{item.get('name', '?')} ×{item.get('quantity', 1)}"
                for item in line_items[:3]
            )
            if len(line_items) > 3:
                items_text += f" +{len(line_items) - 3} more"
            bot_message += f"**#{order_num}** — {order_date}\n{items_text}\n"
            for item in line_items:
                pid     = item.get("product_id")
                pattern = patterns.get(pid)
                if pattern and pattern.overdue:
                    bot_message += f"  ⚠️ {pattern.hint}\n"
            bot_message += "\n"
        suggestions = ["Reorder last order", "Place new order"]

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success":     True,
        "bot_message": bot_message,
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": suggestions,
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":               FlowState.IDLE.value,
            "response_time_ms":         elapsed,
            "order_for_customer_id":    customer_id_target,
            "order_for_display_name":   display_name,
        },
        "flow_state":  FlowState.IDLE.value,
        "pagination":  default_pagination(page),
    }), 200