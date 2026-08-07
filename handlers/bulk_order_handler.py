"""
handlers/bulk_order_handler.py — Bulk order flow handler.

Feature 3: Multi-line, multi-customer bulk ordering for rep and non-rep users.

Public functions:
    handle_bulk_order_trigger()               — entry point, AWAITING_BULK_ORDER_INPUT
    handle_bulk_order_input()                 — parse utterance → confirmation table
    handle_bulk_order_confirmation()          — start per-customer address confirmation
    handle_bulk_address_confirmation_reply()  — route address-step actions

Private helpers:
    _format_bulk_confirmation_table()         — markdown summary table
    _advance_to_next_address_confirmation()   — step through resolved lines
    _create_all_confirmed_orders()            — place all confirmed orders

Session management (db.session.add / db.session.commit) is the caller's
responsibility in chat.py. This module only mutates conversation attributes
and calls flag_modified.
"""

import time
import json

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from app_config import (
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
    BULK_ORDER_ROLES,
)
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination, _get_safe_options
from parsers.bulk_order_parser import (
    parse_bulk_order_utterance,
    BulkOrderLine,
    MultipleCompaniesError,
)
from utils.checkout_fields import (
    count_missing,
    format_missing_fields,
    get_required_fields,
    has_errors,
    validate_bulk_address,
)
import re
import difflib
logger = get_logger("miraq_chat")

_BULK_STATE_KEYS = (
    "pending_bulk_lines", "bulk_current_line_index",
    "bulk_confirmed_lines", "bulk_address_overrides",
    "bulk_awaiting_address_text",
    "bulk_product_missing_indices", "bulk_product_current_pos",
    "bulk_quantity_pending_indices", "bulk_quantity_current_pos",
)

def handle_cancel_bulk_order(user_context, conversation, page, start_time):
    pending_lines = user_context.get("pending_bulk_lines", [])
    is_single_self_order = (
        len(pending_lines) == 1
        and pending_lines[0].get("is_self_order")
    )

    for k in _BULK_STATE_KEYS:
        user_context.pop(k, None)
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value
    elapsed = round((time.time() - start_time) * 1000)

    bot_message = (
        "Order cancelled. What else can I help you with?"
        if is_single_self_order
        else "Bulk order cancelled. What else can I help you with?"
    )

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Browse Products", "Start a bulk order"],
        "session_id": str(conversation.id),
        "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": elapsed},
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200

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
# ── Helper: detect variable products ──
# ══════════════════════════════════════════════════════════════

def _is_variable_product(product_id: int, store_loader) -> bool:
    """Return True if the catalog entry for product_id has variations."""
    if not store_loader or not product_id:
        return False
    for p in (store_loader.products or []):
        if p.get("id") == product_id:
            return bool(p.get("variations")) or p.get("type") == "variable"
    return False

# ══════════════════════════════════════════════════════════════
# ── Helper: dual-access for BulkOrderLine or dict ──
# ══════════════════════════════════════════════════════════════

def _get(line, key, default=None):
    """Read a field from either a BulkOrderLine dataclass or a plain dict."""
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)


# ══════════════════════════════════════════════════════════════
# ── Helper: effective address for a bulk line ──
# ══════════════════════════════════════════════════════════════

def _merge_address_block(base, override):
    """
    Merge a panel override onto a base address block.

    A key ABSENT from the override keeps its base value; a key PRESENT with an
    empty string CLEARS it.

    The previous behaviour skipped empty override values entirely ("don't let
    blank panel fields wipe real data"), which meant a rep could not blank a
    field at all: clearing it in the panel silently restored the stale value,
    and the validation gate would then pass on data the rep had deliberately
    removed. Honouring empties is safe because the panel prefills from the same
    _pick()-generated payload the base came from, so unedited fields round-trip
    to identical values. If _pick is ever narrowed to emit fewer keys than the
    raw address block holds, revisit this.

    None is treated as "absent" rather than "clear", so a malformed payload
    can't wipe an address.
    """
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if value is None:
            continue
        merged[key] = value
    return merged


def _rep_billing_address(conversation, user_context):
    """
    Fetch the LOGGED-IN user's billing block, cached in user_context.

    Requirement: bulk order billing comes from the logged-in user, not from the
    customer the goods ship to. Shipping still comes from the resolved
    company/person — see _effective_address_for_line, which combines them.

    Cached because it is identical for every line in the transaction. A FAILED
    fetch is deliberately not cached: user_context is persisted on the
    conversation, so caching {} after one dropped connection would starve
    billing for the rest of the session with no way to recover. A successful
    fetch that happens to be empty IS cached — that is a real answer.
    """
    if user_context.get("rep_billing_fetched"):
        return user_context.get("rep_billing_address") or {}

    billing = {}
    fetched = False
    rep_id = getattr(conversation, "customer_id", None)
    if rep_id:
        try:
            call = endpoints.fetch_customer(
                customer_id=int(rep_id),
                description="Fetch logged-in user billing for bulk order",
            )
            result = woo_client.execute(call)
            if result.get("success") and isinstance(result.get("data"), dict):
                billing = result["data"].get("billing", {}) or {}
                fetched = True
            else:
                logger.warning(
                    f"bulk_order | rep billing fetch unsuccessful for {rep_id} | "
                    f"error={result.get('error')}"
                )
        except Exception as exc:
            logger.warning(f"bulk_order | rep billing fetch failed | error={exc}")

    if fetched:
        user_context["rep_billing_address"] = billing
        user_context["rep_billing_fetched"] = True

    logger.info(
        f"bulk_order | rep billing for user {rep_id} | fetched={fetched} | "
        f"company={billing.get('company')!r} | keys={sorted(billing.keys())}"
    )
    return billing


def _effective_address_for_line(line, address_overrides, line_idx, rep_email, rep_billing=None):
    """
    Return (billing, shipping) for one bulk line.

    Billing is the LOGGED-IN user's billing block (rep_billing) when available,
    since bulk orders are billed to the rep placing them; the line's own
    billing_address is the fallback. Shipping always comes from the resolved
    company/person. The per-line panel override is merged in last, so anything
    the rep edits by hand still wins.

    This is the ONLY place bulk address merging happens. The validation gate,
    the card prefill and _create_all_confirmed_orders all call it, so they
    cannot drift apart — a line that passes validation is guaranteed to be the
    same line that gets posted to WooCommerce.

    The project_rep default is applied HERE, before validation, because order
    creation auto-fills it from the logged-in rep. Validating before applying it
    would block on a field that would have been populated anyway.
    """
    override = (address_overrides or {}).get(str(line_idx)) or {}

    _billing_base = rep_billing if (rep_billing and rep_billing.get("address_1")) else _get(line, "billing_address")
    billing = _merge_address_block(_billing_base, override.get("billing"))
    shipping = _merge_address_block(_get(line, "shipping_address"), override.get("shipping"))

    if not str(billing.get("project_rep") or "").strip():
        billing["project_rep"] = rep_email or ""

    return billing, shipping



