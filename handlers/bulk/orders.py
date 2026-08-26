"""Confirmation table, cart fork and order creation for the bulk-order flow.

Split verbatim out of handlers/bulk_order_handler.py — pure move, no logic
changes. This module needed NO deferred imports: nothing in it calls back
into the handler, so every dependency below is a plain top-level import.
"""

import time

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from app_config import DEFAULT_PAYMENT_METHOD, DEFAULT_PAYMENT_METHOD_TITLE
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from utils.checkout_fields import (
    format_missing_fields,
    get_required_fields,
    has_errors,
    is_known_rep,
    validate_bulk_address,
)

# Earlier stages of this split. Import chain stays one-directional:
# handler -> orders -> addresses -> recipients -> variants, with common
# depending on nothing.
from handlers.bulk.common import _get, _BULK_STATE_KEYS
from handlers.bulk.variants import _variant_meta_entry
from handlers.bulk.recipients import _line_recipient_display
from handlers.bulk.addresses import (
    _address_group_key,
    _advance_to_next_address_confirmation,
    _effective_address_for_line,
)

logger = get_logger("miraq_chat")

# ══════════════════════════════════════════════════════════════
# ── Private: _build_bulk_confirmation_response ──
# ══════════════════════════════════════════════════════════════

def _line_product_is_live(line, store_loader):
    """True when a parked line's product_id still exists in the current catalog.

    Bulk lines are parked in conversation.context_data while the shopper is
    asked for variants and quantities, and that state outlives the catalog:
    StoreLoader reloads every 6 hours (_refresh_interval) while the
    conversation sits open. Nothing invalidated the parked product_id, so a
    line resolved before a reload was trusted verbatim after it.

    Before the ids became GID-derived, that was actively dangerous: ids were
    positional (idx + 1), so deleting one product shifted every later id down
    by one, and a parked line silently resolved to a DIFFERENT product. The
    add succeeded, the cart filled with the wrong items, and the confirmation
    named the products the shopper had asked for — because the label comes
    from the parked line, not from what was added. No error anywhere.

    Ids are permanent now, so the remaining case is the honest one: the
    product was deleted or unpublished mid-conversation and the id resolves
    to nothing. This also catches any state parked under the old positional
    scheme, since a small ordinal will not match a 13-digit Shopify id.

    Returns True when the loader is unavailable or the product is found —
    "unknown" must never block an add, only a positive miss does.
    """
    if not store_loader:
        return True

    products = getattr(store_loader, "products", None)
    if not products:
        return True

    pid = str(line.get("product_id"))
    for candidate in products:
        if (str(candidate.get("id", "")) == pid
                or str(candidate.get("_shopify_gid", "")) == pid):
            return True
    return False

# ══════════════════════════════════════════════════════════════
# ── Function 3: _format_bulk_confirmation_table (private) ──
# ══════════════════════════════════════════════════════════════

def _format_bulk_confirmation_table(lines) -> str:
    """
    Build a markdown table summarising all parsed bulk order lines.
    Accepts both BulkOrderLine dataclass instances and plain dicts.
    """
    rows = []
    for line in lines:
        customer = _line_recipient_display(line) if isinstance(line, dict) else _get(line, "customer_display_name", "")
        product  = _get(line, "product_name", "")
        qty      = _get(line, "quantity", 0)
        unresolved      = _get(line, "unresolved", False)
        unresolved_reason = _get(line, "unresolved_reason")

        if not unresolved:
            status = "✅ Ready"
        elif unresolved_reason == "product_not_found":
            status = "❌ Product not found"
        elif unresolved_reason == "email_not_provided":
            status = "❌ Email required"
        elif unresolved_reason == "email_not_found":
            status = "❌ Customer not found"
        elif unresolved_reason == "company_not_provided":
            status = "❌ Company required"
        elif unresolved_reason == "company_not_found":
            status = "❌ Company not found"
        elif unresolved_reason == "recipient_not_found":
            status = "❌ Person not found"
        elif unresolved_reason == "recipient_required":
            status = "❌ Recipient required"
        elif unresolved_reason == "both_not_found":
            status = "❌ Both not found"
        else:
            status = "❌ Unresolved"

        rows.append(f"| {customer} | {product} | {qty} | {status} |")

    resolved_count = sum(1 for l in lines if not _get(l, "unresolved", False))
    skipped_count  = len(lines) - resolved_count

    table = (
        "Here's your bulk order summary:\r\n\r\n"
        "| Customer | Product | Qty | Status |\r\n"
        "|---|---|---|---|\r\n"
        + "\r\n".join(rows)
        + "\r\n"
    )

    if skipped_count > 0:
        table += f"\r\n⚠️ {skipped_count} line(s) could not be resolved and will be skipped.\r\n"
    # "line(s)", not "order(s)": several products for one recipient merge into
    # a single order, and this builder has no address context to group by. The
    # order count is computed by _planned_order_count where it is available.
    table += f"✅ {resolved_count} line(s) ready to place."

    return table

