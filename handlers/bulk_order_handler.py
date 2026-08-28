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
    BULK_ORDER_FULL_SCOPE_ROLES,
    ECOMMERCE_BACKEND,
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
    is_known_rep,
    validate_bulk_address,
)

# Variant/axis helpers were split into handlers/bulk/variants.py.
# Imported back under their original names so every existing call site
# in this module (and chat.py's import of
# handle_bulk_variant_selection_reply) keeps working unchanged.
from handlers.bulk.variants import (
    _ask_for_bulk_variant,
    _attribute_terms,
    _ensure_missing_axes,
    _is_variable_product,
    _missing_variant_axes,
    _parent_any_axis_options,
    _parent_axis_meta,
    _parent_variation_axes,
    _slugify,
    _term_slug,
    _variant_meta_entry,
    handle_bulk_variant_selection_reply,
)

# Recipient/company/roster helpers were split into
# handlers/bulk/recipients.py. Imported back under their original names so
# every existing call site in this module (and chat.py's imports of the
# handle_* names) keeps working unchanged.
from handlers.bulk.recipients import (
    RECIPIENT_MODE_DIFFERENT,
    RECIPIENT_MODE_SAME,
    _MORE_CONTACTS_CHIP,
    _PREV_CONTACTS_CHIP,
    _RECIPIENT_CHIP_LIMIT,
    _ask_for_bulk_recipient,
    _ask_recipient_mode,
    _build_recipient_queue,
    _line_recipient_display,
    _recipient_candidates,
    _roster_label,
    handle_bulk_company_choice_reply,
    handle_bulk_company_reply,
    handle_bulk_email_reply,
    handle_bulk_recipient_mode_reply,
    handle_bulk_recipient_reply,
)

# Address resolution/prompting/confirmation was split into
# handlers/bulk/addresses.py. Imported back under their original names so
# every existing call site in this module (and chat.py's imports of the
# handle_* names) keeps working unchanged.
from handlers.bulk.addresses import (
    _ADDRESS_RESOLVABLE_REASONS,
    _ORDER_ADDRESS_CACHE_TTL_SECONDS,
    _address_group_key,
    _address_identity_key,
    _address_label,
    _addresses_for_person,
    _advance_to_next_address_confirmation,
    _ask_for_bulk_address,
    _build_address_card_response,
    _build_address_queue,
    _company_order_addresses,
    _continue_after_addresses_chosen,
    _effective_address_for_line,
    _merge_address_block,
    _norm_name,
    _propagate_address_decision,
    _rep_billing_address,
    _reprompt_address_with_errors,
    handle_bulk_address_choice_reply,
    handle_bulk_address_confirmation_reply,
)