# ══════════════════════════════════════════════════════════════
# ── Function 1: handle_bulk_order_trigger ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_trigger(conversation, user_context, page, start_time):
    """
    Entry point for the bulk order flow.
    Sets flow state to AWAITING_BULK_ORDER_INPUT and prompts the rep.
    Returns a Flask response.
    """
    conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        # Mirrors what the parser actually accepts: one company per order,
        # identified by NAME (never an email), with an optional person per
        # line. The old copy still showed the email form, which the flow no
        # longer supports — reps followed it and hit "company required".
        "bot_message": (
            "Tell me everything you need to order today. "
            "One company per order, with as many products as you like.\r\n\r\n"
            "**Examples:**\r\n"
            "*Order 20 Harmony White, 15 Coral Grey for Company_Name*\r\n"
            "*Order 20 Harmony White for Ashlynn, 15 Coral Grey for Claire "
            "at Company_Name*\r\n\r\n"
            "If you leave out the person, I'll ask who it's for."
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


# ══════════════════════════════════════════════════════════════
# ── Function 2: handle_bulk_order_input ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_input(message, store_loader, conversation, user_context, page, start_time, pre_resolved=None):
    """
    Parses the free-text bulk order utterance, serializes lines into
    user_context, and returns a confirmation table for the rep to approve.
    Returns a Flask response.
    """
    role = user_context.get("role", "")
    customer_id = conversation.customer_id

    # Step 2: Parse
    _parse_meta: dict = {}
    try:
        lines = parse_bulk_order_utterance(
            text=message,
            store_loader=store_loader,
            role=role,
            self_customer_id=str(customer_id) if customer_id else None,
            meta_out=_parse_meta,
        )
    except MultipleCompaniesError as e:
        # One company per transaction — reject rather than guess which one.
        conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value
        _named = ", ".join(f"**{c}**" for c in e.companies)
        elapsed = round((time.time() - start_time) * 1000)
        logger.info(f"bulk_order | rejected multi-company request: {e.companies}")
        return jsonify({
            "success": True,
            "bot_message": (
                f"A bulk order can only be placed for one company at a time, "
                f"but this request names {_named}. Please send a separate "
                f"order for each company."
            ),
            "intent": "bulk_order",
            "flow_state": FlowState.AWAITING_BULK_ORDER_INPUT.value,
            "products": [],
            "actions": [],
            "suggestions": ["Cancel Order"],
            "metadata": {"response_time_ms": elapsed},
            "pagination": default_pagination(page),
        }), 200

    # Step 3: Nothing parsed
    if not lines:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                "I couldn't parse any order lines from that. Please try again."
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

    # Step 4: Serialize to dicts for JSONB storage
    lines_as_dicts = [
        {
            "raw_fragment": l.raw_fragment,
            "company_name": l.company_name,
            "recipient_name": l.recipient_name,
            "email": l.email,
            "product_name": l.product_name,
            "quantity": l.quantity,
            "product_id": l.product_id,
            "variation_id": l.variation_id,
            "customer_id": l.customer_id,
            "customer_display_name": l.customer_display_name,
            "is_self_order": l.is_self_order,
            "shipping_address": l.shipping_address,
            "billing_address": l.billing_address,
            "is_reorder": l.is_reorder,
            "reorder_source_order_id": l.reorder_source_order_id,
            "unresolved": l.unresolved,
            "unresolved_reason": l.unresolved_reason,
            "quantity_explicitly_set":  l.quantity_explicitly_set,
            "unmatched_variant_hint": l.unmatched_variant_hint,
            "blank_variant_axes": list(l.blank_variant_axes or []),
            "variant_meta": {},
            "address_confirmed": False,
            "address_skipped": False,
        }
        for l in lines
    ]
    
     # ── Patch: apply classifier-resolved product when parser misidentified digits in name ──
    if pre_resolved and pre_resolved.product_id:
        for ld in lines_as_dicts:
            if ld.get("unresolved") and ld.get("unresolved_reason") in (
                "product_not_found", "both_not_found"
            ):
                # Strip the known product name from raw_fragment,
                # then re-scan what's left for an explicit quantity
                stripped = re.sub(
                    re.escape(pre_resolved.product_name or ""),
                    "", ld["raw_fragment"], flags=re.I,
                ).strip()
                qty_match = re.search(r'\b(\d+)\b(?!\.\d)', stripped)

                ld["product_id"]   = pre_resolved.product_id
                ld["product_name"] = pre_resolved.product_name
                ld["variation_id"] = pre_resolved.variation_id

                if qty_match:
                    ld["quantity"] = int(qty_match.group(1))
                    ld["quantity_explicitly_set"] = True

                # Fix unresolved: still unresolved only if customer is also missing
                if ld.get("customer_id"):
                    ld["unresolved"]        = False
                    ld["unresolved_reason"] = None
                else:
                    ld["unresolved_reason"] = "email_not_provided"

                logger.debug(
                    f"bulk_handler | pre_resolved patch applied | "
                    f"product='{pre_resolved.product_name}' id={pre_resolved.product_id} "
                    f"qty={ld['quantity']}"
                )

    user_context["pending_bulk_lines"] = lines_as_dicts
    # Keep the raw utterance so AWAITING_BULK_COMPANY can replay it verbatim
    # once the rep names the company.
    user_context["pending_bulk_utterance"] = message
    # Bulk orders bill to the logged-in user — fetch once for the whole batch.
    _rep_billing_address(conversation, user_context)
    # Roster for the resolved company, so the recipient picker can list real
    # people instead of falling back to asking for an email address.
    user_context["bulk_company_roster"] = _parse_meta.get("company_roster", [])
    user_context["bulk_company_scope"]  = _parse_meta.get("company_scope", "")
    user_context["bulk_current_line_index"] = 0
    user_context["bulk_confirmed_lines"] = []
    user_context["bulk_address_overrides"] = {}
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    
    
    # Step 4.5: Any line with a completely blank product name → ask before anything else
    # (applies to all roles — no point asking email/address without knowing what to order)
    product_blank_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("product_name", "").strip()
    ]
    if product_blank_indices:
        user_context["bulk_product_missing_indices"] = product_blank_indices
        user_context["bulk_product_current_pos"] = 0
        conversation.flow_state = FlowState.AWAITING_BULK_PRODUCT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        first_line = lines_as_dicts[product_blank_indices[0]]
        customer_hint = (
            "" if first_line.get("is_self_order")
            else f" for **{first_line['customer_display_name']}**"
            if first_line.get("customer_id") else ""
        )
        already_resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
        resolved_note = f"\r\n\r\n{len(already_resolved)} other line(s) already resolved." if already_resolved else ""

        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"What product and quantity would you like to order{customer_hint}?{resolved_note}"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pagination": default_pagination(page),
        }), 200

    # Step 4.55: Rep lines with no company scope → ask for the company first.
    # Company is the identity key for bulk orders, so this precedes everything.
    if role in BULK_ORDER_ROLES:
        company_missing = [
            l for l in lines_as_dicts
            if l.get("unresolved_reason") in ("company_not_provided", "company_not_found")
        ]
        if company_missing:
            conversation.flow_state = FlowState.AWAITING_BULK_COMPANY.value
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            product_lines = "\r\n".join(
                f"• **{l['quantity']}× {l['product_name']}**"
                for l in company_missing
            )
            _tried = next(
                (l.get("company_name") for l in company_missing if l.get("company_name")),
                "",
            )
            _ask = (
                f"I couldn't find any customers for **{_tried}**. "
                "Which company is this order for?"
                if _tried else
                "Which company is this order for?"
            )

            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": f"Got it:\r\n{product_lines}\r\n\r\n{_ask}",
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Cancel"],
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                    "response_time_ms": elapsed,
                },
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "pagination": default_pagination(page),
            }), 200

    # Step 4.56: Company resolved but no person resolved → ask WHICH person.
    # Bulk orders are keyed on company name, never an email address, so a line
    # that knows its company must never fall through to the email prompt below.
    #
    # Asked PER DISTINCT RECIPIENT, not once for the whole order: example
    # query 2 ships different products to different people at one company, so
    # "Harmony for Ashlynn, Adams for Claire" is two separate questions. Lines
    # that named nobody share a single slot, since one pick genuinely covers
    # them all.
    if role in BULK_ORDER_ROLES:
        queue = _build_recipient_queue(lines_as_dicts)
        if queue:
            # Several lines named nobody — one person or several? Ask before
            # assuming; guessing either way silently misassigns goods.
            if _unnamed_multi_slot(queue) and not user_context.get("bulk_recipient_mode"):
                return _ask_recipient_mode(
                    lines_as_dicts, queue,
                    conversation, user_context, page, start_time,
                )
            user_context["bulk_recipient_queue"] = queue
            user_context["bulk_recipient_pos"] = 0
            return _ask_for_bulk_recipient(
                lines_as_dicts, queue, 0,
                conversation, user_context, page, start_time,
            )

    # Step 4.6: Rep lines missing an email → ask before proceeding
    if role in BULK_ORDER_ROLES:
        email_missing = [l for l in lines_as_dicts if l.get("unresolved_reason") == "email_not_provided"]
        if email_missing:
            conversation.flow_state = FlowState.AWAITING_BULK_EMAIL.value
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            product_lines = "\r\n".join(
                f"• **{l['quantity']}× {l['product_name']}**"
                for l in email_missing
            )
            already_resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
            resolved_note = f"\r\n\r\n{len(already_resolved)} other line(s) already resolved." if already_resolved else ""

            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Got it:\r\n{product_lines}{resolved_note}\r\n\r\n"
                    "Please provide the customer's email address."
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Cancel"],
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                    "response_time_ms": elapsed,
                },
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "pagination": default_pagination(page),
            }), 200

    # Step 4.7: Lines with no explicit quantity — ask before variant selection
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )
           
    # Step 5: Check for variable products with unresolved variation_id
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # "not variation_id" is not enough: a variation can match on colour and
        # still leave Finish / Sample Size as WooCommerce "Any", which the
        # storefront makes the shopper choose. blank_variant_axes carries those.
        if l["product_id"]
        and (not l["variation_id"] or l.get("blank_variant_axes"))
        and _is_variable_product(l["product_id"], store_loader)
    ]

    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"] = 0
        user_context["bulk_variant_cache"] = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    # Step 6: No variant selection needed — return structured confirmation
    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )


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
        customer = _get(line, "customer_display_name", "")
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
    table += f"✅ {resolved_count} order(s) ready to place."

    return table

# ══════════════════════════════════════════════════════════════
# ── Private: _ask_for_bulk_variant ──
# ══════════════════════════════════════════════════════════════