def _build_bulk_cart_response(lines_as_dicts, conversation, user_context, page, start_time):
    """
    A CUSTOMER's multi-line order: add every line to their cart instead of
    placing an order.

    A customer can only order for themselves — no company, no recipient, no
    address to resolve — and the order creation path had nowhere to get
    billing and shipping from: the parser leaves both None on a self-order
    line (bulk_order_parser, the `else` branch of `if _is_rep`), and nothing
    downstream filled them, so these orders posted with an empty shipping
    block. Routing to the cart removes the question: the widget adds the items
    with the shopper's own cookie and nonce, and WooCommerce's own checkout
    fills both blocks from their account.

    It also keeps the backend out of the identity business here. Nothing in
    this path sends a customer_id, so a session carrying an id WooCommerce
    does not recognise can still order.

    Reps are never routed here — they order on behalf of others and keep the
    confirmation table and real order creation.

    Same action vocabulary as the single-item ADD_TO_CART flow in
    cart_handler: one ADD_TO_CART per line, OPEN_CART_PANEL last.
    """
    from store_registry import get_store_loader
    from ecommerce.cart_actions import build_cart_add_action
    from core.actions import build_open_cart_panel

    store_loader = get_store_loader()

    actions = []
    added_labels = []
    unresolved_count = 0
    failed_labels = []
    stale_labels = []

    for line in lines_as_dicts:
        if line.get("unresolved") or not line.get("product_id"):
            unresolved_count += 1
            continue

        name = line.get("product_name") or "item"
        qty = int(line.get("quantity") or 1)

        # The catalog can reload between the line being parked and the shopper
        # finishing the flow — see _line_product_is_live. Checked before
        # building the action, because build_cart_add_action would happily
        # emit an add for whatever the id now points at.
        if not _line_product_is_live(line, store_loader):
            logger.warning(
                f"bulk_order | parked line product_id={line['product_id']!r} "
                f"({name}) is no longer in the catalog — catalog reloaded "
                f"mid-order, or state predates the GID-derived ids. Line "
                f"dropped rather than added blind."
            )
            stale_labels.append(name)
            continue

        # build_variation_payload=True on purpose, matching the confirm-add
        # flow rather than the bare ADD_TO_CART intent: variant_meta holds the
        # axes the variation itself cannot encode (WooCommerce "Any"
        # attributes), and those are exactly what the shopper was just asked
        # for. Dropping them would put an item in the cart missing the finish
        # and size they chose. Costs one variation fetch per line.
        action, err = build_cart_add_action(
            product_id=line["product_id"],
            quantity=qty,
            name=name,
            variation_id=line.get("variation_id") or None,
            resolved_attrs=line.get("variant_meta") or {},
            store_loader=store_loader,
            build_variation_payload=True,
            # This function ALWAYS builds its own itemised bot_message below
            # (added_labels), so each ADD_TO_CART/SHOPIFY_ADD_TO_CART action
            # must not also trigger the widget's per-item confirmation —
            # that duplicated the summary with one "✅ Added X" line per
            # product, plus a redundant /chat/cart-result round trip and a
            # re-dispatched OPEN_CART_PANEL for each. See build_add_to_cart().
            suppress_result=True,
        )

        if action is None:
            # Emitting a broken action would fail silently in the browser and
            # the shopper would see a cart missing an item with no explanation.
            logger.warning(
                f"bulk_order | cart add could not resolve a variant for "
                f"product_id={line['product_id']!r} ({name}) reason={err} — "
                f"line dropped from the cart batch"
            )
            failed_labels.append(name)
            continue

        actions.append(action)
        added_labels.append(f"{qty} × **{name}**")

    if not actions:
        logger.warning("bulk_order | self-order produced no addable cart lines")
        return _build_bulk_confirmation_response(
            lines_as_dicts, conversation, user_context, page, start_time,
            _allow_cart_fork=False,
        )

    actions.append(build_open_cart_panel())

    bot_message = f"Added {len(added_labels)} item(s) to your cart 🛒\r\n\r\n"
    bot_message += "\r\n".join(f"- {label}" for label in added_labels)
    if failed_labels:
        bot_message += (
            f"\r\n\r\nI couldn't add {', '.join(failed_labels)} — "
            "the options weren't specific enough. Search for it and pick the "
            "variant you want."
        )
    if stale_labels:
        # Deliberately a separate sentence from failed_labels: "be more
        # specific" is useless advice when the product is simply gone.
        bot_message += (
            f"\r\n\r\n{', '.join(stale_labels)} is no longer available in the "
            "catalogue, so I left it out. Search for it and I'll show you "
            "what's in stock."
        )
    if unresolved_count:
        bot_message += f"\r\n\r\n{unresolved_count} line(s) I couldn't match to a product were skipped."

    logger.info(
        f"bulk_order | customer self-order → cart | added={len(added_labels)} | "
        f"failed={len(failed_labels)} | stale={len(stale_labels)} | "
        f"unresolved={unresolved_count}"
    )

    for k in _BULK_STATE_KEYS:
        user_context.pop(k, None)
    for k in ("bulk_variant_line_indices", "bulk_variant_current_pos", "bulk_variant_cache"):
        user_context.pop(k, None)

    conversation.flow_state = FlowState.IDLE.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["View cart", "Checkout", "Browse products"],
        "actions": actions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.IDLE.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200