# Confirmation/cart/order-creation was split into handlers/bulk/orders.py,
# and the two cross-cutting helpers into handlers/bulk/common.py. Imported
# back under their original names so every existing call site in this
# module (and chat.py's imports of the handle_* names) keeps working
# unchanged.
from handlers.bulk.common import _get, _BULK_STATE_KEYS
from handlers.bulk.orders import (
    _build_bulk_cart_response,
    _build_bulk_confirmation_response,
    _bulk_line_item,
    _create_all_confirmed_orders,
    _format_bulk_confirmation_table,
    _line_product_is_live,
    _order_group_key,
    _planned_order_count,
    handle_bulk_confirmation_unclear,
    handle_bulk_order_confirmation,
)
import re
import difflib
import unicodedata
logger = get_logger("miraq_chat")




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
            "*Order 20 Harmony White, 15 Coral Grey for Beck LTD*\r\n"
            "*Order 20 Harmony White for Ashlynn, 15 Coral Grey for Claire "
            "at Beck LTD*\r\n\r\n"
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
            "suggestions": ["Cancel"],
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
            "recipient_name": getattr(l, "recipient_name", "") or "",
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
            "unmatched_variant_hint": getattr(l, "unmatched_variant_hint", "") or "",
            # Terms the rep typed that no VARIATION enumerates — the axis is
            # "Any" on the variations and its real options live on the parent.
            # _ask_for_bulk_variant resolves these against the parent options
            # and pre-selects them, so the rep isn't asked again for a size
            # they already gave. This dict is built field-by-field, so a field
            # missing here is silently dropped between parser and prompt
            # however correct both ends are.
            "unmatched_variant_terms": list(getattr(l, "unmatched_variant_terms", None) or []),
            "conflicting_variant_terms": list(getattr(l, "conflicting_variant_terms", None) or []),
            # getattr, not attribute access: these fields were added to
            # BulkOrderLine alongside this handler, so a half-deploy where
            # the parser is older would otherwise 500 on every bulk order
            # rather than simply skipping the newer behaviour.
            "blank_variant_axes": list(getattr(l, "blank_variant_axes", None) or []),
            "candidate_variation_ids": list(getattr(l, "candidate_variation_ids", None) or []),
            "specified_variant_axes": list(getattr(l, "specified_variant_axes", None) or []),
            "self_contained_variant": bool(getattr(l, "self_contained_variant", False)),
            # Seeded from the parser: the chip-card fallback pins Sample Size
            # itself for products with no dedicated chip-card variation.
            "variant_meta": dict(getattr(l, "variant_meta", None) or {}),
            "address_confirmed": False,
            "address_skipped": False,
        }
        for l in lines
    ]
    
     # ── Patch: apply classifier-resolved product when parser misidentified digits in name ──
    if pre_resolved and pre_resolved.product_id:
        _pre_name = pre_resolved.product_name or ""
        for ld in lines_as_dicts:
            if ld.get("unresolved") and ld.get("unresolved_reason") in (
                "product_not_found", "both_not_found"
            ):
                # pre_resolved is ONE whole-message classifier resolution,
                # but a bulk request can have several unresolved lines — only
                # patch a line whose OWN text actually names this product.
                # Without this check, a line the parser genuinely couldn't
                # match to anything (e.g. an attribute-only description like
                # "Grey Marble") silently gets relabeled with an unrelated
                # product resolved from a DIFFERENT line, and the fabricated
                # line goes to the confirmation table looking legitimate.
                if not _pre_name or not re.search(
                    r'\b' + re.escape(_pre_name) + r'\b', ld["raw_fragment"], re.I
                ):
                    continue

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
    # Optional checkout-field clauses ("rep X", "order type Y") lifted out of
    # the message by the parser's clause pre-pass. Stored on the session so
    # they survive every prompt detour (variant, quantity, recipient, address)
    # between here and the address card that applies them — the same reason
    # bulk_company_scope is stored rather than threaded through.
    user_context["bulk_field_clause_values"] = _parse_meta.get("field_clause_values", {})
    user_context["bulk_field_clause_notices"] = _parse_meta.get("field_clause_notices", [])
    # Whether the roster above is the company's FULL membership or just the
    # part we managed to read — governs whether "not found" can be asserted.
    user_context["bulk_company_roster_truncated"] = _parse_meta.get(
        "company_roster_truncated", False
    )
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
            else f" for **{_line_recipient_display(first_line)}**"
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
    # Skipped once the rep has chosen "Continue anyway" — they have already
    # been shown that no records exist and said to proceed, so asking again on
    # any re-entry into this function would be a loop they cannot leave.
    if role in BULK_ORDER_FULL_SCOPE_ROLES and not user_context.get("bulk_company_skipped"):
        company_missing = [
            l for l in lines_as_dicts
            if l.get("unresolved_reason") in ("company_not_provided", "company_not_found")
        ]
        if company_missing:
            # ── Fallback: order-history addresses ───────────────────────────
            # No USER ACCOUNT carries this company name, but the company can
            # still be real — /company-order-addresses derives it from ORDER
            # HISTORY, the only source that sees a company shipped to whose
            # customers never had the company field set on their account.
            #
            # This has to live HERE, not only in _ask_for_bulk_recipient:
            # Step 4.55 returns before that function is ever reached, so a
            # fallback placed only there never runs.
            _tried_co = next(
                (l.get("company_name") for l in company_missing if l.get("company_name")),
                "",
            )
            _addr_rows = _company_order_addresses(_tried_co, user_context) if _tried_co else []
            _addr_opts = []
            for _r in _addr_rows:
                _lbl = _address_label(_r)
                if _lbl and _lbl not in _addr_opts:
                    _addr_opts.append(_lbl)

            if _addr_opts:
                logger.info(
                    f"bulk_order | no customer accounts for '{_tried_co}' — "
                    f"offering {len(_addr_opts)} order-history address(es)"
                )
                conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CHOICE.value
                user_context["bulk_address_only_mode"] = True
                # handle_bulk_address_choice_reply reads its slot from
                # bulk_address_queue[bulk_address_pos] — NOT from any
                # pending_* key. Writing elsewhere left the queue empty, so
                # the handler took its early "nothing to choose" branch and
                # skipped applying the address altogether.
                #
                # `options` must be ROW DICTS (the handler calls
                # _address_label on each), and `line_indices` drives the loop
                # that copies the address onto the lines.
                # One slot PER NAMED RECIPIENT, not one slot for the company.
                #
                # This used to be a single company-wide slot carrying
                # `range(len(lines_as_dicts))`, which threw away the recipient
                # the rep had already typed: "1 Curie for Annabelle Damon, 2
                # Enduring for Andrew Gazda" collapsed into one address applied
                # to every line, so both people got the same delivery and the
                # message asked for names the rep had ALREADY given.
                #
                # `recipient_name` survives on the line even when the roster
                # lookup fails, so it is still the best key here. Options are
                # narrowed to that person's own historical rows when any match,
                # which is usually an exact hit — the same order history that
                # produced this company also shipped to these people.
                _by_recipient = {}
                for _i, _l in enumerate(lines_as_dicts):
                    _by_recipient.setdefault(
                        str(_l.get("recipient_name") or "").strip(), []
                    ).append(_i)

                _queue = []
                for _rname, _idxs in _by_recipient.items():
                    _own = [
                        _r for _r in _addr_rows
                        if _norm_name(
                            f"{_r.get('shipping_first_name') or ''} "
                            f"{_r.get('shipping_last_name') or ''}"
                        ) == _norm_name(_rname)
                    ] if _rname else []
                    if _rname and _own:
                        logger.info(
                            f"bulk_order | roster unavailable — matched "
                            f"'{_rname}' to {len(_own)} order-history "
                            f"address(es) for line(s) {_idxs}"
                        )
                    elif _rname:
                        logger.warning(
                            f"bulk_order | roster unavailable and no order "
                            f"history for '{_rname}' — offering all "
                            f"{len(_addr_rows)} company address(es) for "
                            f"line(s) {_idxs}"
                        )
                    _queue.append({
                        "name": _rname or _tried_co,
                        "company": _tried_co,
                        "options": _own or _addr_rows,
                        "line_indices": _idxs,
                    })
                user_context["bulk_address_queue"] = _queue
                user_context["bulk_address_pos"] = 0
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                _pl = "\r\n".join(
                    f"• **{l['quantity']}× {l['product_name']}**" for l in company_missing
                )
                _el = round((time.time() - start_time) * 1000)
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"Got it:\r\n{_pl}\r\n\r\n"
                        f"I don't have contact records for **{_tried_co}**, but it has "
                        f"been shipped to before. Pick a delivery address and I'll use "
                        f"it" + (
                            "." if any(_by_recipient) and "" not in _by_recipient
                            else " — you can add the recipient's name at confirmation."
                        )
                    ),
                    "intent": "guided_flow",
                    "products": [],
                    "suggestions": _addr_opts[:8] + ["Cancel"],
                    "session_id": str(conversation.id),
                    "metadata": {
                        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                        "company": _tried_co,
                        "candidates": _addr_opts,
                        "address_only_mode": True,
                        "response_time_ms": _el,
                    },
                    "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                    "pagination": default_pagination(page),
                }), 200

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
            # Nothing at all: no customer accounts AND no delivery history.
            # Say so plainly and give a way FORWARD as well as a retry — a
            # Cancel-only prompt strands the rep on a company they may know
            # is real (a brand-new client places a first order with no
            # history and no account yet, which is entirely normal).
            _ask = (
                f"I couldn't find **{_tried}** — no customer records and no "
                "past deliveries under that name.\r\n\r\n"
                "You can enter a different company, or continue anyway and "
                "I'll take the delivery address from you directly."
                if _tried else
                "Which company is this order for?"
            )
            _sugg = (
                ["Continue anyway", "Enter a different company", "Cancel"]
                if _tried else ["Cancel"]
            )

            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": f"Got it:\r\n{product_lines}\r\n\r\n{_ask}",
                "intent": "guided_flow",
                "products": [],
                "suggestions": _sugg,
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                    "requested_company": _tried,
                    "no_records_found": bool(_tried),
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
    if role in BULK_ORDER_FULL_SCOPE_ROLES:
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

    # Step 4.57: A resolved customer with SEVERAL shipping addresses on file
    # must be asked which one. This lives here as well as in
    # _continue_after_slots_filled because the two paths are disjoint: when
    # every recipient resolves straight from the roster (unresolved=0) this
    # function goes directly to the quantity prompt and never reaches that
    # shared exit, so the gate there alone never fired.
    if role in BULK_ORDER_FULL_SCOPE_ROLES:
        _addr_queue = _build_address_queue(lines_as_dicts, user_context)
        if _addr_queue:
            user_context["bulk_address_queue"] = _addr_queue
            user_context["bulk_address_pos"] = 0
            return _ask_for_bulk_address(
                lines_as_dicts, _addr_queue, 0,
                conversation, user_context, page, start_time,
            )

    # Step 4.6: Rep (and customer, full-scope) lines missing an email → ask before proceeding
    if role in BULK_ORDER_FULL_SCOPE_ROLES:
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
        and _is_variable_product(l["product_id"], store_loader)
        and (not l["variation_id"]
             or l.get("blank_variant_axes")
             or _ensure_missing_axes(l, user_context))
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
























def _unnamed_multi_slot(queue) -> bool:
    """True when the queue has an unnamed slot covering more than one line."""
    return any(not s["name"] and len(s["line_indices"]) > 1 for s in queue)

















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
            else f" for **{_line_recipient_display(first_line)}**"
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

    Inserts the address-choice step: a resolved customer can have SEVERAL
    shipping addresses on file (order history), and picking one silently would
    ship to whichever happened to be on their account record. Customers with a
    single address skip this entirely.
    """
    queue = _build_address_queue(lines_as_dicts, user_context)
    if queue:
        user_context["bulk_address_queue"] = queue
        user_context["bulk_address_pos"] = 0
        return _ask_for_bulk_address(
            lines_as_dicts, queue, 0,
            conversation, user_context, page, start_time,
        )
    return _continue_after_addresses_chosen(
        lines_as_dicts, store_loader, conversation, user_context, page, start_time
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
        "suggestions": ["Cancel"],
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
            "suggestions": ["Cancel"],
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
            "suggestions": ["Cancel"],
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
        and _is_variable_product(l["product_id"], store_loader)
        and (not l.get("variation_id")
             or l.get("blank_variant_axes")
             or _ensure_missing_axes(l, user_context))
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