def _parent_any_axis_options(product_id, axis_names, user_context):
    """
    Options for axes the matched variation leaves as WooCommerce "Any".

    Those options exist only on the PARENT product — every variation has the
    attribute key with an empty value — so they can't be derived from the
    variation list the way the normal bulk variant prompt does it. Cached per
    product because the prompt is re-rendered on every failed reply.
    """
    if not axis_names:
        return {}

    cache = user_context.setdefault("bulk_parent_axis_cache", {})
    key = str(product_id)
    if key not in cache:
        axes = {}
        try:
            call = endpoints.fetch_product(
                product_id=product_id,
                description=f"Fetch parent attributes for product_id={product_id}",
            )
            res = woo_client.execute(call)
            data = res.get("data") if res.get("success") else None
            if isinstance(data, dict):
                for a in data.get("attributes", []) or []:
                    if not isinstance(a, dict) or not a.get("variation"):
                        continue
                    name = str(a.get("name") or "").strip()
                    opts = [str(o) for o in (a.get("options") or []) if str(o).strip()]
                    if name and opts:
                        axes[name] = opts
        except Exception as exc:
            logger.warning(
                f"bulk_order | parent attribute fetch failed for {product_id} | error={exc}"
            )
        cache[key] = axes
        user_context["bulk_parent_axis_cache"] = cache

    parent = cache.get(key, {}) or {}
    wanted = {a.strip().lower() for a in axis_names if a}
    return {
        name: opts for name, opts in parent.items()
        if name.strip().lower() in wanted
    }


def _ask_for_bulk_variant(
    lines_as_dicts, needs_variant_indices, pos,
    conversation, user_context, page, start_time,
):
    line_idx = needs_variant_indices[pos]
    line = lines_as_dicts[line_idx]
    product_id = line["product_id"]

    cache = user_context.setdefault("bulk_variant_cache", {})
    cache_key = str(product_id)

    if cache_key not in cache:
        var_call = endpoints.list_variants(
            product_id=product_id,
            per_page=100,
            description=f"Fetch variations for bulk order product_id={product_id}",
        )
        var_result = woo_client.execute(var_call)
        raw = var_result.get("data", []) if var_result.get("success") else []
        cache[cache_key] = raw if isinstance(raw, list) else []
        user_context["bulk_variant_cache"] = cache
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

    variations = cache.get(cache_key, [])

    attr_axes: dict = {}
    for var in variations:
        # _get_safe_options normalises both the custom flat-dict shape and
        # the standard WC list-of-dicts shape, and drops blank options.
        for name, option in _get_safe_options(var.get("attributes", [])).items():
            if name and option:
                attr_axes.setdefault(name, set()).add(option)

    # Axes the matched variation leaves as "Any" carry no value on ANY
    # variation, so attr_axes above can't see them — their options live on the
    # parent product. The storefront makes the shopper pick these, so the rep
    # must be asked too.
    _any_axes = _parent_any_axis_options(
        product_id, line.get("blank_variant_axes") or [], user_context
    )
    for _name, _opts in _any_axes.items():
        attr_axes.setdefault(_name, set()).update(_opts)

    attributes = [
        {"name": name, "options": sorted(opts)}
        for name, opts in attr_axes.items()
    ]

    variation_list = [
        {
            "id": var["id"],
            "attributes": _get_safe_options(var.get("attributes", [])),
        }
        for var in variations
    ]

    conversation.flow_state = FlowState.AWAITING_BULK_VARIANT_SELECTION.value

    # If the user named a variant we couldn't match (e.g. "Aurora Taupe" when
    # Aurora has no Taupe), say so explicitly and show what IS available —
    # otherwise the generic "select the missing details" prompt looks like we
    # ignored what they typed.
    _bad_hint = (line.get("unmatched_variant_hint") or "").strip()
    _hint_not_in_catalog = bool(_bad_hint) and not any(
        _bad_hint.lower() in opt.lower()
        for opts in attr_axes.values() for opt in opts
    )

    # ▼ CHANGED: wrap data inside "payload" key
    action = {
        "type": "SHOW_BULK_VARIANT_PROMPT",
        "payload": {
            "line_index": line_idx,
            "company": line.get("customer_display_name", ""),
            "is_self_order": line.get("is_self_order", False),
            "product_name": line.get("product_name", ""),
            "quantity": line.get("quantity", 0),
            "progress": {"current": pos + 1, "total": len(needs_variant_indices)},
            "attributes": attributes,
            "variations": variation_list,
            "unmatched_variant_hint": _bad_hint if _hint_not_in_catalog else "",
        },
    }

    _line_label = (
        f"{line.get('customer_display_name', '')} × {line.get('quantity', 0) or '?'}"
    )
    if _hint_not_in_catalog:
        _bot_message = (
            f"I couldn't find **{_bad_hint}** for **{line['product_name']}** — "
            f"that option isn't in the catalog. Please pick from the available "
            f"options instead ({_line_label}):"
        )
    else:
        _bot_message = (
            f"Please select the missing product details for "
            f"**{line['product_name']}** ({_line_label}):"
        )

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": _bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Cancel"],
        "actions": [action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Private: _build_bulk_confirmation_response ──
# ══════════════════════════════════════════════════════════════

def _build_bulk_confirmation_response(lines_as_dicts, conversation, user_context, page, start_time):
    resolved_count = sum(1 for l in lines_as_dicts if not l.get("unresolved"))
    unresolved_count = len(lines_as_dicts) - resolved_count
    
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

    # wrap data inside "payload" key
    action = {
        "type": "SHOW_BULK_ORDER_CONFIRMATION",
        "payload": {
            "lines": lines_as_dicts,
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
        },
    }

    bot_message = f"Here's your bulk order — {resolved_count} order(s) ready to place."
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
# ── Public: handle_bulk_variant_selection_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_variant_selection_reply(
    message, store_loader, conversation, user_context, page, start_time
):
    """
    Called when the user replies during AWAITING_BULK_VARIANT_SELECTION.
    Scores each variation by how many of its attribute options appear in the
    message, stamps the best match as variation_id, then advances.
    Re-prompts via _ask_for_bulk_variant if no match is found.
    """
    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    needs_variant_indices = user_context.get("bulk_variant_line_indices", [])
    pos = user_context.get("bulk_variant_current_pos", 0)

    # Guard — shouldn't be here
    if not needs_variant_indices or pos >= len(needs_variant_indices):
        return _build_bulk_confirmation_response(
            lines_as_dicts, conversation, user_context, page, start_time
        )

    line_idx = needs_variant_indices[pos]
    line = lines_as_dicts[line_idx]
    product_id = line["product_id"]
    cache = user_context.get("bulk_variant_cache", {})
    variations = cache.get(str(product_id), [])

    # Score: count how many attribute options from a variation appear in the message
    msg_lower = message.lower()
    best_match = None
    best_score = -1

    for var in variations:
        attrs = var.get("attributes", [])
        if not attrs:
            continue
        score = sum(
            1 for a in attrs if a.get("option", "").lower() in msg_lower
        )
        if score > best_score:
            best_score = score
            best_match = var

    if not best_match or best_score == 0:
        # Re-show the same prompt
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, pos,
            conversation, user_context, page, start_time,
        )

    # Stamp the resolved variation
    import re as _re

    # Record the rep's choice for any axis the variation itself can't encode
    # ("Any" axes). These ride along as order line-item meta — exactly where
    # WooCommerce puts an "Any" attribute chosen on the product page.
    _pending_axes = line.get("blank_variant_axes") or []
    if _pending_axes:
        _opts = _parent_any_axis_options(product_id, _pending_axes, user_context)
        _meta = dict(line.get("variant_meta") or {})
        for _axis, _choices in _opts.items():
            for _choice in sorted(_choices, key=len, reverse=True):
                if _choice.lower() in msg_lower:
                    _meta[_axis] = _choice
                    break
        line["variant_meta"] = _meta
        line["blank_variant_axes"] = [
            a for a in _pending_axes
            if not any(a.strip().lower() == k.strip().lower() for k in _meta)
        ]
        logger.info(
            f"bulk_order | variant meta for line {line_idx} → {_meta} | "
            f"still pending={line['blank_variant_axes']}"
        )

    line["variation_id"] = best_match["id"]
    line["unresolved"] = False
    line["unresolved_reason"] = None
    # The user has now chosen a real option — don't keep warning about the
    # hint that failed, or a re-prompt later would repeat a stale complaint.
    line["unmatched_variant_hint"] = ""

    # If quantity was not specified earlier, extract the last standalone
    # integer from the user's reply. Attribute options like "12x24" don't
    # produce bare word-boundary integers, so the last match is the qty.
    if not line.get("quantity") or not line.get("quantity_explicitly_set"):
        qty_matches = _re.findall(r'\b(\d+)\b', message)
        if qty_matches:
            line["quantity"] = int(qty_matches[-1])
            line["quantity_explicitly_set"] = True   # ← ADD
    
    lines_as_dicts[line_idx] = line
    user_context["pending_bulk_lines"] = lines_as_dicts

    next_pos = pos + 1
    user_context["bulk_variant_current_pos"] = next_pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    if next_pos < len(needs_variant_indices):
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, next_pos,
            conversation, user_context, page, start_time,
        )

    # All variants resolved — show structured confirmation table
    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_email_reply ──