def _build_bulk_confirmation_response(lines_as_dicts, conversation, user_context, page, start_time,
                                      _allow_cart_fork=True):
    # "Ready to place" must exclude lines the rep skipped at the address step.
    # Counting every resolvable line told them N orders were ready when only
    # N-minus-skipped would actually be created — and the skipped rows also
    # rendered as "Ready" in the table, so nothing on the card disagreed.
    skipped_count = sum(1 for l in lines_as_dicts if l.get("address_skipped"))
    resolved_count = sum(
        1 for l in lines_as_dicts
        if not l.get("unresolved") and not l.get("address_skipped")
    )
    unresolved_count = len(lines_as_dicts) - resolved_count - skipped_count
    
    # Everything the rep had was SKIPPED — a different situation from
    # "nothing parsed", and it must not borrow that message. Skipping the
    # only line of a single-line order drops resolved_count to 0, so the
    # guard below answered a deliberate skip with "I couldn't find any
    # products in that message" and dropped the rep back into bulk-order
    # input. The skip had worked; the reply said it hadn't, which reads as
    # the button being broken. Multi-line orders hid this — skipping one of
    # three still left resolved_count above zero.
    if resolved_count == 0 and skipped_count:
        for k in _BULK_STATE_KEYS:
            user_context.pop(k, None)
        conversation.flow_state = FlowState.IDLE.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        elapsed = round((time.time() - start_time) * 1000)
        _noun = "order" if skipped_count == 1 else f"all {skipped_count} orders"
        logger.info(
            f"bulk_order | every line skipped ({skipped_count}) — "
            f"nothing placed, ending the flow"
        )
        return jsonify({
            "success": True,
            "bot_message": (
                f"Skipped — {_noun} set aside and nothing was placed. "
                "What else can I help you with?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Browse Products", "Start a bulk order"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.IDLE.value,
                "response_time_ms": elapsed,
                "skipped_orders": skipped_count,
            },
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    # Nothing resolvable at all — re-prompt instead of showing a confusing
    # confirmation card with 0 orders ready and the user's own raw text
    # echoed back as a fake "product".
    if resolved_count == 0:
        elapsed = round((time.time() - start_time) * 1000)
        conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return jsonify({
            "success": True,
            "bot_message": (
                "I couldn't find any products in that message. "
                "Try the format:\r\n\r\n"
                "**Examples:**\r\n"
                "*Order 20 Harmony White, 15 Coral Grey for Beck LTD*\r\n"
                "*Order 20 Harmony White for Ashlynn, 15 Coral Grey for Claire "
                "at Beck LTD*"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_ORDER_INPUT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_ORDER_INPUT.value,
            "pagination": default_pagination(page),
        }), 200

    # An order with nothing but self-scoped lines goes to the CART, not to
    # order creation — see _build_bulk_cart_response. Forked here because
    # every route into the confirmation table funnels through this function
    # (trigger parse, quantity reply, variant reply), so one branch covers
    # them all.
    #
    # Gated on is_self_order across the LIVE lines, not on role. A true rep
    # (BULK_ORDER_ROLES) never gets is_self_order=True from the parser — it
    # has no self-fallback — so this is unchanged for them: always the table,
    # never the cart. A "customer" (full-scope, see BULK_ORDER_FULL_SCOPE_ROLES)
    # CAN now have is_self_order=True on some or all lines: True on any line
    # where nothing was named (company/recipient/email) — an ordinary
    # self-order, same as their pre-existing behavior — and False the moment
    # they name a company/recipient/email for a line, exactly like a rep.
    # A message can mix both: only when EVERY live line is still self-scoped
    # does the whole thing go to cart; the moment even one line resolved to
    # someone else, everything routes through the confirmation table instead
    # (order creation has no role gate, so a self-scoped line riding along in
    # that batch still creates a normal order for the customer's own account).
    #
    # Placed after the "no products found" guard above so an unparseable
    # message still re-prompts.
    _live_lines = [
        l for l in lines_as_dicts
        if not l.get("unresolved") and not l.get("address_skipped")
    ]
    _all_self_order = bool(_live_lines) and all(l.get("is_self_order") for l in _live_lines)
    if _allow_cart_fork and _all_self_order:
        return _build_bulk_cart_response(
            lines_as_dicts, conversation, user_context, page, start_time
        )

    # Render rows with the RESOLVED recipient. The card reads
    # customer_display_name straight off each line, which still carries the
    # "⚠️ No customers for X" status label from the lookup — so a fully
    # prepared order with a confirmed address and a named recipient displayed
    # as if nothing had been found. Shallow copies only: the underlying lines
    # keep the original label, which the unresolved-line paths still rely on.
    _display_lines = []
    for _l in lines_as_dicts:
        if isinstance(_l, dict):
            _c = dict(_l)
            _c["customer_display_name"] = _line_recipient_display(_l)
            _display_lines.append(_c)
        else:
            _display_lines.append(_l)

    # wrap data inside "payload" key
    _order_count = _planned_order_count(lines_as_dicts, user_context)
    action = {
        "type": "SHOW_BULK_ORDER_CONFIRMATION",
        "payload": {
            "lines": _display_lines,
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
            "skipped_count": skipped_count,
            # Lines merge into one order per recipient, so these two differ:
            # four products for one person is 4 line(s) but 1 order. The card
            # previously read resolved_count as an order count and announced
            # "4 order(s) ready to place" for a single order.
            "order_count": _order_count,
            "line_count": resolved_count,
        },
    }

    # Addresses are confirmed before this card is built, so "ready to place"
    # is now literally true and confirming is the final action.
    if _order_count == resolved_count:
        _summary = f"{_order_count} order(s) ready to place"
    else:
        _summary = (
            f"{resolved_count} product(s) in {_order_count} order(s) ready to place"
        )
    bot_message = (
        f"Here's your bulk order — {_summary}. "
        "Confirming will place them."
    )
    if unresolved_count:
        bot_message += f" {unresolved_count} unresolved line(s) will be skipped."
    bot_message += " Would you like to proceed?"

    for k in ("bulk_variant_line_indices", "bulk_variant_current_pos", "bulk_variant_cache"):
        user_context.pop(k, None)

    conversation.flow_state = FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "No, cancel"],
        "actions": [action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Function 4: handle_bulk_order_confirmation ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_confirmation(user_context, conversation, page, start_time):
    """
    Called when the rep confirms the bulk order table (action == "confirm_bulk_order").
    Addresses are already confirmed by this point, so this PLACES the orders.
    Returns a Flask response.
    """
    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]

    # No valid lines to place
    if not resolved_lines:
        user_context.pop("pending_bulk_lines", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        conversation.flow_state = FlowState.IDLE.value

        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "No valid order lines to place.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Place another bulk order", "Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.IDLE.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    # Addresses are confirmed BEFORE this summary is shown, so "Yes, confirm"
    # is the final step: place the orders. It previously kicked off the
    # address loop, which is why the rep was asked to approve a table and was
    # then immediately asked for more information.
    #
    # Safety gate: several other paths also render this summary (re-entry
    # after an edit, resumed sessions). If any line reaches here without a
    # confirmed address, collect it rather than placing an order whose
    # destination the rep never saw — the reordering must not turn a missed
    # step into an unverified order.
    _unconfirmed = [
        l for l in resolved_lines
        if not l.get("address_confirmed") and not l.get("address_skipped")
    ]
    if _unconfirmed:
        logger.info(
            f"bulk_order | confirm pressed with {len(_unconfirmed)} line(s) "
            f"lacking a confirmed address — collecting before placing"
        )
        user_context["bulk_current_line_index"] = 0
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, 0, conversation, user_context, page, start_time,
        )

    return _create_all_confirmed_orders(user_context, conversation, page, start_time)

def handle_bulk_confirmation_unclear(conversation, page, start_time):
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Please reply **Yes** to confirm all orders or **No** to cancel.",
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "No, cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Function 7: _create_all_confirmed_orders (private) ──
# ══════════════════════════════════════════════════════════════

def _order_group_key(line, line_idx, address_overrides, rep_email, rep_billing,
                     user_context=None):
    """
    Key deciding which lines merge into ONE WooCommerce order.

    Shared by _create_all_confirmed_orders and _planned_order_count so the
    number the rep is shown on the confirmation card is computed the same way
    as the orders actually created — the card said "4 order(s)" for four lines
    that merged into a single order.

    The RECIPIENT NAME is part of the key, not just customer_id. With no
    customer account (address-only mode) every line carries customer_id=None,
    so two different people ordering to the same company address collapsed
    into ONE order labelled with whichever line came first. The effective
    address is included because two lines for the same person can carry
    DIFFERENT addresses when the rep edited one in the per-line panel, and an
    order has exactly one billing and one shipping block.
    """
    _billing, _shipping = _effective_address_for_line(
        line, address_overrides, line_idx, rep_email, rep_billing,
        user_context=user_context,
    )
    _recipient_key = (
        line.get("recipient_name")
        or line.get("customer_display_name")
        or ""
    ).strip().lower()
    return (
        str(line.get("customer_id")),
        _recipient_key,
        _address_group_key(_billing),
        _address_group_key(_shipping),
    ), _billing, _shipping

def _planned_order_count(lines_as_dicts, user_context) -> int:
    """
    How many orders confirming will actually create — distinct recipients,
    not line count.
    """
    overrides = user_context.get("bulk_address_overrides", {}) or {}
    rep_email = user_context.get("rep_email", "")
    rep_billing = user_context.get("rep_billing_address")
    keys = set()
    for idx, line in enumerate(lines_as_dicts):
        if line.get("unresolved") or line.get("address_skipped"):
            continue
        try:
            key, _b, _s = _order_group_key(line, idx, overrides, rep_email, rep_billing,
                                           user_context=user_context)
        except Exception:
            # Counting must never break the confirmation card; fall back to
            # treating this line as its own order.
            key = ("__line__", idx)
        keys.add(key)
    return len(keys)

def _bulk_line_item(line: dict, user_context: dict) -> dict:
    """
    One WooCommerce order line item.

    variant_meta carries the rep's choices for axes the variation itself can't
    encode (WooCommerce "Any" attributes such as Adams' Finish and Sample
    Size). They ride as line-item meta, which is where WooCommerce records an
    "Any" attribute chosen on the product page — without this the order would
    show a colour and nothing else, even though the rep was asked.
    """
    item = {
        "product_id": line["product_id"],
        "variation_id": line.get("variation_id") or 0,
        "quantity": line["quantity"],
    }
    meta = line.get("variant_meta") or {}
    if meta:
        item["meta_data"] = [
            _variant_meta_entry(line.get("product_id"), axis, value, user_context)
            for axis, value in meta.items()
            if str(value).strip()
        ]
    return item

def _create_all_confirmed_orders(user_context, conversation, page, start_time):
    """
    Places WooCommerce orders for every confirmed line, then clears all
    bulk state and returns a summary response.
    Returns a Flask response.
    """
    # Step 1: Gather lines
    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]
    address_overrides = user_context.get("bulk_address_overrides", {})

    confirmed_count = sum(1 for l in resolved_lines if l.get("address_confirmed"))
    skipped   = [l for l in resolved_lines if l.get("address_skipped")]

    # Step 2–3: Place orders. Iterate over resolved_lines with index so we can
    # look up per-line address overrides (keyed by index in resolved_lines).
    created_orders = []
    failed_orders  = []

    # ── Group by recipient ────────────────────────────────────────────────────
    # One order per PERSON, not per line: several products for the same
    # customer merge into a single order with several line_items, while
    # different customers still get their own orders.
    #
    # The group key is the customer plus the effective address. Two lines for
    # the same person can carry DIFFERENT addresses when the rep edited one of
    # them in the per-line panel, and an order has exactly one billing and one
    # shipping block — merging those would silently ship a line to the wrong
    # place, so they stay separate.
    #
    # Order is preserved (dict keeps insertion order) so the summary still
    # reads in the sequence the rep typed.
    rep_email = user_context.get("rep_email", "")

    # Who is placing these orders. Taken from the session, not from any line —
    # the lines carry the RECIPIENT. Coerced because conversation.customer_id
    # arrives as a string on some sessions and WooCommerce wants an int.
    _placer_customer_id = 0
    try:
        _raw_placer = getattr(conversation, "customer_id", None)
        if _raw_placer:
            _placer_customer_id = int(_raw_placer)
    except (TypeError, ValueError):
        logger.warning(
            f"bulk_order | unusable session customer_id "
            f"{getattr(conversation, 'customer_id', None)!r} — orders will be "
            f"created as guest orders"
        )
    if not _placer_customer_id:
        logger.warning(
            "bulk_order | no session customer_id — orders will be created as "
            "guest orders (customer_id 0)"
        )

    _groups: dict = {}

    for line_idx, line in enumerate(resolved_lines):
        if not line.get("address_confirmed"):
            continue

        # Key, billing and shipping all come from _order_group_key so the
        # count shown on the confirmation card cannot disagree with what is
        # created here.
        _key, _billing, _shipping = _order_group_key(
            line, line_idx, address_overrides, rep_email,
            user_context.get("rep_billing_address"),
            user_context=user_context,
        )
        if _key not in _groups:
            _groups[_key] = {
                "line_idx": line_idx,          # first line — used for overrides/meta
                "line": line,
                "billing": _billing,
                "shipping": _shipping,
                "lines": [],
            }
        _groups[_key]["lines"].append(line)

    logger.info(
        f"bulk_order | {len(resolved_lines)} confirmed line(s) → "
        f"{len(_groups)} order(s) after grouping by recipient"
    )

    for _group in _groups.values():
        line       = _group["line"]           # representative line (customer/meta)
        group_lines = _group["lines"]
        billing    = dict(_group["billing"])
        shipping   = dict(_group["shipping"])

        # ── Defence in depth ──
        # The gate in handle_bulk_address_confirmation_reply should already have
        # caught this. Re-checking here means any future path that sets
        # address_confirmed without going through that gate still cannot create
        # a blank-address order — it lands in the failed list instead.
        _errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(_errors):
            failed_orders.append({
                "customer": line["customer_display_name"],
                "product": ", ".join(gl["product_name"] for gl in group_lines),
                "error": f"Missing required address fields: {format_missing_fields(_errors)}",
            })
            logger.warning(
                f"bulk_order | refused to create order for {line['customer_display_name']} "
                f"| missing={format_missing_fields(_errors)}"
            )
            continue

        # ── Defence in depth: project_rep must resolve against the REAL rep
        # list ──
        # is_known_rep() is only enforced at auto-fill time
        # (_effective_address_for_line: billing["project_rep"] = rep_email if
        # is_known_rep(rep_email) else "") and was never re-checked at
        # submission. Autofill only ever seeds a value FROM the validated rep
        # list, but nothing stops a raw API call — or a frontend gap in
        # BulkAddressConfirmationCard.tsx, which isn't in this codebase to
        # verify — from setting billing.project_rep to any non-blank string
        # and having it persist straight into the _billing_project_rep order
        # meta with nothing to stop it. Re-validate here, the same
        # defence-in-depth pattern as the address check just above, so this
        # can never be bypassed regardless of how the field got set. Reuses
        # is_known_rep()'s existing fail-OPEN behaviour during a plugin
        # outage (an empty option list accepts any non-empty value) — the
        # same tradeoff already accepted for autofill, kept consistent here
        # rather than inventing a stricter, inconsistent rule for submission.
        _rep_value = billing.get("project_rep") or ""
        if _rep_value and not is_known_rep(_rep_value):
            failed_orders.append({
                "customer": line["customer_display_name"],
                "product": ", ".join(gl["product_name"] for gl in group_lines),
                "error": f"Invalid rep selection: {_rep_value!r} is not a recognised rep.",
            })
            logger.warning(
                f"bulk_order | refused to create order for {line['customer_display_name']} "
                f"| project_rep {_rep_value!r} not in known rep list — rejected at "
                f"submission (autofill/frontend validation was bypassed or absent)"
            )
            continue

        # ── Custom CS fields → order meta (not address-block fields) ──
        # project_rep is already defaulted inside _effective_address_for_line,
        # and only when the logged-in user is a real rep, AND re-validated
        # against the real rep list just above (the "Defence in depth"
        # project_rep gate). No `or rep_email` fallback here: that second,
        # unvalidated seed would have re-applied a non-rep email at creation
        # time even after the gate and the panel had both correctly left the
        # field blank. Whatever survives to this line is either blank or has
        # already passed is_known_rep(), so this cannot silently drop a value
        # a rep chose nor let an unvalidated one through.
        project_rep  = billing.get("project_rep") or ""
        project_name = billing.get("billing_project") or ""
        field_type   = billing.get("billing_field_type") or ""
        order_notes  = shipping.get("order_notes") or ""
        # WooCommerce core has no set_shipping_email() — unlike billing.email
        # (a real WC property), a shipping email left nested in the shipping
        # object is silently dropped by WC_REST_Orders_Controller::update_address()
        # on order creation. Must go through meta_data like the other custom
        # fields above, or it never persists no matter how it was prefilled.
        shipping_email = shipping.get("email") or ""

        # Four keys, all of them read by something.
        #
        # Deliberately NOT written, though storefront checkout writes them:
        #   _billing_company_name / _shipping_company_name — duplicates of
        #     billing.company / shipping.company, which are already on the
        #     order. /company-order-addresses reads the SHIPPING FIELD
        #     ($order->get_shipping_company()); the _shipping_company_name
        #     meta_query in class-api.php is a USER meta query for the
        #     customer roster, not an order one, so an order-level copy is
        #     never consulted.
        #   _shipping_address_selector — the storefront's saved-address picker
        #     value, always empty for orders this flow creates.
        #
        # _billing_project is the storefront's key and now the only one used
        # anywhere. class-api.php previously wrote and read
        # _billing_project_name — that was the wrong key, not a second valid
        # one, so both its writer and its reader moved here with no fallback.
        # Orders already placed under the old key need a one-time meta rename.
        meta_data = []
        if project_rep:
            meta_data.append({"key": "_billing_project_rep", "value": project_rep})
        if project_name:
            meta_data.append({"key": "_billing_project", "value": project_name})
        if field_type:
            meta_data.append({"key": "_billing_field_type", "value": field_type})
        if shipping_email:
            meta_data.append({"key": "_shipping_email", "value": shipping_email})

        # Remove custom keys from the Woo address blocks — they live in meta.
        # Only shipping's email is stripped: billing.email IS a real WC
        # property (set_billing_email exists) and must stay in the billing
        # block to keep working as it always has.
        for _k in ("project_rep", "billing_project", "billing_field_type", "order_notes"):
            billing.pop(_k, None)
            shipping.pop(_k, None)
        shipping.pop("email", None)

        payload = {
            "status": "processing",
            # The PLACER owns the order, not the recipient.
            #
            # This mirrors what the storefront itself produces: order #1066561
            # was placed by sovan (customer_id 272754865, billing = Silfra
            # Digital) and shipped to Ashlynn Archer at Abel Design Group —
            # customer_id is the person checking out, and the recipient lives
            # entirely in the shipping block. WooCommerce has exactly one
            # customer_id per order, so it cannot hold both.
            #
            # Previously this was line["customer_id"] — the RECIPIENT's account
            # id — which left an address-only order (company with delivery
            # history but no customer accounts) posting customer_id: None and
            # landing as a guest order owned by nobody.
            "customer_id": _placer_customer_id,
            "payment_method": DEFAULT_PAYMENT_METHOD,
            "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
            "set_paid": False,
            "line_items": [
                _bulk_line_item(gl, user_context) for gl in group_lines
            ],
        }
        if meta_data:
            payload["meta_data"] = meta_data
        if order_notes:
            payload["customer_note"] = order_notes
        billing  = {k: v for k, v in billing.items() if v}
        shipping = {k: v for k, v in shipping.items() if v}
        # Attached unconditionally. The old code attached only when address_1
        # was present, which is exactly how "no address" turned into a
        # successful order with an empty billing block rather than a failure.
        # Validation above guarantees both blocks are complete by this point.
        payload["billing"] = billing
        payload["shipping"] = shipping

        # customer_display_name carries a ⚠️ STATUS LABEL when no customer
        # account resolved ("⚠️ No customers for Turner Ceramic Tile"). That
        # is fine on the confirmation table, but it read as the recipient's
        # name in the order log and in the response, so a legitimate
        # address-only order looked like a failed one. Prefer the actual
        # shipping name, then the company, and only fall back to the label.
        _ship_name = " ".join(
            v for v in (shipping.get("first_name"), shipping.get("last_name")) if v
        ).strip()
        _order_for = (
            _ship_name
            or shipping.get("company")
            or billing.get("company")
            or line.get("customer_display_name", "")
        )

        order_call = endpoints.create_order(
            payload=payload,
            description=f"Bulk order for {_order_for}",
        )
        order_resp = woo_client.execute(order_call)

        if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
            new_order = order_resp["data"]
            created_orders.append({
                "order_number": new_order.get("number") or new_order.get("id"),
                "customer": _order_for,
                # A merged order covers several products — name them all, or
                # the summary silently under-reports what was placed.
                #
                # Per-product quantities, NOT a joined name plus a summed
                # count: "Elizabeth Mosaic, London ×2" reads as two of a
                # single thing, when it is one of each. The summary is the
                # rep's only record of what was actually placed, so it has to
                # be unambiguous.
                "product": ", ".join(
                    f"{gl['product_name']} ×{int(gl.get('quantity') or 0)}"
                    for gl in group_lines
                ),
                "quantity": sum(int(gl.get("quantity") or 0) for gl in group_lines),
            })
            logger.info(
                f"bulk_order | created order #{new_order.get('number') or new_order.get('id')} "
                f"for {_line_recipient_display(line)} | {len(group_lines)} line item(s)"
            )
        else:
            failed_orders.append({
                "customer": _order_for,
                "product": ", ".join(gl["product_name"] for gl in group_lines),
                "error": str(order_resp.get("error", "Unknown error")),
            })
            logger.warning(
                f"bulk_order | failed for {line['customer_display_name']} "
                f"product='{line['product_name']}' | error={order_resp.get('error')}"
            )

    # Step 4: Clear all bulk state
    user_context.pop("pending_bulk_lines", None)
    user_context.pop("bulk_current_line_index", None)
    user_context.pop("bulk_confirmed_lines", None)
    user_context.pop("bulk_address_overrides", None)
    user_context.pop("bulk_awaiting_address_text", None)
    user_context.pop("bulk_product_missing_indices", None)
    user_context.pop("bulk_product_current_pos", None)
    user_context.pop("bulk_quantity_pending_indices", None)
    user_context.pop("bulk_quantity_current_pos", None)
    # Must not survive this order: it suppresses the company prompt, and a
    # leftover flag would silently skip company resolution on the NEXT bulk
    # order in the same session.
    user_context.pop("bulk_company_skipped", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value

    # Step 5: Build summary message
    bot_message = ""

    if created_orders:
        bot_message += f"✅ **{len(created_orders)} order(s) placed successfully:**\r\n\r\n"
        for o in created_orders:
            bot_message += f"• **#{o['order_number']}** — {o['customer']}: {o['product']}\r\n"

    if failed_orders:
        bot_message += f"\r\n⚠️ **{len(failed_orders)} order(s) failed:**\r\n\r\n"
        for fail in failed_orders:
            bot_message += f"• {fail['customer']}: {fail['product']} — {fail['error']}\r\n"

    if skipped:
        bot_message += f"\r\n⏭️ **{len(skipped)} order(s) skipped.**\r\n"

    if not bot_message:
        bot_message = "No orders were placed."

    suggestions = ["Show my orders"]
    # if failed_orders:
        # suggestions.append("Try again")
    suggestions.append("Place another bulk order")

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "bulk_order",
        "products": [],
        "suggestions": suggestions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.IDLE.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200