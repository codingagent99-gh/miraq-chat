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

# How many contact names are offered as tappable chips at the recipient step.
# The full roster goes out in metadata.candidates regardless — this only caps
# the chip row, which becomes unusable past roughly this many. Any message
# quoting a contact COUNT must say how many are actually shown, or the rep
# reads "20 contacts" above 8 chips and assumes the rest are unreachable.
_RECIPIENT_CHIP_LIMIT = 8

# Chip labels for paging the contact list. Matched before any name lookup in
# handle_bulk_recipient_reply — if a real contact were ever called this, the
# rep could not select them, so keep them clearly non-name-like.
_MORE_CONTACTS_CHIP = "▸ More contacts"
_PREV_CONTACTS_CHIP = "◂ Previous contacts"

_BULK_STATE_KEYS = (
    "pending_bulk_lines", "bulk_current_line_index",
    "bulk_confirmed_lines", "bulk_address_overrides",
    "bulk_awaiting_address_text",
    "bulk_product_missing_indices", "bulk_product_current_pos",
    "bulk_quantity_pending_indices", "bulk_quantity_current_pos",
    # Which page of contact chips the recipient prompt is showing.
    "bulk_recipient_chip_page",
    # Set when the rep answers "Continue anyway" to a company with no records.
    # It suppresses the company prompt for the REST OF THAT ORDER only — left
    # behind, it would silently skip company resolution on the next bulk order
    # in the same session.
    "bulk_company_skipped",
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
            # getattr, not attribute access: these fields were added to
            # BulkOrderLine alongside this handler, so a half-deploy where
            # the parser is older would otherwise 500 on every bulk order
            # rather than simply skipping the newer behaviour.
            "blank_variant_axes": list(getattr(l, "blank_variant_axes", None) or []),
            "candidate_variation_ids": list(getattr(l, "candidate_variation_ids", None) or []),
            "specified_variant_axes": list(getattr(l, "specified_variant_axes", None) or []),
            "variant_meta": {},
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
    if role in BULK_ORDER_ROLES and not user_context.get("bulk_company_skipped"):
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
                user_context["bulk_address_queue"] = [{
                    "name": _tried_co,
                    "company": _tried_co,
                    "options": _addr_rows,
                    "line_indices": list(range(len(lines_as_dicts))),
                }]
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
                        f"it — you can add the recipient's name at confirmation."
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

    # Step 4.57: A resolved customer with SEVERAL shipping addresses on file
    # must be asked which one. This lives here as well as in
    # _continue_after_slots_filled because the two paths are disjoint: when
    # every recipient resolves straight from the roster (unresolved=0) this
    # function goes directly to the quantity prompt and never reaches that
    # shared exit, so the gate there alone never fired.
    if role in BULK_ORDER_ROLES:
        _addr_queue = _build_address_queue(lines_as_dicts, user_context)
        if _addr_queue:
            user_context["bulk_address_queue"] = _addr_queue
            user_context["bulk_address_pos"] = 0
            return _ask_for_bulk_address(
                lines_as_dicts, _addr_queue, 0,
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
    table += f"✅ {resolved_count} order(s) ready to place."

    return table

# ══════════════════════════════════════════════════════════════
# ── Private: _ask_for_bulk_variant ──
# ══════════════════════════════════════════════════════════════

def _parent_variation_axes(product_id, user_context):
    """
    Every axis the PARENT product marks as used-for-variations, with options.

    This is the only reliable source for WooCommerce "Any" axes. wc/v3's
    variations endpoint OMITS an attribute a variation leaves as Any rather
    than returning it blank, so a variation cannot be inspected to discover
    what it failed to specify — Adams 13544 comes back carrying only Colors,
    with no trace of Finish or Sample Size. Comparing against the parent is
    what surfaces them.
    """
    cache = user_context.setdefault("bulk_parent_axis_cache", {})
    key = str(product_id)
    if key in cache:
        return cache[key]

    axes = {}
    fetched = False
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
            fetched = True
    except Exception as exc:
        logger.warning(
            f"bulk_order | parent attribute fetch failed for {product_id} | error={exc}"
        )

    # Success only — a cached failure would disable the prompt for the session.
    if fetched:
        cache[key] = axes
        user_context["bulk_parent_axis_cache"] = cache
    return axes


def _missing_variant_axes(line, user_context):
    """
    Parent variation axes the line's matched variation does NOT pin down.

    Returns [] for a line with no variation yet (the normal prompt covers it)
    and for a fully specified one.
    """
    product_id = line.get("product_id")
    if not product_id or not line.get("variation_id"):
        return []
    parent = _parent_variation_axes(product_id, user_context)
    if not parent:
        return []
    specified = {
        str(a).strip().lower()
        for a in (line.get("specified_variant_axes") or [])
    }
    return [name for name in parent if name.strip().lower() not in specified]


def _parent_any_axis_options(product_id, axis_names, user_context):
    """Options for a named subset of the parent's variation axes."""
    if not axis_names:
        return {}
    parent = _parent_variation_axes(product_id, user_context)
    wanted = {a.strip().lower() for a in axis_names if a}
    return {
        name: opts for name, opts in parent.items()
        if name.strip().lower() in wanted
    }


def _ensure_missing_axes(line, user_context):
    """
    True when the line has a variation that leaves parent axes unset, and
    stamps them onto the line so the prompt knows what to ask.
    """
    if line.get("blank_variant_axes"):
        return True
    missing = _missing_variant_axes(line, user_context)
    if missing:
        line["blank_variant_axes"] = missing
        logger.info(
            f"bulk_order | product {line.get('product_id')} variation "
            f"{line.get('variation_id')} leaves {missing} unset — will ask"
        )
        return True
    return False


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

    # When the hint narrowed to several variations ("Harmony Moon" in five
    # sizes), offer only those. Using every variation would re-ask the
    # colour the rep already gave and list sizes that do not exist in it.
    _candidates = line.get("candidate_variation_ids") or []
    if _candidates:
        _narrowed = [v for v in variations if v.get("id") in _candidates]
        if _narrowed:
            variations = _narrowed

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
    #
    # blank_variant_axes is only populated once a variation has been resolved
    # and inspected. On the FIRST prompt for a line that has no variation yet
    # it is empty, so this used to add nothing and the rep was asked for
    # Colors alone — then asked again for Finish and Sample Size the moment a
    # colour pinned a variation. Two prompts for one product, the first one
    # visibly incomplete.
    #
    # Derive them here instead: any parent variation axis that NO variation
    # sets is an "Any" axis by definition, which is exactly what
    # _missing_variant_axes would conclude later. Products whose variations
    # encode every axis produce an empty list and are unaffected.
    _blank_axes = list(line.get("blank_variant_axes") or [])
    if not _blank_axes:
        _seen = {k.strip().lower() for k in attr_axes}
        _blank_axes = [
            n for n in _parent_variation_axes(product_id, user_context)
            if n.strip().lower() not in _seen
        ]
    _any_axes = _parent_any_axis_options(product_id, _blank_axes, user_context)
    for _name, _opts in _any_axes.items():
        attr_axes.setdefault(_name, set()).update(_opts)

    # EVERY axis stays on the prompt, including ones the matched variation
    # already pins — the rep may still want to change the colour. What their
    # message settled rides along in `preselected` so the UI can pre-tick it
    # rather than making them find and re-pick a value they already gave.
    attributes = [
        {"name": name, "options": sorted(opts)}
        for name, opts in attr_axes.items()
    ]

    _resolved_axes = {}
    if line.get("variation_id"):
        _matched_var = next(
            (v for v in variations if v.get("id") == line.get("variation_id")), None
        )
        if _matched_var:
            _resolved_axes = {
                n: o
                for n, o in _get_safe_options(_matched_var.get("attributes", [])).items()
                if n and o
            }
    # Use the same derived list the attribute options came from, so the
    # wording ("just need the Finish, Sample Size") matches what's rendered
    # even on the first prompt, where the line carries no blank_variant_axes.
    _open_axes = {a.strip().lower() for a in _blank_axes if a}

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
            "company": _line_recipient_display(line),
            "is_self_order": line.get("is_self_order", False),
            "product_name": line.get("product_name", ""),
            "quantity": line.get("quantity", 0),
            "progress": {"current": pos + 1, "total": len(needs_variant_indices)},
            "attributes": attributes,
            # Axes already settled from the rep's message. The UI should show
            # these as chosen (read-only), not ask for them again.
            "preselected": _resolved_axes,
            "variations": variation_list,
            "unmatched_variant_hint": _bad_hint if _hint_not_in_catalog else "",
        },
    }

    _line_label = (
        f"{_line_recipient_display(line)} × {line.get('quantity', 0) or '?'}"
    )
    if _hint_not_in_catalog:
        _bot_message = (
            f"I couldn't find **{_bad_hint}** for **{line['product_name']}** — "
            f"that option isn't in the catalog. Please pick from the available "
            f"options instead ({_line_label}):"
        )
    else:
        if _resolved_axes and _open_axes:
            _settled = ", ".join(f"{v}" for v in _resolved_axes.values())
            _bot_message = (
                f"**{line['product_name']}** — {_settled} is set. "
                f"Just need the {', '.join(line.get('blank_variant_axes') or [])} "
                f"({_line_label}):"
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
    action = {
        "type": "SHOW_BULK_ORDER_CONFIRMATION",
        "payload": {
            "lines": _display_lines,
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
            "skipped_count": skipped_count,
        },
    }

    # Addresses are confirmed before this card is built, so "ready to place"
    # is now literally true and confirming is the final action.
    bot_message = (
        f"Here's your bulk order — {resolved_count} order(s) ready to place. "
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

    line["variation_id"] = best_match["id"]
    line["unresolved"] = False
    line["unresolved_reason"] = None

    # The variation just chosen may ITSELF leave parent axes unset — "Any"
    # axes that no single variation encodes. Detect that HERE, after stamping.
    # A line whose variation came from this prompt (rather than from a hint in
    # the original message) had no variation to inspect until now, so nothing
    # had ever computed its blank axes and Sample Size / Finish were dropped
    # from the order silently. Lines with a hint were checked earlier, which
    # is why only those ever asked.
    _ensure_missing_axes(line, user_context)

    # Record the rep's choice for any axis the variation itself can't encode.
    # These ride along as order line-item meta — exactly where WooCommerce
    # puts an "Any" attribute chosen on the product page. The reply often
    # already names them ("TOKYO Aegean Blue, Chip Card"), so harvest from
    # this same message before deciding anything is still missing.
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

    # An axis the reply didn't answer → ask THIS line again instead of moving
    # on, which is what silently produced order items with no Sample Size or
    # Finish. Capped: after two further tries the order proceeds without them
    # rather than trapping the rep in a prompt they cannot satisfy (a parent
    # axis with no usable options would otherwise loop forever).
    _still_pending = line.get("blank_variant_axes") or []
    _axis_tries = int(line.get("variant_axis_attempts") or 0)
    if _still_pending and _axis_tries < 2:
        line["variant_axis_attempts"] = _axis_tries + 1
        lines_as_dicts[line_idx] = line
        user_context["pending_bulk_lines"] = lines_as_dicts
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        logger.info(
            f"bulk_order | line {line_idx} still missing {_still_pending} "
            f"after variant pick — re-asking (attempt {_axis_tries + 1}/2)"
        )
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, pos,
            conversation, user_context, page, start_time,
        )
    if _still_pending:
        logger.warning(
            f"bulk_order | line {line_idx} proceeding with {_still_pending} "
            f"unset after {_axis_tries} attempt(s)"
        )

    next_pos = pos + 1
    user_context["bulk_variant_current_pos"] = next_pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    if next_pos < len(needs_variant_indices):
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, next_pos,
            conversation, user_context, page, start_time,
        )

    # All variants resolved — collect ADDRESSES next, then show the summary.
    #
    # This site returns directly rather than going through
    # _continue_after_addresses_chosen, so reordering that function alone left
    # this path unchanged: the rep still saw a "ready to place" table before
    # any address existed.
    _resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
    if _resolved:
        user_context["bulk_current_line_index"] = 0
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            _resolved, 0, conversation, user_context, page, start_time,
        )

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
    Roster entries to offer for this slot, plus label/wording flags.

    Returns (entries, disambiguate_labels, name_matched).

    For a name that matches roster entries, only THOSE entries are offered —
    showing the whole company would bury the actual choice. When the name
    matches nobody, the full roster is offered as a fallback so the rep can
    still pick, but `name_matched` comes back False so the caller words the
    prompt as "I couldn't find X" instead of claiming those people share the
    requested name.

    `disambiguate_labels` is purely about LABELS (append city when two offered
    entries share a display name) and says nothing about whether the requested
    name matched — conflating the two is what produced "There are 20 people
    named Kelly Fitchett" for a roster of 20 unrelated customers that merely
    happened to contain two Carissa Diazes.
    """
    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    # Tolerate a malformed roster (bad JSONB, partial write, older schema)
    # rather than raising or offering unusable buttons.
    roster = [r for r in (roster or []) if isinstance(r, dict) and r.get("display")]

    needle = _norm(slot.get("name") if isinstance(slot, dict) else "")
    entries = roster
    name_matched = False

    if needle and roster:
        # Every word of the requested name must appear in the display name.
        # The previous test was `needle in _norm(display).split()`, which
        # asked whether the whole "kelly fitchett" string equalled one
        # single-word token — impossible for any first+last name, so this
        # branch was dead and only exact equality could ever match.
        needle_tokens = [t for t in needle.split() if t]
        matched = []
        for r in roster:
            hay = _norm(r.get("display"))
            if not hay:
                continue
            hay_tokens = hay.split()
            if (
                hay == needle
                or (needle_tokens and all(t in hay_tokens for t in needle_tokens))
                or needle in hay
            ):
                matched.append(r)

        # Narrow on ANY match, including exactly one. The old guard was
        # `len(matched) > 1`, so a single match — and, critically, ZERO
        # matches — silently left `entries` as the entire company roster.
        if matched:
            entries = matched
            name_matched = True

    counts = {}
    for r in entries:
        k = _norm(r.get("display"))
        counts[k] = counts.get(k, 0) + 1
    disambiguate = any(c > 1 for c in counts.values())
    return entries, disambiguate, name_matched


def _ask_for_bulk_recipient(
    lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
):
    """Prompt for the recipient of one slot in the queue."""
    roster = user_context.get("bulk_company_roster", []) or []
    scope  = user_context.get("bulk_company_scope", "")
    _truncated = bool(user_context.get("bulk_company_roster_truncated", False))

    # ── Stage 1: which COMPANY? ─────────────────────────────────────────────
    # Company lookup is fuzzy (substring OR similar_text >= 70), so one name
    # can legitimately return people from SEVERAL companies — "Turner Ceramic
    # Tile" and "Turner Ceramics" are different businesses. Asking for the
    # person first would mix staff from both into one list, and the rep could
    # pick someone from the wrong company without ever seeing that two
    # existed. Company first, then person.
    def _norm_company(v):
        # Same normalisation the plugin uses: punctuation is noise, so
        # "Turner Ceramic & Tile" and "Turner Ceramic Tile" are ONE company.
        # Deduping on the raw string instead would offer a picker between two
        # spellings of the same business.
        return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]+', ' ', str(v or "").lower())).strip()

    _companies = []
    _seen_c = set()
    for r in roster:
        c = (r.get("company") or "").strip()
        key = _norm_company(c)
        if c and key and key not in _seen_c:
            _seen_c.add(key)
            _companies.append(c)

    if len(_companies) > 1 and not user_context.get("bulk_company_choice_made"):
        conversation.flow_state = FlowState.AWAITING_BULK_COMPANY_CHOICE.value
        user_context["bulk_recipient_queue"] = queue
        user_context["bulk_recipient_pos"] = pos
        user_context["pending_company_choice"] = {"companies": _companies}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        _el = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"**{len(_companies)}** companies match **{scope}**. "
                "Which one is this order for?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": _companies[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
                "candidates": _companies,
                "requested_company": scope,
                "response_time_ms": _el,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
            "pagination": default_pagination(page),
        }), 200


    # ── Fail-safe: no usable slot to ask about ──────────────────────────────
    # A lost/corrupt queue (resumed session, partial context write) would
    # otherwise IndexError here. Bail to the company step rather than
    # guessing which line we were asking about.
    if not queue or not (0 <= pos < len(queue)) or not isinstance(queue[pos], dict):
        logger.warning(
            f"bulk_order | recipient prompt aborted — bad queue state "
            f"(pos={pos}, queue_len={len(queue) if queue else 0}) for company '{scope}'"
        )
        conversation.flow_state = FlowState.AWAITING_BULK_COMPANY.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                "I lost track of who this order is for. "
                "Which company is this order for?"
            ),
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

    slot = queue[pos]
    entries, _disambiguate, _name_matched = _recipient_candidates(slot, roster)
    names = [_roster_label(r, _disambiguate) for r in entries if r.get("display")]

    # ── Fail-safe: nothing to offer ─────────────────────────────────────────
    # An empty/unusable roster previously produced "**X** has 0 contacts."
    # with a Cancel-only button — a dead end. Send the rep back to the
    # company step, which is the thing that actually needs correcting.
    if not names:
        # ── Fallback: order-history addresses ───────────────────────────────
        # An empty roster means no USER ACCOUNT carries this company name. The
        # company can still be real: /company-order-addresses derives it from
        # ORDER HISTORY, which is the only source that sees a company that has
        # been shipped to but whose customers never had the company field set
        # on their account. Offer those destinations rather than dead-ending
        # the rep on a company that demonstrably exists.
        _addr_rows = _company_order_addresses(scope, user_context)
        if _addr_rows:
            _opts = []
            for _r in _addr_rows:
                _lbl = _address_label(_r)
                if _lbl and _lbl not in _opts:
                    _opts.append(_lbl)
            logger.info(
                f"bulk_order | roster empty for '{scope}' — offering "
                f"{len(_opts)} order-history address(es)"
            )
            if _opts:
                conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CHOICE.value
                user_context["bulk_recipient_queue"] = queue
                user_context["bulk_recipient_pos"] = pos
                # Signals the address step is standing in for the recipient
                # step: there is no customer account to attach, so the name is
                # collected at confirmation instead.
                user_context["bulk_address_only_mode"] = True
                user_context["bulk_address_queue"] = [{
                    "name": scope,
                    "company": scope,
                    "options": _addr_rows,
                    "line_indices": list(range(len(lines_as_dicts))),
                }]
                user_context["bulk_address_pos"] = 0
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                _el = round((time.time() - start_time) * 1000)
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"I don't have contact records for **{scope}**, but it has "
                        f"been shipped to before. Pick a delivery address and I'll "
                        f"use it — you can add the recipient's name at confirmation."
                    ),
                    "intent": "guided_flow",
                    "products": [],
                    "suggestions": _opts[:8] + ["Cancel"],
                    "session_id": str(conversation.id),
                    "metadata": {
                        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                        "company": scope,
                        "candidates": _opts,
                        "address_only_mode": True,
                        "response_time_ms": _el,
                    },
                    "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                    "pagination": default_pagination(page),
                }), 200

        logger.warning(
            f"bulk_order | recipient prompt aborted — no usable roster entries "
            f"for company '{scope}' (roster_size={len(roster)})"
        )
        conversation.flow_state = FlowState.AWAITING_BULK_COMPANY.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                # Reached only when the company has NO customer accounts AND has
                # never been shipped to — genuinely unknown to this store.
                f"I can't find **{scope}** — there are no customer records and "
                f"no past deliveries under that name. Check the spelling, or tell "
                f"me which company this order is for."
                if scope else "Which company is this order for?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
                "company": scope,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    conversation.flow_state = FlowState.AWAITING_BULK_RECIPIENT.value
    user_context["bulk_recipient_queue"] = queue
    user_context["bulk_recipient_pos"] = pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # Skip line indices that no longer exist rather than IndexError-ing on a
    # queue built against a different (older) set of lines.
    product_lines = "\r\n".join(
        f"• **{lines_as_dicts[i]['quantity']}× {lines_as_dicts[i]['product_name']}**"
        for i in slot.get("line_indices", [])
        if isinstance(i, int) and 0 <= i < len(lines_as_dicts)
    )

    # ── Chip paging ─────────────────────────────────────────────────────────
    # Paged, not truncated: with 20 contacts the rep saw the first 8 and had
    # no way to reach the other 12 short of typing a name exactly right.
    # Clamped rather than wrapped — a stale page index (roster re-fetched
    # smaller, or the rep backing out of a company choice) would otherwise
    # render an empty chip row with no way forward.
    _chip_page = int(user_context.get("bulk_recipient_chip_page", 0) or 0)
    _max_chip_page = max(0, (len(names) - 1) // _RECIPIENT_CHIP_LIMIT)
    _chip_page = max(0, min(_chip_page, _max_chip_page))
    user_context["bulk_recipient_chip_page"] = _chip_page
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    _chip_lo = _chip_page * _RECIPIENT_CHIP_LIMIT
    _chip_hi = min(_chip_lo + _RECIPIENT_CHIP_LIMIT, len(names))
    _chips = names[_chip_lo:_chip_hi]

    _nav = []
    if _chip_page < _max_chip_page:
        _nav.append(_MORE_CONTACTS_CHIP)
    if _chip_page > 0:
        _nav.append(_PREV_CONTACTS_CHIP)

    # Wording keys off whether the requested name actually matched — never off
    # the label-disambiguation flag, which can be True purely because two
    # UNRELATED people on the roster share a name.
    if slot.get("name") and _name_matched and len(entries) > 1:
        _ask = (
            f"There are {len(entries)} people named **{slot['name']}** at "
            f"**{scope}**, at different addresses. Which one?"
        )
    elif slot.get("name") and _name_matched:
        _ask = (
            f"Is this **{slot['name']}** at **{scope}**? "
            "Confirm or pick someone else."
        )
    elif slot.get("name") and _truncated:
        # The roster is incomplete, so absence was never established — don't
        # tell the rep this person isn't at the company when we only read
        # part of it.
        _ask = (
            f"I couldn't find **{slot['name']}** in the first {len(roster)} "
            f"contacts at **{scope}**, and there may be more I haven't checked. "
            "Who should this go to?"
        )
    elif slot.get("name"):
        _ask = (
            f"I couldn't find **{slot['name']}** at **{scope}**. "
            "Who should this go to?"
        )
    else:
        # Chips are paged rather than truncated: with 20 contacts the rep
        # previously saw 8 and had no way to reach the other 12 except by
        # typing a name exactly right.
        _shown = min(len(names), _RECIPIENT_CHIP_LIMIT)
        if len(names) > _RECIPIENT_CHIP_LIMIT:
            _ask = (
                f"**{scope}** has {len(names)} contacts — showing "
                f"{_chip_lo + 1}–{_chip_hi} of {len(names)}. "
                "Tap a name, or type one."
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
        "suggestions": _chips + _nav + ["Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_RECIPIENT.value,
            "company": scope,
            "candidates": names,
            "requested_name": slot.get("name", ""),
            # False means these candidates are a fallback list of everyone at
            # the company, NOT people matching requested_name — the frontend
            # must not label them as such.
            "name_matched": _name_matched,
            # True means these candidates are only part of the company's
            # contacts — absence from this list proves nothing.
            "roster_truncated": _truncated,
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

    # ── Chip paging, before any name matching ───────────────────────────────
    # These are navigation, not answers: re-ask the SAME slot with the next or
    # previous page of contacts. Matched first so a paging tap can never be
    # fuzzy-matched to a contact whose name happens to look similar, which
    # would silently ship to the wrong person.
    if choice in (_MORE_CONTACTS_CHIP, _PREV_CONTACTS_CHIP):
        _cur = int(user_context.get("bulk_recipient_chip_page", 0) or 0)
        user_context["bulk_recipient_chip_page"] = max(
            0, _cur + (1 if choice == _MORE_CONTACTS_CHIP else -1)
        )
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_recipient(
            lines, queue, pos, conversation, user_context, page, start_time,
        )

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
    entries, _disambiguate, _name_matched = _recipient_candidates(slot_for_match, roster)

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
        picked = _only(lambda r: _norm(_roster_label(r, _disambiguate)) == needle)
        # 2. exact display name, only if it identifies one person
        if not picked:
            picked = _only(lambda r: _norm(r.get("display")) == needle)
        # 3. partial (first/last name), again only if unique
        if not picked:
            def _loose(r):
                hay = _norm(r.get("display"))
                return bool(hay) and (needle in hay or any(needle == w for w in hay.split()))
            picked = _only(_loose)

    # A roster entry with no id cannot be stamped onto a line — treating it as
    # a match would produce an order with no customer attached. Fall through
    # to the re-ask instead.
    if picked and not picked.get("id"):
        logger.warning(
            f"bulk_order | discarded match '{picked.get('display')}' for "
            f"'{choice}' at '{scope}' — roster entry has no id"
        )
        picked = None

    if not picked:
        names = [_roster_label(r, _disambiguate) for r in entries if r.get("display")]
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't match **{choice}** to anyone at **{scope}**."
                + (
                    f" Here are the first {_RECIPIENT_CHIP_LIMIT} of "
                    f"{len(names)} contacts — type a name if it's someone else."
                    if len(names) > _RECIPIENT_CHIP_LIMIT else ""
                )
                if choice else f"Who should this go to at **{scope}**?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": names[:_RECIPIENT_CHIP_LIMIT] + ["Cancel"],
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
        # Fresh chip page for the next slot — carrying page 3 over would open
        # the next question part-way down the contact list.
        user_context["bulk_recipient_chip_page"] = 0
        return _ask_for_bulk_recipient(
            lines, queue, pos + 1,
            conversation, user_context, page, start_time,
        )

    user_context.pop("bulk_recipient_chip_page", None)
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

    # "Continue anyway" after a company with no records at all — the rep knows
    # the company is real (a brand-new client has neither an account nor
    # delivery history).
    #
    # Rejoin the NORMAL flow rather than diverting to free-text address entry.
    # The address-confirmation step later on already renders an editable panel,
    # validates every required field, and blocks until they are filled — so
    # asking the rep to type a name and address here duplicates that step,
    # loses the structured field-by-field validation, and does it BEFORE the
    # quantity and variant questions instead of at the point the rest of the
    # flow puts it.
    #
    # The lines keep their unresolved customer: with no company records there
    # is nobody to resolve them to, so the confirmation panel opens with blank
    # shipping fields for the rep to complete.
    if company.lower() in ("continue anyway", "continue", "proceed anyway", "proceed"):
        user_context.pop("bulk_manual_address_mode", None)
        # Remember that the company lookup was deliberately bypassed, so no
        # later step re-prompts for a company the rep has already skipped.
        user_context["bulk_company_skipped"] = True
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        logger.info(
            "bulk_order | rep chose to continue with no company records — "
            "resuming normal flow; address collected at the confirmation step"
        )
        _lines = user_context.get("pending_bulk_lines", [])
        return _continue_after_slots_filled(
            _lines, store_loader, conversation, user_context, page, start_time
        )

    if company.lower() in ("enter a different company", "different company"):
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "Which company is this order for?",
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
        and _is_variable_product(l["product_id"], store_loader)
        and (not l.get("variation_id")
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


def _company_order_addresses(company, user_context):
    """
    Shipping destinations this company has actually had goods sent to.

    Derived from ORDER HISTORY (GET /company-order-addresses) — the same
    source the storefront's own company address picker uses, and the only one
    that shows several addresses for a single person: /customers/by-company
    returns one account address each, and the THWMA address book is empty on
    this store.

    Cached per company; the whole batch shares one company.
    """
    if not company:
        return []
    cache = user_context.setdefault("bulk_order_address_cache", {})
    key = str(company).strip().lower()
    if key in cache:
        return cache[key]

    rows = []
    fetched = False
    try:
        call = endpoints.fetch_company_order_addresses(
            company_name=company,
            description=f"Order-history addresses for {company!r}",
        )
        res = woo_client.execute(call)
        data = res.get("data") if res.get("success") else None
        if isinstance(data, dict):
            data = data.get("data", [])
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
            fetched = True
    except Exception as exc:
        logger.warning(
            f"bulk_order | order-address lookup failed for {company!r} | error={exc}"
        )

    # Cache SUCCESS only. user_context is persisted on the conversation, so
    # caching [] after a failed call silently disables the address step for
    # the rest of the session — the next turn short-circuits and never even
    # retries the API. A successful empty result IS cached; that is a real
    # answer.
    if fetched:
        cache[key] = rows
        user_context["bulk_order_address_cache"] = cache
    logger.info(
        f"bulk_order | company {company!r} → {len(rows)} historical "
        f"address(es) | fetched={fetched}"
    )
    return rows


def _addresses_for_person(rows, first_name, last_name, customer_id=None):
    """
    Historical addresses belonging to one person, de-duplicated.

    Matched on customer_id when the row has one (guest orders carry 0), else
    on name. Only rows with no street at all are dropped — a partial address
    is still shown.

    These come from past checkouts, so the list includes whatever was typed
    at the time; an entry like "ASDF" with no city or postcode WILL be
    offered. That is deliberate: a stricter filter silently hid a real
    second address for people whose other orders were sloppily entered, and
    the rep can tell junk from real. The label shows every field present, so
    a bad entry is visibly bad at the point of choosing.
    """
    def _n(v):
        return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()

    want_name = f"{_n(first_name)} {_n(last_name)}".strip()
    out, seen = [], set()
    for r in rows:
        rid = int(r.get("customer_id") or 0)
        row_name = f'{_n(r.get("shipping_first_name"))} {_n(r.get("shipping_last_name"))}'.strip()
        if customer_id and rid:
            if str(rid) != str(customer_id):
                continue
        elif want_name and row_name != want_name:
            continue

        a1 = str(r.get("shipping_address_1") or "").strip()
        if not a1:
            continue

        sig = _n("|".join([
            a1,
            str(r.get("shipping_address_2") or ""),
            str(r.get("shipping_city") or ""),
            str(r.get("shipping_state") or ""),
            str(r.get("shipping_postcode") or ""),
        ]))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


def _line_recipient_display(line) -> str:
    """Name to SHOW for a line.

    customer_display_name doubles as a status label when no customer account
    resolved ("⚠️ No customers for X", "⚠️ Not found"). That is useful on the
    unresolved-lines table, but once the rep has supplied a shipping address
    the recipient IS known, and continuing to show the warning makes a
    complete order look broken.

    Order of preference: the shipping name the rep entered, then the company
    on the address, then the stored display name (which may still be a ⚠️
    label — correct when nothing has been supplied yet).
    """
    if not isinstance(line, dict):
        return ""
    ship = line.get("shipping_address") or {}
    name = " ".join(
        v for v in (ship.get("first_name"), ship.get("last_name")) if v
    ).strip()
    if name:
        return name
    if ship.get("company"):
        return ship["company"]
    bill = line.get("billing_address") or {}
    if bill.get("company"):
        return bill["company"]
    return line.get("customer_display_name", "") or ""


def _address_label(row) -> str:
    """One-line address label for the picker."""
    parts = [
        str(row.get("shipping_address_1") or "").strip(),
        str(row.get("shipping_address_2") or "").strip(),
        str(row.get("shipping_city") or "").strip(),
        str(row.get("shipping_state") or "").strip(),
        str(row.get("shipping_postcode") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def _build_address_queue(lines_as_dicts, user_context):
    """
    One slot per resolved customer that has MORE THAN ONE historical address.

    A customer with a single address needs no question — their line already
    carries it. Lines for the same customer share a slot, so the rep is asked
    once per person, not once per product.
    """
    company = user_context.get("bulk_company_scope", "")
    if not company:
        return []
    rows = _company_order_addresses(company, user_context)
    if not rows:
        return []

    slots = {}
    for idx, l in enumerate(lines_as_dicts):
        cid = l.get("customer_id")
        if not cid or l.get("unresolved"):
            continue
        if l.get("address_choice_made"):
            # Already asked and answered. Without this the queue rebuilds on
            # every pass through the shared exit — the quantity reply routes
            # back through it — and the rep is asked the same question twice.
            continue
        ship = l.get("shipping_address") or {}
        options = _addresses_for_person(
            rows, ship.get("first_name"), ship.get("last_name"), cid
        )
        if len(options) < 2:
            continue
        slot = slots.setdefault(str(cid), {
            "customer_id": str(cid),
            "name": l.get("customer_display_name", ""),
            "options": options,
            "line_indices": [],
        })
        slot["line_indices"].append(idx)
    return list(slots.values())


def _ask_for_bulk_address(
    lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
):
    """Prompt for which of a person's known addresses to ship to."""
    slot = queue[pos]
    conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CHOICE.value
    user_context["bulk_address_queue"] = queue
    user_context["bulk_address_pos"] = pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    labels = [_address_label(o) for o in slot["options"]]
    product_lines = "\r\n".join(
        f"• **{lines_as_dicts[i]['quantity']}× {lines_as_dicts[i]['product_name']}**"
        for i in slot["line_indices"]
    )
    _progress = f" ({pos + 1} of {len(queue)})" if len(queue) > 1 else ""

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            f"{product_lines}\r\n\r\n**{slot['name']}** has "
            f"{len(labels)} addresses on file. Which one should this ship to?{_progress}"
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": labels[:8] + ["Cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
            "customer": slot["name"],
            "candidates": labels,
            "progress": {"current": pos + 1, "total": len(queue)},
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
        "pagination": default_pagination(page),
    }), 200