# ══════════════════════════════════════════════════════════════

def _build_recipient_queue(lines_as_dicts, split_unnamed: bool = False) -> list:
    """
    One slot per DISTINCT unresolved recipient.

    Lines naming the same person share a slot (one answer settles them).
    Lines naming NOBODY are ambiguous — "Order Harmony Moon, Adams Grey at
    Beck" could be one person taking both or two people taking one each — so
    the rep is asked which, and split_unnamed carries that answer:

        False -> all unnamed lines share one slot (one question, one person)
        True  -> each unnamed line gets its own slot (asked separately)

    Returns [] when every line already has a customer, which is the signal to
    skip the prompt entirely.
    """
    slots: dict = {}
    for idx, l in enumerate(lines_as_dicts):
        if l.get("unresolved_reason") not in ("recipient_required", "recipient_not_found"):
            continue
        raw = (l.get("recipient_name") or "").strip()
        key = re.sub(r'[^a-z0-9]+', ' ', raw.lower()).strip()
        if not key and split_unnamed:
            key = f"__line_{idx}"   # unique per line -> one question each
        slot = slots.setdefault(key, {"name": raw, "line_indices": []})
        slot["line_indices"].append(idx)
    return list(slots.values())


# Button labels for the "one person or several?" step. Defined once so the
# prompt and the reply matcher can never drift — a mismatch here would make a
# tapped button fall through to the keyword fallback, or worse, re-ask forever.
RECIPIENT_MODE_SAME = "Same person"
RECIPIENT_MODE_DIFFERENT = "Different people"


def _unnamed_multi_slot(queue) -> bool:
    """True when the queue has an unnamed slot covering more than one line."""
    return any(not s["name"] and len(s["line_indices"]) > 1 for s in queue)


def _ask_recipient_mode(
    lines_as_dicts, queue, conversation, user_context, page, start_time,
):
    """
    Ask whether the unnamed lines all go to ONE person or to different people.

    Only reached when more than one line named nobody — with a single line
    there is nothing to disambiguate. Guessing either way silently misassigns
    goods, so the rep is asked once and the answer shapes the queue.
    """
    slot = next(s for s in queue if not s["name"] and len(s["line_indices"]) > 1)
    scope = user_context.get("bulk_company_scope", "")

    conversation.flow_state = FlowState.AWAITING_BULK_RECIPIENT_MODE.value
    user_context["bulk_recipient_queue"] = queue
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    product_lines = "\n".join(
        f"\u2022 **{lines_as_dicts[i]['quantity']}\u00d7 {lines_as_dicts[i]['product_name']}**"
        for i in slot["line_indices"]
    )

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            f"{product_lines}\n\nAre these all for the same person at "
            f"**{scope}**, or for different people?"
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": [RECIPIENT_MODE_SAME, RECIPIENT_MODE_DIFFERENT, "Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT_MODE.value,
            "company": scope,
            "line_count": len(slot["line_indices"]),
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_RECIPIENT_MODE.value,
        "pagination": default_pagination(page),
    }), 200


def handle_bulk_recipient_mode_reply(
    message, store_loader, conversation, user_context, page, start_time
):
    """
    Called during AWAITING_BULK_RECIPIENT_MODE.

    "Same person"     -> unnamed lines stay in one slot, asked once.
    "Different people" -> each unnamed line becomes its own slot.
    Anything unrecognised re-asks rather than guessing.
    """
    reply = (message or "").strip().lower()
    lines = user_context.get("pending_bulk_lines", [])

    # Exact button label first — this is the intended path, and it is decided
    # without interpreting anything. Free text is only a fallback for reps who
    # type instead of tapping.
    if reply == RECIPIENT_MODE_SAME.lower():
        _same, _diff = True, False
    elif reply == RECIPIENT_MODE_DIFFERENT.lower():
        _same, _diff = False, True
    else:
        _same = any(w in reply for w in ("same", "single", "one person", "1 person"))
        _diff = any(w in reply for w in ("different", "separate", "multiple", "each", "various"))

    if _same == _diff:   # both or neither matched -> ambiguous, ask again
        queue = user_context.get("bulk_recipient_queue") or _build_recipient_queue(lines)
        return _ask_recipient_mode(
            lines, queue, conversation, user_context, page, start_time
        )

    mode = "same" if _same else "different"
    user_context["bulk_recipient_mode"] = mode

    queue = _build_recipient_queue(lines, split_unnamed=(mode == "different"))
    user_context["bulk_recipient_queue"] = queue
    user_context["bulk_recipient_pos"] = 0
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    logger.info(
        f"bulk_order | recipient mode = {mode!r} -> {len(queue)} question(s)"
    )

    if not queue:
        return _continue_after_slots_filled(
            lines, store_loader, conversation, user_context, page, start_time
        )

    return _ask_for_bulk_recipient(
        lines, queue, 0, conversation, user_context, page, start_time
    )


def _roster_label(entry: dict, disambiguate: bool = False) -> str:
    """
    Picker label for one roster entry.

    Plain name normally. When two entries share a name — the same person on
    file at two of the company's sites — the name alone is useless, so the
    city (or street, or email) is appended. Without this the rep sees two
    identical buttons and cannot tell which address they are choosing.
    """
    name = (entry.get("display") or "").strip()
    if not disambiguate:
        return name
    detail = (
        entry.get("city")
        or entry.get("address_1")
        or entry.get("email")
        or ""
    ).strip()
    state = (entry.get("state") or "").strip()
    if detail and state and detail != state:
        detail = f"{detail}, {state}"
    return f"{name} \u2014 {detail}" if detail else name


def _recipient_candidates(slot, roster):
    """
    Roster entries to offer for this slot, plus whether to disambiguate.

    For an ambiguous name only the entries matching THAT name are offered —
    showing the whole company would bury the actual choice. Labels are
    disambiguated whenever two offered entries share a display name.
    """
    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    needle = _norm(slot.get("name"))
    entries = roster
    if needle:
        matched = [
            r for r in roster
            if _norm(r.get("display")) == needle
            or needle in _norm(r.get("display")).split()
        ]
        if len(matched) > 1:
            entries = matched

    counts = {}
    for r in entries:
        k = _norm(r.get("display"))
        counts[k] = counts.get(k, 0) + 1
    ambiguous = any(c > 1 for c in counts.values())
    return entries, ambiguous


def _ask_for_bulk_recipient(
    lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
):
    """Prompt for the recipient of one slot in the queue."""
    slot   = queue[pos]
    roster = user_context.get("bulk_company_roster", []) or []
    scope  = user_context.get("bulk_company_scope", "")

    entries, _ambiguous = _recipient_candidates(slot, roster)
    names = [_roster_label(r, _ambiguous) for r in entries if r.get("display")]

    conversation.flow_state = FlowState.AWAITING_BULK_RECIPIENT.value
    user_context["bulk_recipient_queue"] = queue
    user_context["bulk_recipient_pos"] = pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    product_lines = "\r\n".join(
        f"• **{lines_as_dicts[i]['quantity']}× {lines_as_dicts[i]['product_name']}**"
        for i in slot["line_indices"]
    )

    if slot["name"] and _ambiguous:
        _ask = (
            f"There are {len(entries)} people named **{slot['name']}** at "
            f"**{scope}**, at different addresses. Which one?"
        )
    elif slot["name"]:
        _ask = (
            f"I couldn't find **{slot['name']}** at **{scope}**. "
            "Who should this go to?"
        )
    else:
        _ask = f"**{scope}** has {len(names)} contacts. Who should this go to?"

    _progress = f" ({pos + 1} of {len(queue)})" if len(queue) > 1 else ""

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": f"{product_lines}\r\n\r\n{_ask}{_progress}",
        "intent": "guided_flow",
        "products": [],
        "suggestions": names[:8] + ["Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
            "company": scope,
            "candidates": names,
            "requested_name": slot["name"],
            "progress": {"current": pos + 1, "total": len(queue)},
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
        "pagination": default_pagination(page),
    }), 200


def handle_bulk_recipient_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_RECIPIENT when the rep names a person.

    Applies the pick to the CURRENT queue slot only, then advances. Stamping
    every unresolved line at once would silently ship Claire's product to
    Ashlynn whenever the two names failed together.
    """
    choice = (message or "").strip()
    roster = user_context.get("bulk_company_roster", []) or []
    scope  = user_context.get("bulk_company_scope", "")
    lines  = user_context.get("pending_bulk_lines", [])
    queue  = user_context.get("bulk_recipient_queue", []) or []
    pos    = user_context.get("bulk_recipient_pos", 0)

    # Queue lost (e.g. resumed session) — rebuild from the lines themselves.
    if not queue or pos >= len(queue):
        queue = _build_recipient_queue(
            lines,
            split_unnamed=(user_context.get("bulk_recipient_mode") == "different"),
        )
        pos = 0
        if not queue:
            return _continue_after_slots_filled(
                lines, store_loader, conversation, user_context, page, start_time
            )

    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    needle = _norm(choice)
    picked = None
    # Match against the labels actually offered for THIS slot. For a
    # duplicate name those read "Raj Chanda — Dallas, TX", so matching on
    # the bare display name would be ambiguous all over again and would
    # silently take the first record — the exact bug this step exists to
    # prevent.
    slot_for_match = queue[pos] if pos < len(queue) else {"name": ""}
    entries, _ambiguous = _recipient_candidates(slot_for_match, roster)

    def _only(pred):
        """The single entry satisfying pred, or None if 0 or 2+ do.

        Correctness here depends on the MATCH being unique, not on the
        roster being unique: "Elizabeth Rhodes" is an unambiguous answer
        even when two unrelated Raj Chandas sit in the same list. Gating on
        a roster-wide ambiguity flag instead would reject perfectly clear
        replies.
        """
        hits = [r for r in entries if pred(r)]
        return hits[0] if len(hits) == 1 else None

    if needle:
        # 1. exact label as offered ("Raj Chanda — Dallas, TX")
        picked = _only(lambda r: _norm(_roster_label(r, _ambiguous)) == needle)
        # 2. exact display name, only if it identifies one person
        if not picked:
            picked = _only(lambda r: _norm(r.get("display")) == needle)
        # 3. partial (first/last name), again only if unique
        if not picked:
            def _loose(r):
                hay = _norm(r.get("display"))
                return bool(hay) and (needle in hay or any(needle == w for w in hay.split()))
            picked = _only(_loose)

    if not picked:
        names = [_roster_label(r, _ambiguous) for r in entries if r.get("display")]
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't match **{choice}** to anyone at **{scope}**."
                if choice else f"Who should this go to at **{scope}**?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": names[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
                "candidates": names,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
            "pagination": default_pagination(page),
        }), 200

    shipping = picked.get("shipping") or {}
    if not shipping.get("address_1"):
        shipping = picked.get("billing") or {}

    slot = queue[pos]
    for idx in slot["line_indices"]:
        if idx >= len(lines):
            continue
        line = lines[idx]
        line["customer_id"]           = picked["id"]
        line["customer_display_name"] = picked["display"]
        line["recipient_name"]        = picked["display"]
        line["shipping_address"]      = shipping
        line["billing_address"]       = picked.get("billing") or {}
        line["is_self_order"]         = False
        line["unresolved"]            = False
        line["unresolved_reason"]     = None

    user_context["pending_bulk_lines"] = lines
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    logger.info(
        f"bulk_order | recipient '{picked['display']}' (id={picked['id']}) "
        f"applied to line(s) {slot['line_indices']} "
        f"(slot {pos + 1}/{len(queue)}, asked for '{slot['name']}') "
        f"for company '{scope}'"
    )

    # More people still to identify?
    if pos + 1 < len(queue):
        return _ask_for_bulk_recipient(
            lines, queue, pos + 1,
            conversation, user_context, page, start_time,
        )

    user_context.pop("bulk_recipient_queue", None)
    user_context.pop("bulk_recipient_pos", None)
    user_context.pop("bulk_recipient_mode", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    return _continue_after_slots_filled(
        lines, store_loader, conversation, user_context, page, start_time
    )


def handle_bulk_company_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_COMPANY when the rep names the company.

    The company scopes the entire transaction, so rather than patching
    individual lines we re-run the original utterance with the company
    appended — one code path for resolution instead of two.
    """
    company = (message or "").strip().strip('.,')
    if not company:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "Please provide the company name for this order.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    lines = user_context.get("pending_bulk_lines", [])
    original = user_context.get("pending_bulk_utterance", "") or " , ".join(
        l.get("raw_fragment", "") for l in lines if l.get("raw_fragment")
    )

    replay = f"{original} for company {company}"
    logger.info(f"bulk_order | company supplied → replaying with scope '{company}'")

    return handle_bulk_order_input(
        replay, store_loader, conversation, user_context, page, start_time
    )