def handle_bulk_address_choice_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_ADDRESS_CHOICE.

    Applies the chosen address to the CURRENT slot only, then advances — same
    per-slot discipline as the recipient queue, so one person's choice never
    leaks onto another's lines.
    """
    choice = (message or "").strip()
    lines  = user_context.get("pending_bulk_lines", [])
    queue  = user_context.get("bulk_address_queue", []) or []
    pos    = user_context.get("bulk_address_pos", 0)

    if not queue or pos >= len(queue):
        return _continue_after_addresses_chosen(
            lines, store_loader, conversation, user_context, page, start_time
        )

    slot = queue[pos]

    def _n(v):
        return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()

    needle = _n(choice)
    picked = None
    if needle:
        for o in slot["options"]:
            if _n(_address_label(o)) == needle:
                picked = o
                break
        if not picked:
            # Partial: enough of the street to identify exactly one option.
            hits = [o for o in slot["options"] if needle in _n(_address_label(o))]
            if len(hits) == 1:
                picked = hits[0]

    if not picked:
        labels = [_address_label(o) for o in slot["options"]]
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't match **{choice}** to one of "
                f"**{slot['name']}**'s addresses. Please pick one."
                if choice else "Please pick an address."
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": labels[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
                "candidates": labels,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CHOICE.value,
            "pagination": default_pagination(page),
        }), 200

    for idx in slot["line_indices"]:
        if idx >= len(lines):
            continue
        ship = dict(lines[idx].get("shipping_address") or {})

        # The rep's OWN stated recipient wins over the name attached to a
        # historical address. Order-history rows carry whoever that delivery
        # went to, so copying their name across overwrote the people the rep
        # actually named — "1x London for Tamra Smith" silently became Kevin
        # Shuker's order because his name rode along with the address.
        # The address supplies the DESTINATION; the recipient comes from the
        # request.
        _stated = (lines[idx].get("recipient_name") or "").strip()
        if _stated:
            _parts = _stated.split()
            _first, _last = _parts[0], " ".join(_parts[1:])
        else:
            _first = picked.get("shipping_first_name", "") or ship.get("first_name", "")
            _last  = picked.get("shipping_last_name", "")  or ship.get("last_name", "")

        ship.update({
            "first_name": _first,
            "last_name":  _last,
            "company":    picked.get("company", "") or ship.get("company", ""),
            "address_1":  picked.get("shipping_address_1", ""),
            "address_2":  picked.get("shipping_address_2", ""),
            "city":       picked.get("shipping_city", ""),
            "state":      picked.get("shipping_state", ""),
            "postcode":   picked.get("shipping_postcode", ""),
            "country":    picked.get("shipping_country", ""),
        })
        lines[idx]["shipping_address"] = ship
        lines[idx]["address_choice_made"] = True

    user_context["pending_bulk_lines"] = lines
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    logger.info(
        f"bulk_order | address {_address_label(picked)!r} applied to "
        f"line(s) {slot['line_indices']} for {slot['name']} "
        f"(slot {pos + 1}/{len(queue)})"
    )

    if pos + 1 < len(queue):
        return _ask_for_bulk_address(
            lines, queue, pos + 1, conversation, user_context, page, start_time
        )

    user_context.pop("bulk_address_queue", None)
    user_context.pop("bulk_address_pos", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    return _continue_after_addresses_chosen(
        lines, store_loader, conversation, user_context, page, start_time
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


def _continue_after_addresses_chosen(lines_as_dicts, store_loader, conversation, user_context, page, start_time):
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
        and _is_variable_product(l["product_id"], store_loader)
        and (not l.get("variation_id")
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

    # Everything the rep can supply is now supplied EXCEPT the address, so
    # collect that next — the summary comes after, not before.
    #
    # This used to render the summary here and start the address loop only
    # once the rep pressed "Yes, confirm". That asked them to approve a table
    # marked "Ready / N orders ready to place" while no address existed, and
    # made "Yes, confirm" mean "begin collecting addresses" rather than
    # "place the order". Address first, then a summary showing the real
    # destination, then a confirm that actually places.
    _resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
    if _resolved:
        user_context["bulk_current_line_index"] = 0
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            _resolved, 0, conversation, user_context, page, start_time,
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
        # Every address settled — NOW show the summary. It can finally state a
        # real recipient and destination, and its confirm button places the
        # orders rather than starting another data-collection step.
        lines = user_context.get("pending_bulk_lines", [])
        return _build_bulk_confirmation_response(
            lines, conversation, user_context, page, start_time,
        )

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

    # Address-only mode (company has delivery history but no customer
    # account) leaves customer_id None. int(None) raised here and killed
    # the whole card — which is how a chosen address showed up blank.
    _cid = current_line.get("customer_id")
    if not shipping_block.get("address_1") and _cid:
        try:
            cust_call = endpoints.fetch_customer(
                customer_id=int(_cid),
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
        "customer_name": _line_recipient_display(current_line),
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
        f"**Order for {_line_recipient_display(current_line)}** "
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

def _address_group_key(addr) -> str:
    """
    Canonical key for "is this the same destination?".
    
    Only the fields that define a destination, normalised. Hashing the whole
    dict split lines that ship to the identical address purely because the two
    code paths that build them differ in shape — the parser's roster entry
    carries an empty "phone" key and the recipient-reply path does not, so one
    person's two products became two orders.
    """
    a = addr or {}
    fields = (
        "first_name", "last_name", "company",
        "address_1", "address_2", "city", "state", "postcode", "country",
    )
    return "|".join(
        re.sub(r"\s+", " ", str(a.get(f) or "")).strip().lower()
        for f in fields
    )


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
        # The RECIPIENT NAME is part of the key, not just customer_id.
        # With no customer account (address-only mode) every line carries
        # customer_id=None, so two different people ordering to the same
        # company address collapsed into ONE order labelled with whichever
        # line came first — "1× London for Kevin Shuker" when London was
        # Tamra Smith's. Different named recipients stay separate orders.
        _recipient_key = (
            line.get("recipient_name")
            or line.get("customer_display_name")
            or ""
        ).strip().lower()
        _key = (
            str(line.get("customer_id")),
            _recipient_key,
            _address_group_key(_billing),
            _address_group_key(_shipping),
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

def handle_bulk_company_choice_reply(
    message, store_loader, conversation, user_context, page, start_time,
):
    """Rep picked which company the bulk order is for (stage 1 of 2).

    Narrows the roster to that company, then falls through to the recipient
    question. Matching is exact-then-unique-partial: an ambiguous reply
    re-asks rather than guessing, since picking the wrong company here would
    silently ship to a different business.
    """
    pending   = (user_context or {}).get("pending_company_choice") or {}
    companies = pending.get("companies") or []
    roster    = user_context.get("bulk_company_roster", []) or []
    queue     = user_context.get("bulk_recipient_queue") or []
    pos       = user_context.get("bulk_recipient_pos", 0)

    def _norm(v):
        return re.sub(r'[^a-z0-9]+', ' ', str(v or "").lower()).strip()

    reply = _norm(message)


    picked = None
    if reply:
        exact = [c for c in companies if _norm(c) == reply]
        if len(exact) == 1:
            picked = exact[0]
        else:
            partial = [c for c in companies if reply in _norm(c)]
            if len(partial) == 1:
                picked = partial[0]

    if not picked:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "I couldn't tell which company you meant. Please pick one:",
            "intent": "guided_flow",
            "products": [],
            "suggestions": companies[:8] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
                "candidates": companies,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_COMPANY_CHOICE.value,
            "pagination": default_pagination(page),
        }), 200

    # Narrow the roster to the chosen company. The flag stops the question
    # being asked again for later recipients in the same order.
    _p = _norm(picked)
    filtered = [r for r in roster if _norm(r.get("company")) == _p]

    logger.info(
        f"bulk_order | company choice '{picked}' → "
        f"{len(filtered)} of {len(roster)} roster entries"
    )

    user_context["bulk_company_roster"] = filtered or roster
    user_context["bulk_company_scope"]  = picked
    user_context["bulk_company_choice_made"] = True
    user_context.pop("pending_company_choice", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    lines_as_dicts = user_context.get("pending_bulk_lines", []) or []
    return _ask_for_bulk_recipient(
        lines_as_dicts, queue, pos, conversation, user_context, page, start_time,
    )