def handle_bulk_email_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_EMAIL when the rep provides customer email(s).
    Extracts email(s), resolves customers via API, stamps the pending lines,
    then resumes the normal bulk flow (variant selection → confirmation).
    """
    import re as _re
    _EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', _re.I)

    emails = _EMAIL_RE.findall(message)
    if not emails:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "I couldn't find a valid email address. Please provide the customer's email.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
            "pagination": default_pagination(page),
        }), 200

    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    missing_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if l.get("unresolved_reason") == "email_not_provided"
    ]

    # Multiple emails + single unresolved line → clone the line for each email
    if len(emails) > 1 and len(missing_indices) == 1:
        base_idx = missing_indices[0]
        base = lines_as_dicts[base_idx]
        clones = [{**base, "email": email} for email in emails]
        lines_as_dicts = lines_as_dicts[:base_idx] + clones + lines_as_dicts[base_idx + 1:]
        missing_indices = list(range(base_idx, base_idx + len(emails)))
    else:
        # Assign emails round-robin to unresolved lines
        for i, line_idx in enumerate(missing_indices):
            lines_as_dicts[line_idx]["email"] = emails[i % len(emails)]

    # Resolve each newly assigned email
    email_cache: dict = {}
    for line_idx in missing_indices:
        line = lines_as_dicts[line_idx]
        email = line.get("email", "")
        if not email:
            continue

        if email not in email_cache:
            call = endpoints.search_customers_by_email(
                email=email,
                per_page=1,
                description=f"Bulk email reply lookup: '{email}'",
            )
            result = woo_client.execute(call)
            customers = result.get("data", []) if result.get("success") else []
            if isinstance(customers, list) and customers:
                c = customers[0]
                company = c.get("company") or c.get("billing", {}).get("company", "")
                full_name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
                billing = c.get("billing", {}) or {}
                shipping = c.get("shipping", {}) or {}
                if not shipping.get("address_1"):
                    shipping = billing
                email_cache[email] = {
                    "id": str(c["id"]),
                    "display": company or full_name or f"Customer #{c['id']}",
                    "billing": billing,
                    "shipping": shipping,
                }
            else:
                email_cache[email] = None

        resolution = email_cache.get(email)
        if resolution:
            line["customer_id"] = resolution["id"]
            line["customer_display_name"] = resolution["display"]
            line["billing_address"] = resolution["billing"]
            line["shipping_address"] = resolution["shipping"]
            line["unresolved"] = line.get("product_id") is None
            line["unresolved_reason"] = "product_not_found" if line["unresolved"] else None
        else:
            line["unresolved"] = True
            line["unresolved_reason"] = "email_not_found"
            line["customer_display_name"] = "⚠️ Not found"

    user_context["pending_bulk_lines"] = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    
    blank_after_email = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("product_name", "").strip()
    ]
    if blank_after_email:
        user_context["bulk_product_missing_indices"] = blank_after_email
        user_context["bulk_product_current_pos"] = 0
        conversation.flow_state = FlowState.AWAITING_BULK_PRODUCT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        first_line = lines_as_dicts[blank_after_email[0]]
        customer_hint = (
            "" if first_line.get("is_self_order")
            else f" for **{first_line['customer_display_name']}**"
            if first_line.get("customer_id") else ""
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": f"What product and quantity would you like to order{customer_hint}?",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pagination": default_pagination(page),
        }), 200
        
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )

    # Check for variable products still needing variant selection
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # See the note at the first gate: a matched variation can still leave
        # axes as "Any", which the rep must still choose.
        if l.get("product_id")
        and (not l.get("variation_id") or l.get("blank_variant_axes"))
        and _is_variable_product(l["product_id"], store_loader)
    ]

    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"] = 0
        user_context["bulk_variant_cache"] = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

# ══════════════════════════════════════════════════════════════
# ── Public: handle_product_reorder ──
# ══════════════════════════════════════════════════════════════

def handle_product_reorder(payload, store_loader, conversation, user_context, page, start_time):
    """
    Called when a rep clicks 'Reorder' on the product order history card.

    Payload shape (from _format_product_orders_for_action):
        {
            "order_id":              "12345",
            "order_number":          "12345",
            "customer_id":           "272749971",
            "customer_display_name": "Abel Design Group",
            "items": [
                {"product_name": "Harmony White", "product_id": 16972,
                 "variation_id": 0, "quantity": 20}
            ]
        }

    Builds BulkOrderLine dicts and drops straight into address confirmation —
    no variant or quantity prompts needed (those are already known from the
    original order).
    """
    customer_id      = payload.get("customer_id", "")
    customer_display = payload.get("customer_display_name", "")
    order_id         = payload.get("order_id", "")
    items            = payload.get("items", [])

    if not customer_id or not items:
        logger.warning(f"handle_product_reorder | missing customer_id or items | payload={payload}")
        return None

    # ── Fetch fresh billing/shipping address for this customer ──
    billing  = {}
    shipping = {}
    try:
        cust_call   = endpoints.fetch_customer(
            customer_id=int(customer_id),
            description=f"Fetch address for reorder — {customer_display}",
        )
        cust_result = woo_client.execute(cust_call)
        if cust_result.get("success") and isinstance(cust_result.get("data"), dict):
            data     = cust_result["data"]
            billing  = data.get("billing",  {}) or {}
            shipping = data.get("shipping", {}) or {}
            if not shipping.get("address_1"):
                shipping = billing
    except Exception as exc:
        logger.warning(f"handle_product_reorder | address fetch failed | error={exc}")

    # ── Build one BulkOrderLine per line item ──
    lines_as_dicts = []
    for item in items:
        product_id   = item.get("product_id")
        variation_id = item.get("variation_id") or None
        if not product_id:
            continue
        lines_as_dicts.append({
            "raw_fragment":            "",
            "company_name":            customer_display,
            "email":                   "",
            "product_name":            item.get("product_name", ""),
            "quantity":                item.get("quantity", 1),
            "quantity_explicitly_set": True,
            "product_id":              product_id,
            "variation_id":            variation_id,
            "customer_id":             str(customer_id),
            "customer_display_name":   customer_display,
            "shipping_address":        shipping,
            "billing_address":         billing,
            "is_reorder":              True,
            "reorder_source_order_id": int(order_id) if order_id else None,
            "unresolved":              False,
            "unresolved_reason":       None,
            "address_confirmed":       False,
            "address_skipped":         False,
        })

    if not lines_as_dicts:
        return None

    # ── Store state and go straight to address confirmation ──
    user_context["pending_bulk_lines"]     = lines_as_dicts
    # Reorder is a separate entry point into the bulk flow, so it needs the
    # logged-in user's billing cached too.
    _rep_billing_address(conversation, user_context)
    user_context["bulk_current_line_index"] = 0
    user_context["bulk_confirmed_lines"]   = []
    user_context["bulk_address_overrides"] = {}
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    logger.info(
        f"handle_product_reorder | order_id={order_id} | customer={customer_display} "
        f"| {len(lines_as_dicts)} line(s)"
    )

    return _advance_to_next_address_confirmation(
        resolved_lines=lines_as_dicts,
        idx=0,
        conversation=conversation,
        user_context=user_context,
        page=page,
        start_time=start_time,
    )
    
# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_product_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_product_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_PRODUCT when the rep provides a product name
    (and optional quantity) for a line that had a blank product.

    After filling the product, resumes the normal pipeline:
      blank product → email missing → variant selection → confirmation
    """
    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    missing_indices = user_context.get("bulk_product_missing_indices", [])
    pos = user_context.get("bulk_product_current_pos", 0)
    role = user_context.get("role", "")

    # Guard
    if not missing_indices or pos >= len(missing_indices):
        return _continue_after_slots_filled(lines_as_dicts, store_loader, conversation, user_context, page, start_time)

    line_idx = missing_indices[pos]
    line = lines_as_dicts[line_idx]

    # ── Parse quantity from the reply ──
    qty_match = re.search(r'\b(\d+)\b', message)
    if qty_match:
        line["quantity"] = int(qty_match.group(1))
        # Strip quantity from the product portion
        product_text = (message[:qty_match.start()] + message[qty_match.end():]).strip().strip(".,- ")
    else:
        product_text = message.strip().strip(".,- ")

    # ── Resolve product against catalog ──
    resolved_id = None
    resolved_name = product_text

    if store_loader and product_text:
        products = store_loader.products or []

        # Exact match
        for p in products:
            if p.get("name", "").lower() == product_text.lower():
                resolved_id = p["id"]
                resolved_name = p["name"]
                break

        # First-word match
        if resolved_id is None:
            first_word = product_text.split()[0] if product_text.split() else ""
            for p in products:
                if p.get("name", "").lower() == first_word.lower():
                    resolved_id = p["id"]
                    resolved_name = p["name"]
                    break

        # Fuzzy fallback
        if resolved_id is None:
            product_names = [p.get("name", "") for p in products]
            close = difflib.get_close_matches(product_text, product_names, n=1, cutoff=0.6)
            if close:
                resolved_name = close[0]
                resolved_id = next(
                    (p["id"] for p in products if p.get("name") == resolved_name), None
                )

    line["product_name"] = resolved_name
    line["product_id"] = resolved_id

    # Recompute unresolved state
    customer_missing = line.get("customer_id") is None
    if resolved_id:
        line["unresolved"] = customer_missing
        if customer_missing:
            line["unresolved_reason"] = "email_not_provided" if not line.get("email") else "email_not_found"
        else:
            line["unresolved_reason"] = None
    else:
        line["unresolved"] = True
        line["unresolved_reason"] = "product_not_found"

    lines_as_dicts[line_idx] = line
    next_pos = pos + 1
    user_context["bulk_product_current_pos"] = next_pos
    user_context["pending_bulk_lines"] = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # More blank products in this batch?
    if next_pos < len(missing_indices):
        next_idx = missing_indices[next_pos]
        next_line = lines_as_dicts[next_idx]
        customer_hint = (
            "" if first_line.get("is_self_order")
            else f" for **{first_line['customer_display_name']}**"
            if first_line.get("customer_id") else ""
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": f"What product and quantity would you like to order{customer_hint}?",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pagination": default_pagination(page),
        }), 200

    # All blank products filled — continue pipeline
    return _continue_after_slots_filled(
        lines_as_dicts, store_loader, conversation, user_context, page, start_time
    )


def _continue_after_slots_filled(lines_as_dicts, store_loader, conversation, user_context, page, start_time):
    """
    Shared exit point after all blank-product slots are filled.
    Cleans up product-tracking keys, then checks for missing emails,
    variable products, and finally builds the confirmation response.
    """
    role = user_context.get("role", "")

    # Clean up product-slot tracking
    user_context.pop("bulk_product_missing_indices", None)
    user_context.pop("bulk_product_current_pos", None)
    
    # Quantity unset on any resolved line?       ← ADD
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )

    # Email still missing on any line?
    if role in BULK_ORDER_ROLES:
        email_missing = [l for l in lines_as_dicts if l.get("unresolved_reason") == "email_not_provided"]
        if email_missing:
            conversation.flow_state = FlowState.AWAITING_BULK_EMAIL.value
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            product_lines = "\r\n".join(
                f"• **{l['quantity']}× {l['product_name']}**" for l in email_missing
            )
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Got it:\r\n{product_lines}\r\n\r\n"
                    "Please provide the customer's email address."
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Cancel"],
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                    "response_time_ms": elapsed,
                },
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "pagination": default_pagination(page),
            }), 200

    # Variable products needing variant selection?
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # See the note at the first gate: a matched variation can still leave
        # axes as "Any", which the rep must still choose.
        if l.get("product_id")
        and (not l.get("variation_id") or l.get("blank_variant_axes"))
        and _is_variable_product(l["product_id"], store_loader)
    ]
    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"] = 0
        user_context["bulk_variant_cache"] = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

# ══════════════════════════════════════════════════════════════
# ── Private: _prompt_for_quantity ──
# ══════════════════════════════════════════════════════════════

def _prompt_for_quantity(qty_unset, lines_as_dicts, conversation, user_context, page, start_time):
    """
    Store the pending indices, set AWAITING_BULK_QUANTITY, and ask for
    the quantity of the first unset line.
    """
    user_context["bulk_quantity_pending_indices"] = qty_unset
    user_context["bulk_quantity_current_pos"]     = 0
    conversation.flow_state  = FlowState.AWAITING_BULK_QUANTITY.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    line = lines_as_dicts[qty_unset[0]]
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success":     True,
        "bot_message": _build_quantity_prompt(line),
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": ["1", "5", "10", "15", "20", "Cancel"],
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
            "response_time_ms": elapsed,
        },
        "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
        "pagination":  default_pagination(page),
    }), 200

def _build_quantity_prompt(line: dict) -> str:
    product = line['product_name']
    customer = line['customer_display_name']
    if line.get('is_self_order'):
        return f"How many **{product}** shall I order?"
    return f"How many **{product}** for **{customer}**?"

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_quantity_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_quantity_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_QUANTITY when the rep specifies a quantity.
    Stamps the quantity, advances to the next unset line or continues pipeline.
    """
    import re as _re

    lines_as_dicts   = user_context.get("pending_bulk_lines", [])
    pending_indices  = user_context.get("bulk_quantity_pending_indices", [])
    pos              = user_context.get("bulk_quantity_current_pos", 0)

    if not pending_indices or pos >= len(pending_indices):
        return _continue_after_quantity_filled(
            lines_as_dicts, store_loader, conversation, user_context, page, start_time
        )

    line_idx = pending_indices[pos]
    line     = lines_as_dicts[line_idx]

    qty_match = _re.search(r'\b(\d+)\b', message)
    if not qty_match:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": (
                f"Please enter a quantity for **{line['product_name']}** "
                f"(e.g. 1, 5, 10):"
            ),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["1", "5", "10", "15", "20", "Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
            "pagination":  default_pagination(page),
        }), 200

    line["quantity"]              = int(qty_match.group(1))
    line["quantity_explicitly_set"] = True
    lines_as_dicts[line_idx]     = line

    next_pos = pos + 1
    user_context["bulk_quantity_current_pos"] = next_pos
    user_context["pending_bulk_lines"]        = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # More lines still need a quantity?
    if next_pos < len(pending_indices):
        next_line = lines_as_dicts[pending_indices[next_pos]]
        elapsed   = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": _build_quantity_prompt(next_line),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["1", "5", "10", "15", "20", "Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
            "pagination":  default_pagination(page),
        }), 200

    return _continue_after_quantity_filled(
        lines_as_dicts, store_loader, conversation, user_context, page, start_time
    )


def _continue_after_quantity_filled(lines_as_dicts, store_loader, conversation, user_context, page, start_time):
    """
    Exit point after all quantity slots are filled.
    Cleans up quantity-tracking keys then continues to variant selection or confirmation.
    """
    user_context.pop("bulk_quantity_pending_indices", None)
    user_context.pop("bulk_quantity_current_pos", None)

    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # See the note at the first gate: a matched variation can still leave
        # axes as "Any", which the rep must still choose.
        if l.get("product_id")
        and (not l.get("variation_id") or l.get("blank_variant_axes"))
        and _is_variable_product(l["product_id"], store_loader)
    ]
    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"]  = 0
        user_context["bulk_variant_cache"]        = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )
    
# ══════════════════════════════════════════════════════════════
# ── Function 4: handle_bulk_order_confirmation ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_confirmation(user_context, conversation, page, start_time):
    """
    Called when the rep confirms the bulk order table (action == "confirm_bulk_order").
    Begins the per-customer address confirmation loop.
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

    return _advance_to_next_address_confirmation(
        resolved_lines=resolved_lines,
        idx=0,
        conversation=conversation,
        user_context=user_context,
        page=page,
        start_time=start_time,
    )


# ══════════════════════════════════════════════════════════════
# ── Function 5: handle_bulk_address_confirmation_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_address_confirmation_reply(action, message, conversation, user_context, page, start_time):
    """
    Routes address-step actions during AWAITING_BULK_ADDRESS_CONFIRMATION.
    action is one of:
        "bulk_address_confirmed"         — use the address shown
        "bulk_address_change"            — user wants to type a different address (legacy)
        "bulk_address_override_text"     — user has typed a new address (legacy)
        "bulk_address_override_structured" — user saved edited billing+shipping via the panel
        "bulk_address_skip"              — skip this order entirely
    Returns a Flask response.
    """
    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]
    idx = user_context.get("bulk_current_line_index", 0)

    # Step 1: All lines already processed
    if idx >= len(resolved_lines):
        return _create_all_confirmed_orders(user_context, conversation, page, start_time)

    current_line = resolved_lines[idx]

    # Step 3: Address confirmed — use address as-is
    if action == "bulk_address_confirmed":
        # ── Validation gate ──
        # POST /wc/v3/orders performs no address validation of its own, so this
        # is the only thing standing between "Confirm" on a card reading "No
        # address on file" and a live order with an empty billing block.
        billing, shipping = _effective_address_for_line(
            current_line,
            user_context.get("bulk_address_overrides", {}),
            idx,
            user_context.get("rep_email", ""),
            user_context.get("rep_billing_address"),
        )
        errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(errors):
            return _reprompt_address_with_errors(
                resolved_lines, idx, conversation, user_context, page, start_time, errors,
            )

        current_line["address_confirmed"] = True
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 3b: Structured save from the inline edit panel.
    # message is "__BULK_ADDR__<json>" with {"billing": {...}, "shipping": {...}}.
    # Override is keyed by line index so repeated companies stay independent.
    elif action == "bulk_address_override_structured":
        raw = message.strip()
        if raw.startswith("__BULK_ADDR__"):
            raw = raw[len("__BULK_ADDR__"):]
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError) as exc:
            logger.warning(
                f"bulk_address_override_structured | bad JSON | error={exc} | raw={raw[:200]!r}"
            )
            parsed = {}

        edited_billing = parsed.get("billing") or {}
        edited_shipping = parsed.get("shipping") or {}

        # Persist the override BEFORE validating, so a rejected save doesn't
        # throw away what the rep just typed — the re-prompt prefills from the
        # effective address, which reads through this override.
        overrides = user_context.setdefault("bulk_address_overrides", {})
        overrides[str(idx)] = {
            "billing": edited_billing,
            "shipping": edited_shipping,
        }
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        # ── Validation gate ──
        billing, shipping = _effective_address_for_line(
            current_line, overrides, idx, user_context.get("rep_email", ""),
            user_context.get("rep_billing_address"),
        )
        errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(errors):
            return _reprompt_address_with_errors(
                resolved_lines, idx, conversation, user_context, page, start_time, errors,
            )

        current_line["address_confirmed"] = True
        user_context.pop("bulk_awaiting_address_text", None)
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 4: Rep wants to change the address — re-show the card so the inline
    # edit panel is the entry point.
    #
    # This used to set bulk_awaiting_address_text and ask the rep to type a
    # free-text address. That path could not produce a valid address by
    # construction — it wrote the whole typed string into address_1 and left
    # city/state/postcode/country empty — so with required-field validation in
    # place it would reject every time. The structured panel is now the only
    # edit surface.
    elif action == "bulk_address_change":
        user_context.pop("bulk_awaiting_address_text", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _build_address_card_response(
            resolved_lines, idx, conversation, user_context, page, start_time,
        )

    # Step 5: Legacy free-text override.
    #
    # Retired (see Step 4) but kept reachable so any session already in the
    # bulk_awaiting_address_text sub-state when this shipped can still complete
    # rather than dead-ending. It runs through the same validation gate as every
    # other path, so it cannot create a blank-address order; in practice it will
    # reject and route the rep to the panel.
    elif action == "bulk_address_override_text":
        user_context.pop("bulk_awaiting_address_text", None)
        overrides = user_context.setdefault("bulk_address_overrides", {})
        overrides[str(idx)] = {
            "shipping": {
                "address_1": message.strip(),
            },
        }
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        billing, shipping = _effective_address_for_line(
            current_line, overrides, idx, user_context.get("rep_email", ""),
            user_context.get("rep_billing_address"),
        )
        errors = validate_bulk_address(billing, shipping, get_required_fields())
        if has_errors(errors):
            return _reprompt_address_with_errors(
                resolved_lines, idx, conversation, user_context, page, start_time, errors,
            )

        current_line["address_confirmed"] = True
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 6: Skip this customer's order
    elif action == "bulk_address_skip":
        current_line["address_skipped"] = True
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Unexpected action — fall through safely
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Please reply **Yes** to confirm the address, **Change address** to update it, or **Skip** to skip this order.",
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "Change address", "Skip this order"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200


# ══════════════════════════════════════════════════════════════
# ── Function 6: _advance_to_next_address_confirmation (private) ──
# ══════════════════════════════════════════════════════════════

def _advance_to_next_address_confirmation(resolved_lines, idx, conversation, user_context, page, start_time):
    """
    Walk forward to the next line still needing address confirmation and show
    its card. When every line has been confirmed or skipped, place the orders.
    """
    # Step 1: Skip already-processed lines
    while idx < len(resolved_lines):
        line = resolved_lines[idx]
        if not line.get("address_confirmed") and not line.get("address_skipped"):
            break
        idx += 1

    if idx >= len(resolved_lines):
        return _create_all_confirmed_orders(user_context, conversation, page, start_time)

    return _build_address_card_response(
        resolved_lines, idx, conversation, user_context, page, start_time,
    )


def _reprompt_address_with_errors(
    resolved_lines, idx, conversation, user_context, page, start_time, errors
):
    """
    Re-show the SAME line's address card after validation rejected it.

    Deliberately does NOT advance bulk_current_line_index and does NOT set
    address_confirmed — the rep stays on this line until the address is valid or
    they skip it.
    """
    return _build_address_card_response(
        resolved_lines, idx, conversation, user_context, page, start_time,
        validation_errors=errors,
    )


def _build_address_card_response(
    resolved_lines, idx, conversation, user_context, page, start_time,
    validation_errors=None,
):
    """
    Build the SHOW_BULK_ADDRESS_CONFIRMATION card for resolved_lines[idx].

    Shared by the normal advance path and the validation re-prompt so the two
    can't drift. When `validation_errors` is supplied the card is rendered in
    its blocked form: the errors ride along in the payload, the bot message
    names what's missing, and "Yes, confirm" is REMOVED from the suggestion
    chips.

    That last part matters: conversation_flow.py maps any reply matching
    yes|yeah|confirm|ok|sure|correct to the bulk_address_confirmed action, so
    leaving the chip on screen would invite the rep straight back into the
    rejection they just hit.
    """
    current_line = resolved_lines[idx]
    user_context["bulk_current_line_index"] = idx
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value

    # The shipping block drives the read-only summary line on the card.
    shipping_block = current_line.get("shipping_address") or {}
    billing_block = current_line.get("billing_address") or {}

    if not shipping_block.get("address_1"):
        try:
            cust_call = endpoints.fetch_customer(
                customer_id=int(current_line["customer_id"]),
                description=f"Fetch address for {current_line['customer_display_name']}",
            )
            cust_result = woo_client.execute(cust_call)
            if cust_result.get("success") and isinstance(cust_result.get("data"), dict):
                data = cust_result["data"]
                fetched_billing = data.get("billing", {}) or {}
                fetched_shipping = data.get("shipping", {}) or {}
                # Shipping block drives this confirmation card; fall back to
                # billing if the customer has no shipping address on file.
                shipping_block = fetched_shipping if fetched_shipping.get("address_1") else fetched_billing
                current_line["shipping_address"] = shipping_block
                # Populate the billing block too, so order creation has both.
                if not billing_block.get("address_1"):
                    billing_block = fetched_billing or shipping_block
                    current_line["billing_address"] = billing_block
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
        except Exception as exc:
            logger.warning(
                f"_build_address_card_response | failed to fetch address "
                f"for customer_id={current_line.get('customer_id')} | error={exc}"
            )

    # Prefill from the EFFECTIVE address, not the raw base blocks, so a rep who
    # saved a partial edit and got rejected sees their own values back in the
    # panel instead of the original ones.
    effective_billing, effective_shipping = _effective_address_for_line(
        current_line,
        user_context.get("bulk_address_overrides", {}),
        idx,
        user_context.get("rep_email", ""),
        user_context.get("rep_billing_address"),
    )

    addr_parts = [
        effective_shipping.get("address_1", ""),
        effective_shipping.get("address_2", ""),
        effective_shipping.get("city", ""),
        effective_shipping.get("state", ""),
        effective_shipping.get("postcode", ""),
    ]
    addr_str = ", ".join(p for p in addr_parts if p) or "No address on file"

    items_text = f"{current_line['product_name']} ×{current_line['quantity']}"
    if current_line.get("is_reorder"):
        items_text = f"[Reorder] {items_text}"

    # Full field set so the inline edit panel can prefill every field.
    # Billing carries the CS custom fields; shipping carries order_notes.
    _BILLING_FIELDS = (
        "first_name", "last_name", "company",
        "billing_field_type", "billing_project",
        "address_1", "address_2", "city", "state", "postcode", "country",
        "phone", "email", "project_rep",
    )
    _SHIPPING_FIELDS = (
        "first_name", "last_name", "company",
        "address_1", "address_2", "city", "state", "postcode", "country",
        "order_notes",
    )

    def _pick(block, fields):
        return {f: (block or {}).get(f, "") for f in fields}

    billing_payload = _pick(effective_billing, _BILLING_FIELDS)
    shipping_payload = _pick(effective_shipping, _SHIPPING_FIELDS)

    # project_rep is already defaulted to the logged-in rep inside
    # _effective_address_for_line, so the dropdown arrives pre-selected.

    # ▼ emit a structured action so React can render the address card + panel
    payload = {
        "customer_name": current_line["customer_display_name"],
        "items_text": items_text,
        # Legacy read-only summary fields (kept for back-compat).
        "address": {
            "address_1": effective_shipping.get("address_1", ""),
            "address_2": effective_shipping.get("address_2", ""),
            "city":      effective_shipping.get("city", ""),
            "state":     effective_shipping.get("state", ""),
            "postcode":  effective_shipping.get("postcode", ""),
        },
        "addr_str": addr_str,
        # Full structured blocks for the editable panel prefill.
        "billing": billing_payload,
        "shipping": shipping_payload,
        "progress": {"current": idx + 1, "total": len(resolved_lines)},
    }
    if has_errors(validation_errors):
        payload["validation_errors"] = validation_errors

    address_action = {
        "type": "SHOW_BULK_ADDRESS_CONFIRMATION",
        "payload": payload,
    }

    header = (
        f"**Order for {current_line['customer_display_name']}** "
        if not current_line.get("is_self_order") else "**Your order** "
    ) + f"({idx + 1} of {len(resolved_lines)})\r\n\r\n"

    if has_errors(validation_errors):
        missing_count = count_missing(validation_errors)
        bot_message = (
            header
            + f"📦 {items_text}\r\n"
            + f"📍 Shipping to: {addr_str}\r\n\r\n"
            + f"⚠️ This order is missing {missing_count} required "
            + ("field" if missing_count == 1 else "fields")
            + f": {format_missing_fields(validation_errors)}.\r\n\r\n"
            + "Please update the address, or skip this order."
        )
        suggestions = ["Change address", "Skip this order"]
        logger.info(
            f"bulk_order | address validation blocked line {idx} "
            f"({current_line.get('customer_display_name')}) | "
            f"missing={format_missing_fields(validation_errors)}"
        )
    else:
        bot_message = (
            header
            + f"📦 {items_text}\r\n"
            + f"📍 Shipping to: {addr_str}\r\n\r\n"
            + "Confirm this address?"
        )
        suggestions = ["Yes, confirm", "Change address", "Skip this order"]

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestions,
        "actions": [address_action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Function 7: _create_all_confirmed_orders (private) ──
# ══════════════════════════════════════════════════════════════

def _bulk_line_item(line: dict) -> dict:
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
            {"key": str(axis), "value": str(value)}
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
    _groups: dict = {}

    for line_idx, line in enumerate(resolved_lines):
        if not line.get("address_confirmed"):
            continue

        _billing, _shipping = _effective_address_for_line(
            line, address_overrides, line_idx, rep_email,
            user_context.get("rep_billing_address"),
        )
        _key = (
            str(line.get("customer_id")),
            json.dumps(_billing, sort_keys=True, default=str),
            json.dumps(_shipping, sort_keys=True, default=str),
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

        # ── Custom CS fields → order meta (not address-block fields) ──
        # project_rep is already defaulted to the logged-in rep inside
        # _effective_address_for_line.
        project_rep  = billing.get("project_rep") or rep_email
        project_name = billing.get("billing_project") or ""
        field_type   = billing.get("billing_field_type") or ""
        order_notes  = shipping.get("order_notes") or ""

        meta_data = []
        if project_rep:
            meta_data.append({"key": "_billing_project_rep", "value": project_rep})
        if project_name:
            meta_data.append({"key": "_billing_project_name", "value": project_name})
        if field_type:
            meta_data.append({"key": "_billing_field_type", "value": field_type})

        # Remove custom keys from the Woo address blocks — they live in meta.
        for _k in ("project_rep", "billing_project", "billing_field_type", "order_notes"):
            billing.pop(_k, None)
            shipping.pop(_k, None)

        payload = {
            "status": "processing",
            "customer_id": line["customer_id"],
            "payment_method": DEFAULT_PAYMENT_METHOD,
            "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
            "set_paid": False,
            "line_items": [
                _bulk_line_item(gl) for gl in group_lines
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

        order_call = endpoints.create_order(
            payload=payload,
            description=f"Bulk order for {line['customer_display_name']}",
        )
        order_resp = woo_client.execute(order_call)

        if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
            new_order = order_resp["data"]
            created_orders.append({
                "order_number": new_order.get("number") or new_order.get("id"),
                "customer": line["customer_display_name"],
                # A merged order covers several products — name them all, or
                # the summary silently under-reports what was placed.
                "product": ", ".join(gl["product_name"] for gl in group_lines),
                "quantity": sum(int(gl.get("quantity") or 0) for gl in group_lines),
            })
            logger.info(
                f"bulk_order | created order #{new_order.get('number') or new_order.get('id')} "
                f"for {line['customer_display_name']} | {len(group_lines)} line item(s)"
            )
        else:
            failed_orders.append({
                "customer": line["customer_display_name"],
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
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value

    # Step 5: Build summary message
    bot_message = ""

    if created_orders:
        bot_message += f"✅ **{len(created_orders)} order(s) placed successfully:**\r\n\r\n"
        for o in created_orders:
            bot_message += f"• **#{o['order_number']}** — {o['customer']}: {o['product']} ×{o['quantity']}\r\n"

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