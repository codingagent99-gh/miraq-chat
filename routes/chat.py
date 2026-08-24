"""
Chat endpoint as a Flask Blueprint.
Fully migrated to persistent PostgreSQL storage.
Refactored: business logic extracted into parsers/ and handlers/.
"""

import time
import uuid
from api_builder.store_helpers import attr_slug_for_label
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from models import db, Conversation, Message, Intent
from sqlalchemy.orm.attributes import flag_modified
import re
from classifier.consolidation import _resolve_tag_attribute_overlap
import json
from handlers.chat_utils import resolve_session_id
from classifier.utils import normalize_for_tag_compare
from config.store_config import ATTRIBUTE_DISAMBIGUATION_GROUPS
from models import ExtractedEntities, ClassifiedResult, WooAPICall
from app_config import (
    ORDER_INTENTS,
    CART_INTENTS,
    is_order_report_admin,
    ORDER_CREATE_INTENTS,
    CLASSIFIER_PROVIDER_TAG,
    BULK_ORDER_ROLES,
    ECOMMERCE_BACKEND,
    get_currency_symbol,
)
from core.actions import build_open_checkout_panel, build_open_cart_panel
from woo_client import woo_client
from formatters import format_product, format_custom_product, format_category, _entities_to_dict
from response_generator import (
    generate_bot_message, generate_suggestions, _resolve_user_placeholders,
    _resolve_category_breadcrumb, _resolve_tag_name,
    _resolve_attribute_label, _resolve_attribute_term_name,
)
from classifier import classify
from api_builder import build_api_calls
from conversation_flow import FlowState, handle_flow_state, is_order_flow, _flow_context_message, is_bare_exit

# Maps a flow-state STRING that used to be a valid FlowState value onto its
# current replacement. Consulted only when FlowState(conversation.flow_state)
# raises — i.e. the persisted string no longer matches any live enum member —
# so a conversation row written before a rename ships still resumes where it
# left off instead of silently resetting to IDLE.
#
# Add an entry here whenever a FlowState member's .value changes; remove it
# once enough time has passed that no live conversation could still carry the
# old string (rows are per-session, not long-lived, so this does not need to
# stay forever).
_LEGACY_FLOW_STATE_ALIASES = {
    # "awaiting_order_for_email" -> AWAITING_ORDER_FOR_CUSTOMER: the order-for
    # flow stopped using email as the customer identifier (name + company
    # now), and the state name changed to match.
    "awaiting_order_for_email": FlowState.AWAITING_ORDER_FOR_CUSTOMER,
}
from chat_logger import get_logger, sanitize_log_string
from store_registry import get_store_loader
from ecommerce import endpoints
from ecommerce.cart_actions import build_cart_add_action
from ecommerce.unsupported import (
    find_unsupported_call,
    message_for as unsupported_message_for,
)
from models.usage_guard import enforce_daily_limit

from handlers.chat_utils import default_pagination, build_pagination, format_order_for_frontend
from handlers.flow_handler import handle_flow
from handlers.llm_handler import run_llm_fallback
from handlers.order_handler import (
    handle_reorder,
    handle_order_detail,
    handle_quick_order,
    handle_historical_search,
    handle_order_status,
)
from handlers.variant_handler import handle_variant_selection, handle_variation_product, handle_quantity_and_variant_check
from handlers.search_handler import log_matched_products, handle_empty_results
from handlers.suggestion_retry_handler import handle_suggestion_retry
from handlers.filter_clarification_handler import resolve_filter_clarification, apply_semantic_match
from handlers.semantic_clarification_handler import build_semantic_clarification
from handlers.search_refinement import (
    merge_into_active_search, save_active_search, clear_active_search,
    describe_active_filters, describe_active_filters_labeled,
    detect_slot_conflicts, active_search_is_fresh,
)
from parsers.catalog_parser import _detect_explicit_taxonomy_signal
from handlers.refinement_choice_handler import build_refinement_prompt, resolve_refinement_choice
from handlers.no_results_choice_handler import build_no_results_prompt, resolve_no_results_choice
from config.store_config import SEMANTIC_AUTO_APPLY_THRESHOLD, ATTRIBUTE_DISAMBIGUATION_GROUPS
from parsers.catalog_parser import parse_csv_message
from parsers.address_parser import extract_address, address_summary
from utils.language_utils import detect_and_translate
from handlers.cart_handler import handle_cart_intent
from handlers.order_stats_handler import (
    handle_order_stats,
    handle_rep_choice_reply,
    handle_date_range_reply,
    prompt_for_order_list_range,
)
from core.actions import build_propose_checkout_address
from utils.rep_utils import (
    fetch_product_order_history  as _fetch_product_order_history,
    format_product_orders_for_action as _format_product_orders_for_action,
)
# ── Bulk order & sales rep handlers (top-level; no deferred imports needed) ──
from handlers.bulk_order_handler import (
    handle_bulk_order_trigger,
    handle_bulk_order_input,
    handle_bulk_order_confirmation,
    handle_bulk_address_confirmation_reply,
    handle_bulk_variant_selection_reply,
    handle_bulk_email_reply,
    handle_bulk_company_reply,
    handle_bulk_company_choice_reply,
    handle_bulk_recipient_reply,
    handle_bulk_recipient_mode_reply,
    handle_bulk_address_choice_reply,
    handle_bulk_product_reply,
    handle_bulk_quantity_reply,
    handle_cancel_bulk_order,
    handle_bulk_confirmation_unclear,
    handle_product_reorder,
)
from handlers.sales_rep_handler import (
    handle_order_for_email_reply,
    handle_order_for_prompt,
)

import re
from config.store_config import GENERIC_WORD_SYNONYMS

def _apply_generic_word_synonyms(text: str) -> str:
    """
    Swap user-facing vocabulary for the internal word every downstream
    extractor/classifier already understands (e.g. "material" → "category",
    since this store's UI calls categories "Material" but the codebase's
    matching logic everywhere is keyed on the word "category"). Runs once,
    on the final text, so no individual call site needs its own synonym list.
    """
    for user_word, internal_word in GENERIC_WORD_SYNONYMS.items():
        text = re.sub(rf'\b{re.escape(user_word)}\b', internal_word, text, flags=re.IGNORECASE)
    return text

logger = get_logger("miraq_chat")
chat_bp = Blueprint("chat", __name__)


# ══════════════════════════════════════════════════════════════
# ─── MODULE-LEVEL HELPERS ───
# ══════════════════════════════════════════════════════════════

from parsers.bulk_order_parser import COMPANY_SCOPE_TAIL_RE, EXPLICIT_COMPANY_SCOPE_RE


def _recipient_scope_tokens(message: str, is_bulk: bool = False) -> list:
    """
    Tokens sitting in a bulk-order scope tail — after "for" or "at".

    These are proper nouns ("... Adams Grey at Beck", "... for Abel Design
    Group") and must never be fuzzy-matched against catalog vocabulary. Left
    unprotected, "beck" is corrected to the attribute term "back" and the
    company lookup silently runs against the wrong company; "abel" ties against
    ['abeto','azul','area','panel'] and hijacks the turn into a typo
    clarification chip before the parser ever sees it.

    ONLY applied to bulk orders. The marker words are far too common in
    ordinary questions — "look at the blue tiles", "show me bella at 12x12" —
    to blanket-protect; doing so would switch typo correction off across much
    of the catalog vocabulary.

    The marker list comes from COMPANY_SCOPE_TAIL_RE in the bulk parser, so a
    marker added to the format is protected here automatically rather than
    needing the same edit in two files.

    Returned lowercase for correct_message(suppressed_tokens=...), which passes
    them through verbatim and does not raise them as ambiguities.
    """
    if not message or not is_bulk:
        return []

    # The marker words themselves must be protected too. "company" is not a
    # catalog term, so the corrector ties it against distant tile names
    # ('bombay', 'romano' at edit distance 3) and hijacks the whole turn
    # into a typo-clarification chip before the parser ever runs.
    tokens = ["for", "at", "company"]
    for tail in COMPANY_SCOPE_TAIL_RE.findall(message):
        for tok in re.split(r'[^A-Za-z]+', tail):
            if len(tok) > 1:
                tokens.append(tok.lower())
    return tokens


def _is_inline_bulk_order(message: str, store_loader=None) -> bool:
    # Check 1: comma-separated fragments with quantities (original)
    fragments = [f.strip() for f in message.split(",") if f.strip()]
    qualified = sum(
        1 for f in fragments
        if re.search(r"\d", f) and (
            re.search(r"\bfor\b", f, re.I) or
            re.search(r"\b(order|buy|purchase|reorder|re-order)\b", f, re.I)
        )
    )
    if qualified >= 2:
        return True

    # Check 2: 2+ resolvable catalog products — same logic as BulkOrderEvaluator
    if store_loader and store_loader.products:
        _name_set = {p["name"].lower() for p in store_loader.products if p.get("name")}
        resolved_count = sum(
            1 for name in _name_set
            if re.search(r"\b" + re.escape(name) + r"\b", message, re.I)
        )
        if resolved_count >= 2:
            return True

        # Check 3: ONE product + an explicit company. Mirrors the matching
        # branch in BulkOrderEvaluator, and must stay in step with it.
        #
        # This function gates _company_scope_tokens, which runs BEFORE
        # classification. Without this branch, "order allspice chipcard for
        # gensler company" was not bulk here, so "company" never reached the
        # suppressed-token list — and the typo corrector tied it against
        # 'romano'/'bombay' at edit distance 3 and hijacked the turn into a
        # clarification chip. The classifier never saw the message at all, so
        # the evaluator's own company branch could not fire.
        #
        # The guard already named "company" as a token to protect; it simply
        # could not be reached for a single-product message.
        if resolved_count >= 1 and EXPLICIT_COMPANY_SCOPE_RE.search(message):
            return True

    return False


def _merge_phase_entities(result):
    """
    Restore phase-1 entity richness when the phase-2 classifier pass degraded it.

    parse_csv_message runs a second pass on attribute-stripped text. When that
    text is nearly empty the second pass returns blank entities and a weaker
    intent, overwriting the correct product_id / attributes resolved in pass 1.
    We surface the pass-1 result through result.phase1_entities and merge it
    back here.

    Returns (intent, entities, confidence).
    """
    intent     = result.intent
    entities   = result.entities
    confidence = result.confidence

    phase1 = getattr(result, "phase1_entities", None)
    logger.debug(
        f"[MERGE_PHASE_TRACE] phase1_attrs={getattr(phase1, 'attributes', None)} | "
        f"entities_attrs={entities.attributes}"
    )
    if phase1 is None:
        return intent, entities, confidence

    # Restore product identity if pass-2 lost it
    if not entities.product_id and phase1.product_id:
        entities.product_id   = phase1.product_id
        entities.product_name = phase1.product_name
        entities.product_slug = phase1.product_slug
        logger.debug(
            f"[EntityMerge] Restored product_id={phase1.product_id} "
            f"name='{phase1.product_name}' from phase-1 entities"
        )

    # Merge attributes: keep all phase-1 keys that pass-2 dropped.
    # Different extraction passes can spell the same attribute differently
    # (e.g. "tile size" vs "tile-size") — only the spelling that matches
    # WooCommerce's human-readable label actually resolves via
    # attr_slug_for_label. When two keys represent the same attribute, keep
    # whichever one resolves; if neither does, leave the existing one alone.
    if phase1.attributes:
        merged_attrs = dict(entities.attributes)
        for p1_key, p1_val in phase1.attributes.items():
            p1_norm = normalize_for_tag_compare(p1_key.replace("-", " "))
            equivalent_key = next(
                (k for k in merged_attrs
                 if normalize_for_tag_compare(k.replace("-", " ")) == p1_norm),
                None
            )
            if equivalent_key is None:
                # Don't blindly restore a phase-1 key whose disambiguation
                # sibling is ALREADY present and resolved (e.g. user said
                # "tile size" explicitly; the precise catalog match correctly
                # resolved only tile-size, omitting sample-size — phase-1's
                # older, less precise pass guessed both, but that's not a
                # loss to restore, it's a deliberate disambiguation phase-2
                # already made correctly). See ATTRIBUTE_DISAMBIGUATION_GROUPS.
                p1_key_lower = p1_key.lower().strip()
                sibling_already_resolved = any(
                    p1_key_lower in group and any(
                        normalize_for_tag_compare(k.replace("-", " "))
                        == normalize_for_tag_compare(sibling.replace("-", " "))
                        for k in merged_attrs
                        for sibling in group
                        if sibling != p1_key_lower
                    )
                    for group in ATTRIBUTE_DISAMBIGUATION_GROUPS
                )
                if not sibling_already_resolved:
                    merged_attrs[p1_key] = p1_val
            elif attr_slug_for_label(p1_key) and not attr_slug_for_label(equivalent_key):
                merged_attrs[p1_key] = merged_attrs.pop(equivalent_key)

        if merged_attrs != entities.attributes:
            logger.debug(
                f"[EntityMerge] attributes merged: "
                f"phase1={phase1.attributes} + phase2={entities.attributes} "
                f"→ {merged_attrs}"
            )
        entities.attributes = merged_attrs
        

        # Re-run tag/attribute overlap resolution — merging phase-1's
        # attributes on top of phase-2's tag_slugs (set independently by
        # phase1_catalog_match, which never calls this consolidation step
        # itself) can introduce a FRESH tag+attribute collision that didn't
        # exist when either individual pass checked for one on its own
        # entities (e.g. tag_slugs=['quick-ship'] from catalog match +
        # attributes['quick-ship']='yes' restored from the NLP pass —
        # same concept, two representations, never reconciled until now).
        _resolve_tag_attribute_overlap(entities)

    # Restore attribute_term_ids if pass-2 lost them
    if not entities.attribute_term_ids and getattr(phase1, "attribute_term_ids", None):
        entities.attribute_term_ids = phase1.attribute_term_ids
        entities.attribute_slug     = phase1.attribute_slug
        logger.debug(
            f"[EntityMerge] Restored attribute_term_ids={phase1.attribute_term_ids}"
        )

    # Restore lookup_email if pass-2 stripped the email from its text.
    # Phase-2 runs on attribute-masked text, which removes the '@domain' token,
    # so extract_email finds nothing on pass-2 and the address is lost without this.
    if not entities.lookup_email and getattr(phase1, "lookup_email", None):
        entities.lookup_email = phase1.lookup_email
        logger.debug(
            f"[EntityMerge] Restored lookup_email={phase1.lookup_email!r} from phase-1 entities"
        )

    # Restore date range if pass-2 lost it (same masking risk as lookup_email).
    if not entities.date_after and getattr(phase1, "date_after", None):
        entities.date_after  = phase1.date_after
        entities.date_before = phase1.date_before
        logger.debug(
            f"[EntityMerge] Restored date_after={phase1.date_after!r} from phase-1 entities"
        )

    # Use pass-1 intent/confidence when pass-2 fell back to a weaker signal
    _WEAK_INTENTS = {Intent.PRODUCT_SEARCH}
    if result.intent in _WEAK_INTENTS and getattr(result, "phase1_intent", None) not in _WEAK_INTENTS:
        _old_intent    = result.intent
        intent         = result.phase1_intent
        result.intent  = intent
        confidence     = max(confidence, result.phase1_confidence or confidence)
        logger.debug(
            f"[EntityMerge] Upgraded intent {_old_intent} → {intent} from phase-1"
        )
    
    if intent == Intent.UNKNOWN and entities.semantic_auto_applied:
        _old_intent = intent
        intent = Intent.FILTER_BY_ATTRIBUTE
        confidence = max(confidence, 0.9)
        result.intent = intent
        logger.debug(f"[EntityMerge] Upgraded intent {_old_intent} → {intent} — fresh semantic auto-materialize this turn")

    return intent, entities, confidence


def _dispatch_bulk_action(action, message, role, store_loader, conversation, user_context, page, start_time, customer_id=None):
    """
    Route a bulk-order or sales-rep flow action to its handler.
    Returns a Flask response tuple, or None if the action is not recognised.
    """
    if action == "process_bulk_input":
        return handle_bulk_order_input(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_variant_selection":
        return handle_bulk_variant_selection_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_address_choice_reply":
        return handle_bulk_address_choice_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_recipient_mode_reply":
        return handle_bulk_recipient_mode_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_company_choice_reply":
        return handle_bulk_company_choice_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_rep_choice_reply":
        # Admin disambiguating which rep an order report refers to.
        return handle_rep_choice_reply(
            message, conversation, user_context, page, start_time, customer_id
        )
    elif action == "process_date_range_reply":
        # Admin answering "which period should I cover?" for an order report.
        # Returns a Flask response for the stats report, or a plain dict when
        # the window belongs to the ALL-ORDERS list — that one is answered by
        # the normal API-call pipeline further down, so it can't be completed
        # from inside the handler. The dict is passed back to the caller.
        return handle_date_range_reply(
            message, conversation, user_context, page, start_time, customer_id
        )
    elif action == "process_bulk_recipient_reply":
        return handle_bulk_recipient_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_company_reply":
        return handle_bulk_company_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_email_reply":
        return handle_bulk_email_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "process_bulk_product_reply":
        return handle_bulk_product_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action == "confirm_bulk_order":
        return handle_bulk_order_confirmation(
            user_context, conversation, page, start_time
        )
    elif action == "cancel_bulk_order":
        return handle_cancel_bulk_order(
            user_context, conversation, page, start_time
        )
    elif action == "bulk_confirmation_unclear":
        return handle_bulk_confirmation_unclear(
            conversation, page, start_time
        )
    elif action == "process_bulk_quantity_reply":
        return handle_bulk_quantity_reply(
            message, store_loader, conversation, user_context, page, start_time
        )
    elif action in (
        "bulk_address_confirmed",
        "bulk_address_change",
        "bulk_address_override_text",
        "bulk_address_override_structured",
        "bulk_address_skip",
    ):
        return handle_bulk_address_confirmation_reply(
            action, message, conversation, user_context, page, start_time
        )
    elif action == "resolve_order_for_email" and role in BULK_ORDER_ROLES:
        return handle_order_for_email_reply(
            message, conversation, user_context, page, start_time,
            customer_id=customer_id,
        )
    return None


def _handle_cart_flow(action, user_context, conversation, store_loader, page, start_time):
    """
    Handle cart confirmation flow actions returned by handle_flow_state.

    Covers the three cart-confirmation actions that live inside the flow
    state machine rather than as standalone intents:
      - prompt_cart_confirmation  (AWAITING_QUANTITY → ask "add to cart?")
      - confirm_add_to_cart       (AWAITING_CART_CONFIRMATION → Yes)
      - decline_add_to_cart       (AWAITING_CART_CONFIRMATION → No)

    Returns a raw Flask response (caller wraps with _ft), or None if the
    action is not a cart-flow action.
    """
    if action == "prompt_cart_confirmation":
        pid      = user_context.get("pending_product_id")
        vid      = user_context.get("pending_variation_id")
        qty      = user_context.get("pending_quantity") or 1
        name     = user_context.get("pending_product_name", "item")
        resolved = user_context.get("resolved_attributes") or {}
        variant_label  = " / ".join(str(v) for v in resolved.values()) if resolved else ""
        variant_suffix = f" ({variant_label})" if variant_label else ""

        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": f"Got it — add **{name}**{variant_suffix} ×{qty} to your cart?",
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["Yes, add it", "No thanks"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":           FlowState.AWAITING_CART_CONFIRMATION.value,
                "pending_product_id":   pid,
                "pending_product_name": name,
                "pending_quantity":     qty,
                "pending_variation_id": vid,
                "resolved_attributes":  resolved,
                "response_time_ms":     elapsed,
            },
            "flow_state":  FlowState.AWAITING_CART_CONFIRMATION.value,
            "pagination":  default_pagination(page),
            "actions":     [],
        })

    if action == "confirm_add_to_cart":
        pid      = user_context.get("pending_product_id")
        vid      = user_context.get("pending_variation_id")
        qty      = user_context.get("pending_quantity") or 1
        name     = user_context.get("pending_product_name", "item")
        resolved = user_context.get("resolved_attributes") or {}

        if not pid:
            return None

        # Backend decides the action shape. Previously this branched on the
        # VARIATION id being a GID, so a Shopify simple product (no variation
        # id) fell into the Woo branch and shipped a product GID in the
        # variant slot — an add that always failed in the browser.
        _action, _err = build_cart_add_action(
            product_id=pid,
            quantity=qty,
            name=name,
            variation_id=vid,
            resolved_attrs=resolved,
            store_loader=store_loader,
        )

        if _action is None:
            # Several variants and nothing selects between them. Ask rather
            # than guess — silently adding the wrong size/colour is worse.
            logger.info(
                f"confirm_add_to_cart: unresolved variant | product={pid!r} "
                f"reason={_err}"
            )
            return jsonify({
                "success":     True,
                "bot_message": (
                    f"I need to know which version of **{name}** you'd like "
                    "before I add it to your cart."
                ),
                "intent":      Intent.ADD_TO_CART.value,
                "suggestions": ["Show me the options", "Cancel"],
                "session_id":  str(conversation.id),
                "pagination":  default_pagination(page),
                "flow_state":  FlowState.AWAITING_VARIANT_SELECTION.value,
                "actions":     [],
            })

        actions = [_action]

        return jsonify({
            "success":     True,
            "bot_message": f"Adding **{name}** ×{qty} to your cart…",
            "intent":      Intent.ADD_TO_CART.value,
            "suggestions": ["Proceed to checkout", "Continue shopping", "View cart"],
            "session_id":  str(conversation.id),
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
            "actions":     actions,
        })

    if action == "decline_add_to_cart":
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": "No problem! What else are you looking for?",
            "intent":      "browse",
            "products":    [],
            "suggestions": ["Browse Products", "View categories", "View cart"],
            "session_id":  str(conversation.id),
            "metadata":    {"response_time_ms": elapsed},
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
        })

    return None

# ══════════════════════════════════════════════════════════════
# ─── ADDRESS PROPOSAL ───
# ══════════════════════════════════════════════════════════════

def _maybe_attach_address_proposal(
    response_data: dict,
    message: str,
    customer_id,
    current_flow_state=None,
) -> None:
    """
    Inspect *message* for a plausible postal address.  When one is found,
    append a PROPOSE_CHECKOUT_ADDRESS action to response_data["actions"] and
    update bot_message / suggestions.

    Skipped during multi-turn flows (variant labels, quantity replies, etc.
    commonly contain dimension strings or comma-delimited tokens that look
    like addresses but aren't).

    Mutates *response_data* in-place; returns None.  Silent on any error.
    """
    if not customer_id:
        return
    # Only propose addresses from a clean conversational turn — never from
    # a guided flow reply (variant text, quantity, "Yes, add it", etc.)
    if current_flow_state is not None and current_flow_state != FlowState.IDLE:
        return

    try:
        parsed = extract_address(message)
        if not parsed:
            return

        # Fetch the customer's saved billing/shipping for the "existing_on_file" field.
        # Skipped on Shopify: fetch_customer is an unimplemented shopify_admin
        # stub (the woo_client backstop would block it anyway). The proposal
        # still works — it just has no address on file to compare against.
        existing = None
        if ECOMMERCE_BACKEND != "shopify":
            try:
                from woo_client import woo_client as _woo
                cust_resp = _woo.execute(endpoints.fetch_customer(
                    customer_id=customer_id,
                    description="Fetch customer address for PROPOSE_CHECKOUT_ADDRESS",
                ))
                if cust_resp.get("success") and isinstance(cust_resp.get("data"), dict):
                    _billing  = cust_resp["data"].get("billing", {})
                    _shipping = cust_resp["data"].get("shipping", {})
                    existing  = _shipping if (_shipping.get("address_1") or _shipping.get("city")) else (
                        _billing if (_billing.get("address_1") or _billing.get("city")) else None
                    )
            except Exception:
                pass  # proceed without existing address

        action = build_propose_checkout_address(parsed=parsed, existing_on_file=existing)
        actions = response_data.get("actions")
        if not isinstance(actions, list):
            actions = []
        actions.append(action)
        response_data["actions"] = actions

        summary = address_summary(parsed)
        response_data["bot_message"] = (
            f"I noticed an address — **{summary}**. "
            "Would you like to use it for shipping?"
        )
        response_data["suggestions"] = [
            "Use the new address",
            "Use my saved address",
            "Let me type a different one",
        ]
    except Exception as exc:
        logger.warning(f"_maybe_attach_address_proposal failed silently | error={exc}")


# ══════════════════════════════════════════════════════════════
# ─── TYPO CORRECTION NOTE ───
# ══════════════════════════════════════════════════════════════

def _display_message(content: str) -> str:
    """Friendly text for a stored user message that carries a structured payload.

    Cards submit "__SENTINEL__<json>" so the flow handlers get exact data, and
    that raw string is what lands in the messages table. Replaying it into the
    transcript shows the user a wall of JSON they never typed, so it is
    rewritten on the way out. Anything unrecognised is returned untouched.
    """
    if not isinstance(content, str):
        return content
    text = content.strip()

    if text.startswith("__DATE_RANGE__"):
        try:
            payload = json.loads(text[len("__DATE_RANGE__"):] or "{}")
        except (ValueError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            if payload.get("all_time"):
                return "📅 All time"
            after, before = payload.get("after"), payload.get("before")
            if after and before:
                return f"📅 {after} to {before}"
        return "📅 Selected a date range"

    if text.startswith("__BULK_ADDR__"):
        return "✏️ Updated billing & shipping address"

    return content


def _build_typo_correction_note(corrections: list, found_results: bool = True) -> str:
    """
    Render a short, honest note when we silently corrected misspelled terms
    before searching — so the shopper knows we didn't find their exact
    words, and what we substituted instead, rather than just showing
    results with no explanation.

    `found_results` controls phrasing only (deterministic, no LLM call —
    we already know both the correction pairs and the search outcome by
    the time this runs, so a template is enough): when the corrected
    search still came back empty, generate_bot_message() will already say
    "I couldn't find any products matching X" right below this note, so we
    switch to "I corrected X to Y" instead of "couldn't find X" to avoid
    saying "couldn't find" twice in the same response.

    Skips "manual_override" entries in the note text alongside fuzzy ones
    using the same "original -> corrected" pairing; distance is irrelevant
    to the shopper so it's omitted.
    """
    if not corrections:
        return ""
    if len(corrections) == 1:
        c = corrections[0]
        if found_results:
            return f"Couldn't find \"{c['original']}\" — showing results for **{c['corrected']}** instead."
        return f"I corrected \"{c['original']}\" to **{c['corrected']}**."
    pairs = ", ".join(f"\"{c['original']}\" → **{c['corrected']}**" for c in corrections)
    if found_results:
        return f"Couldn't find an exact match for a couple of terms, so I corrected: {pairs}."
    return f"I corrected a couple of terms: {pairs}."


# ══════════════════════════════════════════════════════════════
# ─── DATABASE SESSION HELPERS ───
# ══════════════════════════════════════════════════════════════


def _finalize_turn(
    conversation,
    flask_response,
    *,
    _proposal_message=None,
    _proposal_customer_id=None,
    _proposal_flow_state=None,
    _typo_corrections=None,
):
    """
    Interceptor: Extracts bot message from Flask response, saves to DB,
    commits the transaction, and returns the updated response.
    """
    if isinstance(flask_response, tuple):
        resp_obj, status_code = flask_response
    else:
        resp_obj, status_code = flask_response, 200

    try:
        data = resp_obj.get_json()
    except Exception:
        data = {}

    if not data:
        db.session.commit()
        return flask_response

    # 0. Optionally attach PROPOSE_CHECKOUT_ADDRESS action
    if _proposal_message and _proposal_customer_id:
        _maybe_attach_address_proposal(
            data,
            _proposal_message,
            _proposal_customer_id,
            current_flow_state=_proposal_flow_state,
        )

    # 0.5. Prepend a note when this turn silently corrected misspelled terms.
    if _typo_corrections and data.get("bot_message"):
        _found_results = bool(data.get("products")) or bool(data.get("categories"))
        _typo_note = _build_typo_correction_note(_typo_corrections, found_results=_found_results)
        if _typo_note:
            data["bot_message"] = f"{_typo_note}\n\n{data['bot_message']}"

    combined_metadata = data.get("metadata", {}).copy()
    combined_metadata["products"]    = data.get("products", [])
    combined_metadata["categories"]  = data.get("categories", [])
    combined_metadata["suggestions"] = data.get("suggestions", [])
    combined_metadata["actions"]     = data.get("actions", [])
    # Order lists travel at the TOP level of the response, not inside
    # metadata, so they were never stored here and never came back from
    # /chat/history — an order list rendered live and then vanished into a
    # bare paragraph on reload, leaving the summary text above a card list
    # that no longer existed. The frontend has always been ready for these
    # (mapHistoryEntryToMessage reads m.orders and m.order_pagination); only
    # the backend half was missing. Stored only when present, same as
    # products.
    if data.get("orders"):
        combined_metadata["orders"] = data.get("orders")
        if data.get("order_pagination"):
            combined_metadata["order_pagination"] = data.get("order_pagination")

    # 1. Save Bot Message
    bot_msg = Message(
        conversation_id=conversation.id,
        role="bot",
        content=data.get("bot_message", ""),
        intent=data.get("intent", ""),
        metadata_json=combined_metadata,
    )
    db.session.add(bot_msg)

    # 2. Update Conversation State
    conversation.flow_state = data.get("flow_state", conversation.flow_state)

    context_data = dict(conversation.context_data)

    _WIPE_KEYS = [
        "pending_product_id", "pending_product_name",
        "pending_quantity", "pending_variation_id", "resolved_attributes",
    ]

    if conversation.flow_state in ("idle", "awaiting_anything_else", "closing"):
        for k in _WIPE_KEYS:
            context_data.pop(k, None)
        if "metadata" in data:
            for k in _WIPE_KEYS:
                data["metadata"].pop(k, None)
    else:
        if "metadata" in data:
            for k in _WIPE_KEYS:
                if k in data["metadata"] and data["metadata"][k] is not None:
                    context_data[k] = data["metadata"][k]

    conversation.context_data = context_data
    flag_modified(conversation, "context_data")

    # 3. Commit
    db.session.commit()

    # 4. Inject session ID
    data["session_id"] = str(conversation.id)

    # 5. Inject actions array (always present)
    raw_actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    data["actions"] = list(raw_actions)

    return jsonify(data), status_code


# ══════════════════════════════════════════════════════════════
# ─── HISTORY ROUTE ───
# ══════════════════════════════════════════════════════════════

@chat_bp.route("/chat/history", methods=["GET"])
def get_chat_history():
    """Fetches paginated chat history for the frontend to hydrate the UI."""
    miraq_session = request.headers.get("X-MiraQ-Session")
    if not miraq_session:
        return jsonify({"messages": [], "has_more": False}), 200

    try:
        session_uuid = uuid.UUID(miraq_session)
        conversation = Conversation.query.get(session_uuid)

        if not conversation:
            return jsonify({"messages": [], "has_more": False}), 200

        page  = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 20))
        offset = (page - 1) * limit

        messages_query = (
            Message.query
            .filter_by(conversation_id=session_uuid)
            .order_by(Message.created_at.desc())
            .limit(limit).offset(offset).all()
        )

        total_messages = Message.query.filter_by(conversation_id=session_uuid).count()
        has_more = (offset + limit) < total_messages

        messages_query.reverse()

        history = []
        for msg in messages_query:
            item = {
                "role":      msg.role,
                # Structured picks are stored verbatim so the flow handlers can
                # re-read them, but the raw "__SENTINEL__{json}" is not what the
                # user typed and must never surface in a replayed transcript.
                # The live path masks these in the widget; this covers reload.
                "message":   _display_message(msg.content) if msg.role == "user" else msg.content,
                "intent":    msg.intent,
                "timestamp": msg.created_at.isoformat(),
            }
            if msg.role == "bot" and msg.metadata_json:
                item["products"]    = msg.metadata_json.get("products", [])
                item["categories"]  = msg.metadata_json.get("categories", [])
                item["suggestions"] = msg.metadata_json.get("suggestions", [])
                item["actions"]     = msg.metadata_json.get("actions", [])
                # Top-level on a live response, so they must be top-level here
                # too — mapHistoryEntryToMessage reads m.orders, not
                # m.metadata.orders. Only emitted when the turn actually had
                # an order list, so ordinary messages are unchanged.
                _orders = msg.metadata_json.get("orders")
                if _orders:
                    item["orders"] = _orders
                    _opg = msg.metadata_json.get("order_pagination")
                    if _opg:
                        item["order_pagination"] = _opg
                item["metadata"]    = {
                    k: v for k, v in msg.metadata_json.items()
                    if k not in ("products", "categories", "suggestions",
                                 "actions", "orders", "order_pagination")
                }
            history.append(item)

        return jsonify({
            "messages":  history,
            "has_more":  has_more,
            "next_page": page + 1 if has_more else None,
        }), 200

    except ValueError:
        return jsonify({"messages": [], "has_more": False}), 200


# ══════════════════════════════════════════════════════════════
# ─── HELPER: Wipe stale cart context ───
# ══════════════════════════════════════════════════════════════

def _wipe_stale_cart(conversation, user_context, current_flow_state):
    """Clear pending cart keys when flow is idle."""
    if current_flow_state not in (FlowState.IDLE, FlowState.AWAITING_ANYTHING_ELSE):
        return
    keys = [
        "pending_product_id", "pending_product_name",
        "pending_quantity", "pending_variation_id", "resolved_attributes",
    ]
    wiped = False
    for k in keys:
        if k in user_context:
            user_context.pop(k)
            wiped = True
    if wiped:
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")


# ══════════════════════════════════════════════════════════════
# ─── HELPER: Empty order guard ───
# ══════════════════════════════════════════════════════════════

def _check_empty_order(intent, entities, conversation, page, start_time):
    if intent == Intent.BULK_ORDER:
        return None   # bulk orders carry no single product_id — skip this guard

    # Guard any intent that expects a concrete product.
    # QUICK_ORDER is intentionally excluded from ORDER_CREATE_INTENTS (it routes
    # to add-to-cart, not order creation), but it still needs a product to proceed.
    _order_guard_intents = set(ORDER_CREATE_INTENTS) | {Intent.QUICK_ORDER}
    if intent not in _order_guard_intents or getattr(entities, "product_id", None):
        return None

    p_name  = (getattr(entities, "product_name", None) or "").lower().strip()
    s_term  = (getattr(entities, "search_term", None) or "").lower().strip()
    generic = {"", "product", "a product", "the product", "item", "an item", "something", "anything", "order", "some"}

    if p_name not in generic or s_term not in generic:
        return None
    if getattr(entities, "attributes", {}) or getattr(entities, "target_category_slugs", set()):
        return None

    logger.info(f"🛑 Caught generic order words | p_name='{p_name}' | s_term='{s_term}'")
    elapsed = time.time() - start_time
    return _finalize_turn(conversation, jsonify({
        "success":     True,
        "bot_message": "To place an order, please include the product name! For example, you can type: **'I want to order Plumeria'**.",
        "intent":      "clarification_needed",
        "products":    [],
        "suggestions": ["Show me the catalog", "Cancel"],
        "session_id":  str(conversation.id),
        "metadata":    {"confidence": 1.0, "products_count": 0, "response_time_ms": round(elapsed * 1000)},
        "pagination":  default_pagination(page),
        "flow_state":  FlowState.IDLE.value,
    }))

# ══════════════════════════════════════════════════════════════
# ─── HELPER: Execute API and collect products ───
# ══════════════════════════════════════════════════════════════

def _execute_loader_memory_call(call) -> list:
    """Serve catalog metadata straight from the in-memory StoreLoader.

    Handles surface="loader_memory" calls built by api_builder's Shopify
    branches (CATEGORY_LIST / CATEGORY_BROWSE without slug / PRODUCT_CATALOG).
    Returns the same list-of-dicts shape the WooCommerce list_categories /
    list_tags responses produce, so downstream formatting is untouched.
    """
    loader = get_store_loader()
    if not loader:
        return []
    op = (getattr(call, "body", None) or {}).get("_op", "")
    if op == "list_categories":
        return [
            {"id": c.get("id"), "name": c.get("name", ""), "slug": c.get("slug", ""),
             "count": c.get("count", 0), "parent": c.get("parent", 0)}
            for c in (loader.categories or [])
            if c.get("slug") != "uncategorized"
        ]
    if op == "list_tags":
        return [
            {"id": t.get("id"), "name": t.get("name", ""), "slug": t.get("slug", ""),
             "count": t.get("count", 0)}
            for t in (loader.tags or [])
        ]
    logger.warning(f"_execute_loader_memory_call: unknown op {op!r}")
    return []


def _execute_api_calls(intent, api_calls, _resolve_variant):
    if _resolve_variant:
        return [], [], [], []

    if intent in ORDER_CREATE_INTENTS:
        api_calls_to_execute = [c for c in api_calls if not (c.method == "POST" and "/orders" in c.endpoint)]
    else:
        api_calls_to_execute = api_calls

    # ── split by surface ─────────────────────────────────────────────
    shopify_calls       = [c for c in api_calls_to_execute if getattr(c, "surface", "") == "shopify_graphql"]
    shopify_order_calls = [c for c in api_calls_to_execute if getattr(c, "surface", "") == "shopify_orders"]
    loader_memory_calls = [c for c in api_calls_to_execute if getattr(c, "surface", "") == "loader_memory"]
    woo_calls           = [c for c in api_calls_to_execute
                           if getattr(c, "surface", "") not in ("shopify_graphql", "shopify_orders", "loader_memory")]

    api_responses = woo_client.execute_all(woo_calls)

    for call in loader_memory_calls:
        try:
            api_responses.append({"success": True, "data": _execute_loader_memory_call(call), "call": call})
        except Exception as exc:
            logger.error(f"loader_memory call failed: {exc}", exc_info=True)
            api_responses.append({"success": False, "error": str(exc), "call": call})

    if shopify_order_calls:
        from api_builder.shopify_orders_executor import ShopifyOrdersExecutor
        orders_executor = ShopifyOrdersExecutor()
        for call in shopify_order_calls:
            try:
                result = orders_executor.execute(call)
                logger.debug(f"[DEBUG] Shopify order response data keys: {list(result.keys())}")
                api_responses.append({"success": True, "data": result, "call": call})
            except Exception as exc:
                logger.error(f"ShopifyOrdersExecutor failed: {exc}", exc_info=True)
                api_responses.append({"success": False, "error": str(exc), "call": call})

    if shopify_calls:
        from api_builder.shopify_graphql_executor import ShopifyGraphQLExecutor
        executor = ShopifyGraphQLExecutor(get_store_loader())
        for call in shopify_calls:
            try:
                result = executor.execute_from_body(call.body)
                api_responses.append({"success": True, "data": result, "call": call})
            except Exception as exc:
                logger.error(f"ShopifyGraphQLExecutor failed: {exc}", exc_info=True)
                api_responses.append({"success": False, "error": str(exc), "call": call})
    # ─────────────────────────────────────────────────────────────────────

    all_products_raw = []
    order_data       = []

    def _enrich(prod_list):
        for p in prod_list:
            if "type" not in p:
                p["type"] = "variable" if p.get("variations") else "simple"

    for resp in api_responses:
        if resp.get("success"):
            data   = resp.get("data")
            target = order_data if intent in ORDER_INTENTS else all_products_raw
            if isinstance(data, dict) and "products" in data:
                _enrich(data["products"])
                target.extend(data["products"])
            elif isinstance(data, dict) and "orders" in data:
                target.extend(data["orders"])
            elif isinstance(data, list):
                _enrich(data)
                target.extend(data)
            elif isinstance(data, dict):
                _enrich([data])
                target.append(data)

    return all_products_raw, order_data, api_responses, api_calls_to_execute


# ══════════════════════════════════════════════════════════════
# ─── HELPER: Per-product matched-filter context (for product card badges) ───
# ══════════════════════════════════════════════════════════════

def _compute_matched_against(raw_product: dict, formatted_product: dict, entities) -> list[str]:
    """
    Builds display labels for the "matched against" badge on the product card —
    EVERY category, tag, and attribute this product matched, including OR-pair
    branches (e.g. "Tile Floor" OR "Application: Floor"), not just the plain
    AND-required filters. Nothing here is capped or summarized — the badge has
    to reflect the full, accurate match context, not a partial view of it.

    Categories/tags are matched off the RAW product (slug-based — same extraction
    Step 3.1 already uses in log_matched_products, confirmed correct against
    production logs). Attributes are matched off the FORMATTED product instead:
    the raw attrs list from this pipeline comes back with empty options at this
    stage (Step 3.1's own debug log shows attrs={} even for products with real
    attribute data), while format_product() reliably resolves name/options by
    the time formatting is done.

    Attribute term matching uses case-insensitive EXACT equality against each
    formatted option, not substring containment. Substring matching was tried
    and reverted: it produced a false positive on "Application" — composite
    display strings like "Int: Wall, Floor, Wet Area; Ext: Wall" contain the
    word "Floor" but are NOT the actual taxonomy term match (confirmed against
    a real response where the aggregate breakdown showed "Application: 0"
    matched products, while substring matching incorrectly said they had).
    Net effect: attributes whose formatted options are composite/rolled-up
    strings (rather than one discrete value per option) won't get a badge for
    that attribute even when they're part of a genuine OR-pair match — a real
    gap, but a false "matched" badge would be worse than a missing one.

    entities.attr_tag_or_pairs is read as a list of plain dicts with
    .get('cat_slugs') / .get('tag_slug') / .get('attr_taxonomy') /
    .get('attr_term') — confirmed against the existing OR-pair handling
    earlier in this file (the _detect_explicit_taxonomy_signal block), which
    accesses it the same way.
    """
    labels = []

    p_cat_slugs = {c["slug"].lower() for c in raw_product.get("categories", []) if isinstance(c, dict) and c.get("slug")}
    p_tag_slugs = {t["slug"].lower() for t in raw_product.get("tags", []) if isinstance(t, dict) and t.get("slug")}
    formatted_attrs = formatted_product.get("attributes") or []

    def _attr_match(attr_key: str, attr_val) -> str:
        """Returns the clean "Label: Value" string if this product's formatted
        attributes contain attr_val under attr_key, else None."""
        if not attr_val:
            return None
        clean_name = _resolve_attribute_label(attr_key)
        clean_val = _resolve_attribute_term_name(attr_key, attr_val)
        match = next(
            (a for a in formatted_attrs
             if isinstance(a, dict) and a.get("name", "").lower() == clean_name.lower()),
            None,
        )
        if match and any(clean_val.lower() == str(o).lower() for o in match.get("options", [])):
            return f"{clean_name}: {clean_val}"
        return None

    # ── Plain AND-required filters ──
    target_cat_slugs = {s.lower() for s in (getattr(entities, "target_category_slugs", None) or set())}
    for slug in sorted(p_cat_slugs & target_cat_slugs):
        labels.append(_resolve_category_breadcrumb(slug))

    target_tag_slugs = {s.lower() for s in (getattr(entities, "tag_slugs", None) or [])}
    for slug in sorted(p_tag_slugs & target_tag_slugs):
        labels.append(_resolve_tag_name(slug))

    for attr_key, attr_val in (getattr(entities, "attributes", None) or {}).items():
        label = _attr_match(attr_key, attr_val)
        if label:
            labels.append(label)

    # ── OR-pairs — category branch OR tag branch OR attribute branch ──
    for op in (getattr(entities, "attr_tag_or_pairs", None) or []):
        for slug in (op.get("cat_slugs") or []):
            if slug.lower() in p_cat_slugs:
                labels.append(_resolve_category_breadcrumb(slug))

        op_tag = op.get("tag_slug")
        if op_tag and op_tag.lower() in p_tag_slugs:
            labels.append(_resolve_tag_name(op_tag))

        taxonomy = (op.get("attr_taxonomy") or "").removeprefix("pa_")
        attr_term = op.get("attr_term")
        if taxonomy and attr_term:
            label = _attr_match(taxonomy, attr_term)
            if label:
                labels.append(label)
                
    logger.warning(f"MATCHED_AGAINST_DEBUG | product_id={raw_product.get('id')} | labels={labels}")
    return list(dict.fromkeys(labels))  # de-dupe, preserve order


# ══════════════════════════════════════════════════════════════
# ─── HELPER: Build final response ───
# ══════════════════════════════════════════════════════════════

def _build_final_response(
    intent, entities, confidence, all_products_raw, order_data,
    api_responses, api_calls_to_execute, conversation, page, start_time,
    payload_context=None,
    customer_id=None,
    refinement_summary=None,
):
    """Format products and build the final JSON response."""
    products        = []
    categories      = []
    suggestions_list = []
    _sr_ctx     = payload_context or {}
    _sr_role    = _sr_ctx.get("role", "")

    if intent in (Intent.CATEGORY_LIST, Intent.PRODUCT_CATALOG):
        seen_names = set()
        for cat in all_products_raw:
            name = cat.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                categories.append({
                    "id":    cat.get("id"),
                    "name":  name.replace("&amp;", "&"),
                    "slug":  cat.get("slug", ""),
                    "count": cat.get("count", 0),
                })
    else:
        for p in all_products_raw:
            if p.get("parent_id"):
                continue
            if isinstance(p.get("attributes"), dict):
                fp = format_custom_product(p)
            else:
                fp = format_product(p)
            fp["matched_against"] = _compute_matched_against(p, fp, entities)
            products.append(fp)

    products   = [p for p in products if p.get("name")]
    pagination = build_pagination(page, api_responses, api_calls_to_execute)

    or_pair_breakdown = None
    for resp in api_responses:
        if resp.get("success") and resp.get("or_group_breakdown"):
            or_pair_breakdown = resp["or_group_breakdown"]
            break

    if intent in (Intent.CATEGORY_LIST, Intent.PRODUCT_CATALOG):
        bot_message      = "Here are our top categories to help you get started!"
        suggestions_list = ["Cancel"]
    else:
        bot_message      = generate_bot_message(
            intent, entities, products, confidence, order_data,
            total_items=pagination.get("total_items"), page=page,
            customer_id=customer_id,
            or_pair_breakdown=or_pair_breakdown,
        )
        suggestions_list = generate_suggestions(intent, entities, products)

    # ── Refinement prefix + New Search affordance ──
    # On a refined search, show the accumulated filter set so the shopper always
    # knows what they're looking at. Whenever product results are shown, offer
    # "New Search" — the only (guaranteed) way to reset the accumulated filters.
    _PRODUCT_RESULT_INTENTS = {
        Intent.PRODUCT_SEARCH, Intent.FILTER_BY_ATTRIBUTE,
        Intent.CATEGORY_BROWSE, Intent.PRODUCT_LIST, Intent.PRODUCT_BY_TAG,
        Intent.MOST_POPULAR,
    }
    if refinement_summary:
        bot_message = f"*Showing {refinement_summary}*\n\n{bot_message}"
    if intent in _PRODUCT_RESULT_INTENTS and products and "New Search" not in suggestions_list:
        suggestions_list = list(suggestions_list) + ["New Search"]
    elif intent == Intent.PRODUCT_QUICK_SHIP and "New Search" not in suggestions_list:
        suggestions_list = list(suggestions_list) + ["New Search"]

    # ── Determine flow state ──────────────────────────────────────────────────
    _BROWSING_INTENTS = {
        Intent.PRODUCT_SEARCH,
        Intent.PRODUCT_DETAIL,
        Intent.PRODUCT_LIST,
        Intent.PRODUCT_BY_TAG,
        Intent.PRODUCT_BY_COLLECTION,
        Intent.FILTER_BY_ATTRIBUTE,
        Intent.CATEGORY_BROWSE,
        Intent.MOST_POPULAR,
    }

    _single_product_found = (
        len(products) == 1
        and bool(entities.product_id)

    )

    if intent in ORDER_CREATE_INTENTS and order_data:
        next_flow_state = FlowState.AWAITING_ANYTHING_ELSE.value

    elif intent in _BROWSING_INTENTS and _single_product_found:
        next_flow_state = FlowState.AWAITING_CART_CONFIRMATION.value
        product_name = products[0].get("name", "this product")
        bot_message = (
            f"{bot_message}\n\nWould you like to add **{product_name}** to your cart?"
        )
        suggestions_list = ["Yes, add it", "No thanks"]

    else:
        next_flow_state = FlowState.IDLE.value

    # ── Build response ────────────────────────────────────────────────────────
    elapsed  = time.time() - start_time
    response = {
        "success":     True,
        "bot_message": bot_message,
        "intent":      intent.value,
        "products":    products,
        "categories":  categories,
        "suggestions": suggestions_list,
        "session_id":  str(conversation.id),
        "metadata": {
            "confidence":       round(confidence, 2),
            "products_count":   len(products),
            "categories_count": len(categories),
            "provider":         CLASSIFIER_PROVIDER_TAG,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "response_time_ms": round(elapsed * 1000),
            "intent_raw":       intent.value,
            "entities":         _entities_to_dict(entities),
            "resolved_query":   api_calls_to_execute[-1].body if api_calls_to_execute else None,
        },
        "pagination":  pagination,
        "flow_state":  next_flow_state,
    }

    if intent in (Intent.ORDER_HISTORY, Intent.LAST_ORDER) and order_data:
        response["orders"]           = [format_order_for_frontend(o) for o in order_data]
        response["order_pagination"] = build_pagination(page, api_responses, api_calls_to_execute)
        # Mirrors the admin gate in handle_order_status (order_handler.py) —
        # without this the frontend never offers the CSV download control.
        response["metadata"]["allow_order_download"] = is_order_report_admin(_sr_role)

    _sr_actions = response.get("actions", [])

    # Rep-only affordances are WooCommerce-only: the bulk-order and CS-rep
    # flows they open depend on custom-plugin endpoints with no Shopify
    # implementation. Suppress them entirely rather than surfacing buttons
    # that dead-end (SHOW_PRODUCT_RECENT_ORDERS also triggers a Woo call).
    _sr_rep_features = ECOMMERCE_BACKEND != "shopify"

    from app_config import CUSTOM_ORDER_ROLES, ORDER_REPORT_ADMIN_ROLES
    _can_view_orders = CUSTOM_ORDER_ROLES | ORDER_REPORT_ADMIN_ROLES

    # Bulk-order button: rep roles only (they place orders).
    if _sr_rep_features and _sr_role in BULK_ORDER_ROLES and customer_id and products:
        _sr_actions.append({"type": "SHOW_RECENTLY_ORDERED_BUTTON", "payload": {}})

    # Product order history: any rep or admin can VIEW it.
    # Gated separately so an administrator logged in for reporting doesn't miss
    # it just because they're not in the bulk-order role set.
    if _sr_rep_features and _sr_role in _can_view_orders and customer_id and products:
        _searched_product_id = getattr(entities, "product_id", None)

        # Show only when the user explicitly asks — not on every product search.
        if _searched_product_id and intent in (Intent.ORDER_HISTORY, Intent.LAST_ORDER, Intent.HISTORICAL_SEARCH):
            _recent_orders = _fetch_product_order_history(_searched_product_id, _sr_role)
            if _recent_orders:
                _sr_actions.append({
                    "type": "SHOW_PRODUCT_RECENT_ORDERS",
                    "payload": {
                        "orders": _format_product_orders_for_action(_recent_orders),
                    },
                })

    if _sr_rep_features and customer_id:
        _sr_actions.append({"type": "SHOW_BULK_ORDER_BUTTON", "payload": {}})

    if _sr_actions:
        response["actions"] = _sr_actions

    return _finalize_turn(conversation, jsonify(response))


# ══════════════════════════════════════════════════════════════
# ─── HELPER: Handle customer intent responses ───
# ══════════════════════════════════════════════════════════════

def _handle_customer_intents(
    intent, entities, confidence, order_data, api_calls_to_execute, api_responses,
    conversation, page, start_time,
):
    """Handle FETCH_CUSTOMER and UPDATE_CUSTOMER intents. Returns response or None."""
    if intent == Intent.FETCH_CUSTOMER:
        elapsed      = int((time.time() - start_time) * 1000)
        customer_raw = order_data[0] if order_data else {}

        display = {}
        for field_key in entities.customer_fields_requested:
            if "." in field_key:
                section, key = field_key.split(".", 1)
                display[field_key] = customer_raw.get(section, {}).get(key)
            elif field_key == "full_name":
                display["name"] = f"{customer_raw.get('first_name', '')} {customer_raw.get('last_name', '')}".strip()
            else:
                display[field_key] = customer_raw.get(field_key)

        lines = [f"**{k}**: {v or 'not set'}" for k, v in display.items()]

        return _finalize_turn(conversation, jsonify({
            "success":     True,
            "bot_message": "Here's what I have on file:\n" + "\n".join(lines),
            "intent":      "fetch_customer",
            "products":    [],
            "suggestions": [],
            "session_id":  str(conversation.id),
            "metadata":    {"confidence": round(confidence, 2), "response_time_ms": elapsed},
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
        }))

    if intent == Intent.UPDATE_CUSTOMER:
        elapsed        = int((time.time() - start_time) * 1000)
        update_success = False
        for _api_call, _api_resp in zip(api_calls_to_execute, api_responses):
            if _api_call.method == "PUT" and "/customers/" in _api_call.endpoint:
                update_success = _api_resp.get("success", False)
                break
        _update_signal = [{"success": update_success}]

        return _finalize_turn(conversation, jsonify({
            "success":     update_success,
            "bot_message": generate_bot_message(intent, entities, [], confidence, _update_signal),
            "intent":      intent.value,
            "products":    [],
            "suggestions": generate_suggestions(intent, entities, []),
            "session_id":  str(conversation.id),
            "metadata": {
                "confidence":       round(confidence, 2),
                "products_count":   0,
                "provider":         CLASSIFIER_PROVIDER_TAG,
                "timestamp":        datetime.now(timezone.utc).isoformat(),
                "response_time_ms": elapsed,
                "intent_raw":       intent.value,
                "entities":         _entities_to_dict(entities),
            },
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
        }))

    return None


# ══════════════════════════════════════════════════════════════
# ─── MAIN CHAT PIPELINE ───
# ══════════════════════════════════════════════════════════════

@chat_bp.route("/chat/order-confirmed", methods=["POST"])
def handle_order_confirmed():
    data           = request.get_json() or {}
    session_id_str = data.get("session_id")
    order_id       = data.get("order_id")
    msg_text       = f"✅ Order #{order_id} placed."

    if session_id_str:
        try:
            conv = Conversation.query.get(uuid.UUID(session_id_str))
            if not conv:
                logger.warning(f"[order_confirmed] No Conversation found for session_id={session_id_str}")
            if conv:
                db.session.add(Message(
                    conversation_id=conv.id,
                    role="bot",
                    content=msg_text,
                    intent="order_placed",
                    metadata_json={},
                ))
                db.session.commit()
        except Exception as exc:
            logger.warning(f"[order_confirmed] DB write failed: {exc}")
            db.session.rollback()
    else:
        logger.warning("[order_confirmed] Called with no session_id")

    return jsonify({"success": True, "bot_message": msg_text}), 200

@chat_bp.route("/chat/cart-result", methods=["POST"])
def handle_cart_result():
    data           = request.get_json() or {}
    session_id_str = data.get("session_id")
    success        = bool(data.get("success", False))
    product_name   = data.get("product_name") or "item"
    quantity       = int(data.get("quantity") or 1)

    if success:
        msg_text    = f"✅ Added **{product_name}** ×{quantity} to your cart."
        out_actions = [build_open_cart_panel()]
        suggestions = ["Proceed to checkout", "Continue shopping", "View cart"]
        intent      = Intent.ADD_TO_CART.value
    else:
        msg_text    = f"⚠️ Couldn't add **{product_name}** to your cart. Please try again."
        out_actions = []
        suggestions = ["View cart", "Browse products"]
        intent      = "error"

    if session_id_str:
        try:
            conv = Conversation.query.get(uuid.UUID(session_id_str))
            if conv:
                db.session.add(Message(
                    conversation_id=conv.id,
                    role="bot",
                    content=msg_text,
                    intent=intent,
                    metadata_json={"actions": out_actions, "suggestions": suggestions},
                ))
                db.session.commit()
        except Exception as exc:
            logger.warning(f"[cart_result] DB write failed: {exc}")
            db.session.rollback()

    return jsonify({
        "success":     True,
        "bot_message": msg_text,
        "actions":     out_actions,
        "suggestions": suggestions,
    }), 200
    
@chat_bp.route("/chat", methods=["POST"])
@enforce_daily_limit
def chat():
    start_time = time.time()

    # ── Parse request ──
    body = request.get_json(silent=True)
    if not body:
        logger.warning("POST /chat | Invalid JSON body")
        return jsonify({
            "success":     False,
            "bot_message": "Invalid request. Send JSON with 'message' field.",
            "intent": "error", "products": [],
            "suggestions": ["Browse Products", "What categories do you have?"],
            "session_id": "", "metadata": {"error": "Invalid JSON body"},
            "pagination": default_pagination(),
        }), 400

    message = body.get("message", "").strip()
    page    = int(body.get("page", 1))

    # ── Platform validation ───────────────────────────────────────────────
    # The widget reports which platform its bundle was built for. A mismatch
    # means a widget is pointed at the wrong backend — e.g. a Shopify
    # storefront talking to a WooCommerce deployment. Every answer from here
    # on would be about the wrong catalogue, and cart/order actions would
    # reference ids that don't exist on the other side, so reject the request
    # outright rather than serving plausible-looking nonsense.
    #
    # This is validation, NOT selection: the backend is chosen per deployment
    # by ECOMMERCE_BACKEND (imported at module load in several modules), so a
    # per-request switch would be a lie. Absent field = older widget = allowed.
    _claimed_platform = (body.get("platform") or "").strip().lower()
    if _claimed_platform and _claimed_platform != ECOMMERCE_BACKEND:
        logger.error(
            f"POST /chat | platform mismatch | widget={_claimed_platform!r} "
            f"backend={ECOMMERCE_BACKEND!r} — rejecting request"
        )
        return jsonify({
            "success":     False,
            "bot_message": (
                "This store's chat assistant isn't configured correctly. "
                "Please let the store owner know — no action was taken."
            ),
            "intent": "error", "products": [],
            "suggestions": [],
            "session_id": body.get("session_id") or "",
            "metadata": {
                "error": "platform_mismatch",
                "widget_platform": _claimed_platform,
                "backend_platform": ECOMMERCE_BACKEND,
            },
            "pagination": default_pagination(),
        }), 400

    # ── Language detection ──
    # Skip translation during variant selection — the user is typing back
    # catalog attribute values (colors, dimensions, finishes) that were shown
    # to them verbatim. Running translation on these corrupts the strings
    # (e.g. 12"X24" → 12 "X24") and breaks attribute matching downstream.
    _payload_flow_state = body.get("user_context", {}).get("flow_state", "idle")
    if _payload_flow_state == FlowState.AWAITING_VARIANT_SELECTION.value:
        was_translated, detected_lang = False, "en"
        logger.debug("[LangCheck] Skipping translation — AWAITING_VARIANT_SELECTION flow")
    else:
        message, was_translated, detected_lang = detect_and_translate(message)
        if was_translated:
            logger.info(f"[LangCheck] translated from '{detected_lang}' | '{message[:100]}'")

    # ── Generic word synonyms (e.g. "material" → "category") ──
    # Runs on the final English text, after translation, so every downstream
    # extractor/classifier/keyword-matcher that already understands the
    # internal word ("category") automatically handles the user's word
    # ("material") too, with zero changes needed at each individual site.
    message = _apply_generic_word_synonyms(message)

    # ── Session & DB setup ──
    session_id   = resolve_session_id()
    conversation = Conversation.query.get(session_id)
    if not conversation:
        conversation = Conversation(id=session_id)
        db.session.add(conversation)
        db.session.commit()

    payload_context = body.get("user_context", {})
    if payload_context.get("customer_id") and not conversation.customer_id:
        conversation.customer_id = str(payload_context.get("customer_id"))
        db.session.commit()

    customer_id  = conversation.customer_id
    user_context = conversation.context_data or {}

    # Persist role into user_context so handlers can read it without payload_context
    role = payload_context.get("role", "")
    if role and user_context.get("role") != role:
        user_context["role"] = role

    # Persist rep email so order creation can default project_rep to the
    # logged-in rep (custom-api saves the rep's email into _billing_project_rep).
    _incoming_email = payload_context.get("email", "")
    if _incoming_email and user_context.get("rep_email") != _incoming_email:
        user_context["rep_email"] = _incoming_email
        flag_modified(conversation, "context_data")

    if user_context is not conversation.context_data:
        conversation.context_data = user_context

    truncated_msg = message[:100] + "..." if len(message) > 100 else message
    logger.info(
        f'POST /chat | session={session_id} | message="{sanitize_log_string(truncated_msg)}" '
        f"| customer_id={customer_id} | flow_state={conversation.flow_state}"
    )

    if not message:
        return jsonify({
            "success":     False,
            "bot_message": "Please type a message! Try asking about our products, categories, or your orders.",
            "intent": "error", "products": [],
            "suggestions": ["Browse Products", "What categories do you have?"],
            "session_id": str(conversation.id), "metadata": {"error": "Empty message"},
            "pagination": default_pagination(page),
        }), 400

    # ── New Search reset ──────────────────────────────────────────────────
    # ARCHITECTURALLY DISTINCT from the suggestion-interpretation system: this
    # is an early exact-match interceptor (like __bulk_order_trigger__), not a
    # phrase the classifier happens to handle. It is merely DELIVERED via a
    # suggestion chip ("New Search") because that is zero-frontend. Do NOT
    # "consolidate" this by routing it through classify() — the whole point is a
    # GUARANTEED reset, not a probable one.
    if message.strip().lower() == "new search":
        clear_active_search(user_context)
        conversation.context_data = user_context
        conversation.flow_state = FlowState.IDLE.value
        flag_modified(conversation, "context_data")
        db.session.commit()
        return jsonify({
            "success": True,
            "bot_message": "What would you like to search for?",
            "intent": "new_search",
            "products": [],
            "suggestions": [],
            "session_id": str(conversation.id),
            "metadata": {"flow_state": FlowState.IDLE.value},
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    try:
        # ── Save user message ──
        user_msg = Message(conversation_id=conversation.id, role="user", content=message)
        db.session.add(user_msg)
        db.session.commit()
        
        # ── Single store_loader fetch for the whole request ──
        store_loader = get_store_loader()

        def _ft(resp):
            """Local alias: wraps _finalize_turn with address-proposal context and,
            if this turn corrected any misspelled terms, a note about it."""
            return _finalize_turn(
                conversation, resp,
                _proposal_message=message,
                _proposal_customer_id=customer_id,
                _proposal_flow_state=current_flow_state,
                _typo_corrections=_typo_corrections,
            )

        # ── Resolve flow state ──
        try:
            current_flow_state = FlowState(conversation.flow_state)
        except ValueError:
            # Legacy flow-state strings whose enum value was renamed (see
            # _LEGACY_FLOW_STATE_ALIASES) map onto their replacement so a
            # session already mid-flow continues normally — the pending
            # context (order_for_pending_name, etc.) is untouched by the
            # rename and still sitting in conversation.context_data, so the
            # rep's next reply is handled exactly as it would have been
            # before. Anything not in the alias table falls back to IDLE,
            # same as always: a genuinely stale or corrupt value should not
            # crash the turn.
            current_flow_state = _LEGACY_FLOW_STATE_ALIASES.get(
                conversation.flow_state, FlowState.IDLE
            )

        _wipe_stale_cart(conversation, user_context, current_flow_state)

        # ── Bare cancel in IDLE / AWAITING_ANYTHING_ELSE ──────────────────────
        # conversation_flow's escape hatch deliberately skips these two states,
        # and they are exactly the two states where typo correction runs — so
        # without this, "cancel" was corrected to the nearest catalog term
        # ("panel") and searched as a product. IDLE is not a no-op state either:
        # active_search filter accumulation survives here, so cancel has real
        # work to do — it resets the same way New Search does.
        #
        # Placed AFTER flow-state resolution (needs current_flow_state) and
        # BEFORE typo correction (must see the raw word). Clarification states
        # are excluded: their own handlers own the cancel chip.
        if (
            current_flow_state in (FlowState.IDLE, FlowState.AWAITING_ANYTHING_ELSE)
            and is_bare_exit(message)
        ):
            clear_active_search(user_context)
            conversation.context_data  = user_context
            conversation.flow_state    = FlowState.IDLE.value
            user_context["flow_state"] = FlowState.IDLE.value
            flag_modified(conversation, "context_data")
            db.session.commit()
            logger.info(
                f"Bare exit word in {current_flow_state.value} — reset to idle | "
                f"session={conversation.id} | message={message!r}"
            )
            return jsonify({
                "success": True,
                "bot_message": "No problem — I've cleared that. What would you like to look for?",
                "intent": "new_search",
                "products": [],
                "suggestions": ["Browse Products", "View my orders"],
                "session_id": str(conversation.id),
                "metadata": {"flow_state": FlowState.IDLE.value},
                "flow_state": FlowState.IDLE.value,
                "pagination": default_pagination(page),
            }), 200

        # Bound before the typo-correction block below so _ft can always read
        # it, even when that block's guard condition is False this turn (e.g.
        # a "__"-prefixed control message) or hasn't run yet.
        _typo_corrections: list = []

        # ── Typo-ambiguity clarification resolution ───────────────────────────
        # Answers a chip we asked last turn ("did you mean X or Y?"). Resolves
        # to plain message text (not entities — see typo_clarification_handler
        # docstring) and falls through into normal typo correction below, on
        # the *resolved* message, so the rest of the sentence still gets fixed.
        if (
            current_flow_state == FlowState.AWAITING_FILTER_CLARIFICATION
            and user_context.get("pending_typo_clarification")
        ):
            from handlers.typo_clarification_handler import (
                resolve_typo_clarification,
                TYPO_CLARIFICATION_CANCELLED,
            )
            _resolved_message = resolve_typo_clarification(
                message, user_context, user_context["pending_typo_clarification"]
            )

            # Cancel: exit the chip WITHOUT reprocessing the text. Feeding
            # the still-ambiguous token back into correct_message() below is
            # what re-raised the identical prompt on every turn.
            if _resolved_message is TYPO_CLARIFICATION_CANCELLED:
                conversation.context_data  = user_context
                conversation.flow_state    = FlowState.IDLE.value
                user_context["flow_state"] = FlowState.IDLE.value
                flag_modified(conversation, "context_data")
                db.session.commit()
                return jsonify({
                    "success": True,
                    "bot_message": "No problem — what would you like to search for?",
                    "intent": "new_search",
                    "products": [],
                    "suggestions": ["Browse Products", "View my orders"],
                    "session_id": str(conversation.id),
                    "metadata": {"flow_state": FlowState.IDLE.value},
                    "flow_state": FlowState.IDLE.value,
                    "pagination": default_pagination(page),
                }), 200

            if _resolved_message is not None:
                message                  = _resolved_message
                current_flow_state       = FlowState.IDLE
                conversation.flow_state  = FlowState.IDLE.value
                user_context["flow_state"] = FlowState.IDLE.value

        # ── Typo correction (pre-classification) ─────────────────────────────
        if (
            current_flow_state in (FlowState.IDLE, FlowState.AWAITING_ANYTHING_ELSE)
            and not message.startswith("__")
            and not re.match(r"(?i)^no\s*-\s*search\s*for\s*['\"]", message)
        ):
            from utils.typo_correction import correct_message, find_mos_confusions
            _suppressed = list(user_context.get("typo_suppressed_tokens", []))
            _suppressed.extend(
                _recipient_scope_tokens(
                    message, is_bulk=_is_inline_bulk_order(message, store_loader)
                )
            )
            _corrected, _typo_corrections, _typo_ambiguities = correct_message(
                message, store_loader,
                suppressed_tokens=_suppressed,
            )
            # Keep the ORIGINAL wording. Rep-name resolution matches against
            # this rather than the corrected text, because correction rewrites
            # tokens toward catalog vocabulary — "Bullock" became "Block" (a
            # mosaic-type attribute), so the rep lookup searched for someone
            # who does not exist. Matching the raw message sidesteps that
            # entirely instead of trying to protect names positionally.
            user_context["raw_message_for_names"] = message

            if _typo_corrections:
                message = _corrected
                user_msg.metadata_json = {
                    "typo_corrections": _typo_corrections,
                    "corrected_message": _corrected,
                }
                db.session.commit()

            # ── "mos" confirmation guard ──
            # Runs on _corrected (the exact text entity extraction is about to
            # see) rather than the raw message, so it catches BOTH the
            # uncorrected case ("mosiah" survived the edit-distance budget)
            # and the mis-corrected case (the corrector confidently rewrote it
            # to some other "mos" term). Prepended so it always wins the
            # one-chip-per-turn slot over a regular edit-distance tie.
            _mos_confusions = find_mos_confusions(
                _corrected,
                suppressed_tokens=user_context.get("typo_suppressed_tokens", []),
            )
            if _mos_confusions:
                _typo_ambiguities = _mos_confusions + _typo_ambiguities

            if _typo_ambiguities:
                # _corrected already has any unambiguous fixes applied and the
                # tied token left as-is — that's exactly the base text to chip
                # on and splice into. v1: surface only the first tie per turn
                # (see typo_clarification_handler docstring).
                from handlers.typo_clarification_handler import build_typo_clarification
                _typo_clarification_resp = build_typo_clarification(
                    _typo_ambiguities[0], _corrected, user_context, str(conversation.id), page, start_time,
                )
                conversation.context_data = user_context
                conversation.flow_state   = FlowState.AWAITING_FILTER_CLARIFICATION.value
                flag_modified(conversation, "context_data")
                db.session.commit()
                return _ft(_typo_clarification_resp)

        # ── Product reorder intercept ──   ← MOVE HERE (after current_flow_state is set)
        if message.startswith("__PRODUCT_REORDER__"):
            try:
                _reorder_payload = json.loads(message[len("__PRODUCT_REORDER__"):])
            except (ValueError, TypeError):
                _reorder_payload = {}
            if _reorder_payload:
                resp = handle_product_reorder(
                    _reorder_payload, store_loader, conversation, user_context, page, start_time
                )
                if resp:
                    return _finalize_turn(conversation, resp)  # no _proposal_message

        # ── Bulk cancel intercept ──
        # Fired by the "Cancel bulk process" button on BulkAddressConfirmationCard.
        # Short-circuits before the flow state machine so all pending bulk state
        # is cleared regardless of which sub-step the address flow is in.
        if message.strip() == "__BULK_CANCEL__":
            resp = handle_cancel_bulk_order(user_context, conversation, page, start_time)
            return _finalize_turn(conversation, resp)  # no address proposal on a cancel

        # ── Step 0.5: Suggestion retry (early exit) ──
        sr_resp = handle_suggestion_retry(body, message, str(conversation.id), customer_id, page, start_time)
        if sr_resp:
            return _ft(sr_resp)

        # ── Step 1: Filter clarification bypass ──
        _skip_classification = False
        bypass_result        = None
        _rfn_resolved        = False

        forced_search_match = re.match(r"(?i)^no\s*-\s*search\s*for\s*['\"](.*?)['\"]$", message)

        if current_flow_state == FlowState.AWAITING_FILTER_CLARIFICATION:
            pending_semantic = user_context.get("pending_semantic_match")
            if pending_semantic:
                clarification_result = resolve_filter_clarification(message, user_context, pending_semantic)
                if clarification_result:
                    current_flow_state              = FlowState.IDLE
                    user_context["flow_state"]      = FlowState.IDLE.value
                    conversation.context_data       = user_context
                    bypass_result                   = clarification_result
                    _skip_classification            = True

        elif current_flow_state == FlowState.AWAITING_REFINEMENT_CHOICE:
            _pending_rfn = user_context.get("pending_refinement")
            if _pending_rfn:
                _rfn_entities = resolve_refinement_choice(message, _pending_rfn)
                # Always reset state — recognized chip or unexpected input, we leave
                # AWAITING_REFINEMENT_CHOICE either way so the next turn is clean.
                current_flow_state                = FlowState.IDLE
                conversation.flow_state           = FlowState.IDLE.value
                user_context["flow_state"]        = FlowState.IDLE.value
                user_context.pop("pending_refinement", None)
                conversation.context_data         = user_context
                flag_modified(conversation, "context_data")
                if _rfn_entities is not None:
                    # Recognized chip — use resolved entities, bypass classification.
                    bypass_result        = ClassifiedResult(
                        intent=Intent.PRODUCT_SEARCH,
                        entities=_rfn_entities,
                        confidence=0.99,
                    )
                    _skip_classification = True
                    _rfn_resolved        = True
                # else: unrecognized input — fall through to normal classification.

        elif current_flow_state == FlowState.AWAITING_NO_RESULTS_CHOICE:
            _pending_nr = user_context.get("pending_no_results_choice")
            if _pending_nr:
                _nr_entities = resolve_no_results_choice(message, _pending_nr)
                # Always reset state — recognized chip or unexpected input, we leave
                # AWAITING_NO_RESULTS_CHOICE either way so the next turn is clean.
                current_flow_state                = FlowState.IDLE
                conversation.flow_state           = FlowState.IDLE.value
                user_context["flow_state"]        = FlowState.IDLE.value
                user_context.pop("pending_no_results_choice", None)
                conversation.context_data         = user_context
                flag_modified(conversation, "context_data")
                if _nr_entities is not None:
                    # Recognized chip — use resolved entities, bypass classification.
                    bypass_result        = ClassifiedResult(
                        intent=Intent.PRODUCT_SEARCH,
                        entities=_nr_entities,
                        confidence=0.99,
                    )
                    _skip_classification = True
                    _rfn_resolved        = True
                # else: unrecognized input — fall through to normal classification.

        elif forced_search_match:
            extracted_term = forced_search_match.group(1)
            logger.info(f"Intercepted explicit forced search string. Term: '{extracted_term}'")
            bypass_entities = ExtractedEntities(search_term=extracted_term)
            bypass_result   = ClassifiedResult(
                intent=Intent.PRODUCT_SEARCH,
                entities=bypass_entities,
                confidence=1.0,
            )
            _skip_classification = True

        # ── Step 2: Conversation flow state machine ──
        flow_context = {
            "pending_product_name":     user_context.get("pending_product_name"),
            "pending_product_id":       user_context.get("pending_product_id"),
            "pending_quantity":         user_context.get("pending_quantity"),
            "pending_variation_id":     user_context.get("pending_variation_id"),
            "resolved_attributes":      user_context.get("resolved_attributes"),
            "bulk_awaiting_address_text": user_context.get("bulk_awaiting_address_text", False),
        }

        flow_result = None
        if current_flow_state not in (FlowState.IDLE, FlowState.AWAITING_ANYTHING_ELSE):
            flow_result = handle_flow_state(
                state=current_flow_state, message=message,
                entities=flow_context, confidence=0.0,
            )
            # Guard: if an order flow returned None (fell through), don't let the
            # classifier run — the user sent something off-topic mid-flow.
            if flow_result is None and is_order_flow(current_flow_state):
                _ctx = _flow_context_message(current_flow_state)
                return _ft((jsonify({
                    "success": True,
                    "bot_message": _ctx["bot_message"],
                    "suggestions": _ctx["suggestions"],
                    "flow_state": _ctx["flow_state"],
                    "session_id": str(conversation.id),
                    "products": [],
                    "intent": "unknown",
                    "metadata": {"confidence": 0.0},
                    "pagination": default_pagination(page),
                }), 200))
            if flow_result and flow_result.get("override_message"):
                message = flow_result["override_message"]

        if flow_result:
            _persistent_keys = [
                "pending_product_id", "pending_product_name", "pending_quantity",
                "pending_variation_id", "resolved_attributes",
            ]
            for k in _persistent_keys:
                if k in flow_result and flow_result[k] is not None:
                    user_context[k] = flow_result[k]
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

        # ── Cart confirmation flow (prompt / confirm / decline) ──
        _flow_action = flow_result.get("action") if flow_result else None
        if _flow_action in ("prompt_cart_confirmation", "confirm_add_to_cart", "decline_add_to_cart"):
            resp = _handle_cart_flow(_flow_action, user_context, conversation, store_loader, page, start_time)
            if resp is not None:
                return _ft(resp)

        # ── Flow router (pass_through=False, no action) ──
        # pass_through=True means "also let classifier run after"
        _needs_flow_handler = (
            flow_result
            and "action" not in flow_result
            and not flow_result.get("pass_through")
        )
        if _needs_flow_handler:
            resp = handle_flow(
                flow_result, user_context, str(conversation.id),
                customer_id, page, start_time,
            )
            if resp:
                return _ft(resp)

        _resume_order_list = None

        # ── Early action dispatch ──────────────────────────────────────────
        # Bulk/rep flow actions short-circuit here before classification so
        # replies like "Yes, confirm" or "Change address" are never misrouted
        # to the product-search or update_customer classifier paths.
        if _flow_action:
            # A resume dict (not a response) means the flow resolved a date
            # window for the all-orders list; fall through to classification
            # with the window applied instead of returning here.
            resp = _dispatch_bulk_action(
                _flow_action, message, role, store_loader,
                conversation, user_context, page, start_time,
                customer_id=customer_id,
            )
            if isinstance(resp, dict) and resp.get("resume") == "order_list":
                # Not a response — the admin picked a window for the
                # all-orders list. Rewrite the turn as an order-history
                # request carrying that window and let the normal pipeline
                # answer it, so there is one code path building order lists
                # rather than two that can drift apart.
                #
                # This rewrite destroys whatever scope wording ("my"/"all")
                # the ORIGINAL turn had — re-classifying the literal string
                # "view all orders" always derives scope="all". The real
                # scope traveled through resp["scope"] (parked in
                # pending_order_stats by prompt_for_order_list_range) and is
                # reapplied to entities AFTER classification below, the same
                # way date_after/date_before already are.
                _resume_order_list = resp
                message = "view all orders"
                _skip_classification = False
            elif resp is not None:
                return _ft(resp)

        # ── Step 3: Classify ──
        _resolve_variant = bool(flow_result and flow_result.get("resolve_variant"))

        if _skip_classification:
            result = bypass_result
        else:
            result = parse_csv_message(message, store_loader)

        # Merge phase-1 / phase-2 entity richness
        intent, entities, confidence = _merge_phase_entities(result)

        # Lock in variant state
        if current_flow_state == FlowState.AWAITING_VARIANT_SELECTION:
            _resolve_variant      = True
            intent                = Intent.QUICK_ORDER
            result.intent         = intent
            entities.product_id   = user_context.get("pending_product_id")
            entities.product_name = user_context.get("pending_product_name")
            confidence            = 1.0

        # ── Step 4: LLM fallback ──
        session_history = [{"role": m.role, "message": m.content} for m in conversation.messages[-4:-1]]

        if not _resolve_variant:
            logger.debug(
                f"[STEP1.5_GATE_TRACE] intent={intent} | confidence={confidence} | "
                f"product_name={entities.product_name!r} | "
                f"target_category_slugs={getattr(entities, 'target_category_slugs', None)} | "
                f"attr_tag_or_pairs={entities.attr_tag_or_pairs}"
            )
            llm_outcome = run_llm_fallback(
                message=message, intent=intent, entities=entities, confidence=confidence,
                session_id=str(conversation.id), session_history=session_history,
                store_loader=store_loader, page=page, start_time=start_time,
                order_create_intents=ORDER_CREATE_INTENTS, user_context=user_context,
            )

            if llm_outcome is not None:
                if not isinstance(llm_outcome, tuple) or not isinstance(llm_outcome[0], tuple):
                    if hasattr(llm_outcome, "get_data"):
                        return _ft(llm_outcome)
                    if isinstance(llm_outcome, tuple) and len(llm_outcome) == 2 and isinstance(llm_outcome[1], int):
                        return _ft(llm_outcome)
                if isinstance(llm_outcome, tuple) and len(llm_outcome) == 4:
                    intent, entities, confidence, result = llm_outcome

        # ── Step 4.5: Same-value attribute collision check ──────────────────
        # Any two (or more) attributes can independently end up holding the EXACT
        # SAME value purely because extraction matches a value against every
        # taxonomy whose term list happens to contain it — regardless of whether
        # the user actually named more than one of them (e.g. "sample size 12x12"
        # also gets matched as Tile Size; "green" matches both Color and Colors 2;
        # "mosaic" matches Visual, Product Type, Sample Size, and Mosaic Type all
        # at once). That's one filter, not several — and if the user explicitly
        # named exactly one of the colliding attributes, that one wins outright
        # rather than triggering a clarification prompt.
        _value_groups: dict = {}
        for k, v in entities.attributes.items():
            _value_groups.setdefault(v, []).append(k)

        _msg_tokens = normalize_for_tag_compare(message.lower())

        for _shared_value, _keys_present in list(_value_groups.items()):
            if len(_keys_present) < 2:
                continue

            _keys_norm = {k.lower().strip() for k in _keys_present}
            if not any(_keys_norm <= set(group) for group in ATTRIBUTE_DISAMBIGUATION_GROUPS):
                continue  # not a known genuine ambiguity — let the OR-merge handle it, no clarification

            _msg_tokens = normalize_for_tag_compare(message.lower())
            # If one candidate's label is a substring of another's (e.g. "color"
            # inside "colors 2"), explicit-mention detection is unreliable — the
            # shorter name appearing in the message doesn't rule out the longer
            # one, since the longer one's own name contains it too. Treat this
            # shape of collision as a genuine tie rather than guessing.
            _has_naming_overlap = any(
                a != b and (
                    a.replace("-", " ").strip() in b.replace("-", " ").strip()
                    or b.replace("-", " ").strip() in a.replace("-", " ").strip()
                )
                for a in _keys_present for b in _keys_present
            )

            _explicitly_named = []
            if not _has_naming_overlap:
                _explicitly_named = [
                    k for k in _keys_present
                    if normalize_for_tag_compare(k.replace("-", " ")) <= _msg_tokens
                ]

            if len(_explicitly_named) == 1:
                _winner = _explicitly_named[0]
                for k in _keys_present:
                    if k != _winner:
                        del entities.attributes[k]
            else:
                _candidates = []
                for k in _keys_present:
                    if attr_slug_for_label(k):  # validity check only
                        _candidates.append({
                            "type": "attribute",
                            "taxonomy": k,
                            "slug": _shared_value,
                            "suggested_name": _resolve_attribute_label(k),
                            "user_text": _shared_value,
                            "is_negative": False,
                            "score": 1.0,
                        })
                if len(_candidates) > 1:
                    for k in _keys_present:
                        del entities.attributes[k]
                    entities.semantic_matches.append(_candidates)

        # ── Step 5: Semantic match resolution (auto-apply or clarify) ──
        # Skip entirely when an email lookup is in play: an email address can
        # contain a catalog term as a substring (e.g. "a.annick@interior...")
        # which would falsely match a category and hijack the order-status flow.
        if (
            entities.semantic_matches
            and not getattr(entities, "lookup_email", None)
            and current_flow_state != FlowState.AWAITING_FILTER_CLARIFICATION
            and intent not in (
                Intent.PRODUCT_ATTRIBUTE_INFO, Intent.PRODUCT_VARIATIONS,
                Intent.QUICK_ORDER, Intent.PLACE_ORDER, Intent.ORDER_ITEM,
                Intent.BULK_ORDER,
                Intent.ORDER_STATUS, Intent.ORDER_TRACKING,
            )
        ):
            # Prune semantic matches already covered by the two-phase entity
            # merge. e.g. product_quick_ship sets quick-ship:yes in attributes;
            # the tag semantic match for 'quick-ship' is then redundant.
            _resolved_attr_keys = set(entities.attributes.keys())
            _resolved_tag_slugs = set(entities.tag_slugs)

            def _sem_already_covered(candidate: dict) -> bool:
                ctype = candidate.get("type")
                slug  = candidate.get("slug", "")
                tax   = candidate.get("taxonomy", "")
                if ctype == "tag":
                    # Covered if the tag or an attribute with the same key is
                    # already set (overlap will be OR-paired by consolidation)
                    return slug in _resolved_tag_slugs or slug in _resolved_attr_keys
                if ctype == "attribute":
                    return tax in _resolved_attr_keys
                return False

            entities.semantic_matches = [
                group for group in entities.semantic_matches
                if not all(
                    _sem_already_covered(c)
                    for c in (group if isinstance(group, list) else [group])
                )
            ]

            _rejected = user_context.get("rejected_semantic_terms", [])
            _sem_groups = [
                [c] if isinstance(c, dict) else list(c)
                for c in entities.semantic_matches
            ]
            _sem_groups = [
                [c for c in g if c["suggested_name"] not in _rejected]
                for g in _sem_groups
            ]
            _sem_groups = [g for g in _sem_groups if g]

            _auto_applied = False
            if (
                len(_sem_groups) == 1
                and len(_sem_groups[0]) == 1
                and _sem_groups[0][0].get("score", 0) >= SEMANTIC_AUTO_APPLY_THRESHOLD
            ):
                _pre_clear_count = len(entities.semantic_matches)
                _m = _sem_groups[0][0]
                apply_semantic_match(entities, _m)
                entities.semantic_matches = []
                entities.search_term = None
                _auto_applied = True
                logger.info(
                    f"[SemanticAutoApply] score={_m.get('score', 0):.4f} >= {SEMANTIC_AUTO_APPLY_THRESHOLD}"
                    f" | applied {_m['type']}:{_m['suggested_name']} (slug={_m.get('slug')}, taxonomy={_m.get('taxonomy')})"
                    f" | raw_semantic_matches_count={_pre_clear_count} before filtering"
                )


            if not _auto_applied:
                clarification_resp = build_semantic_clarification(
                    entities, user_context, str(conversation.id), page, start_time, flow_result,
                )
                if clarification_resp:
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")
                    return _ft(clarification_resp)

        # Shopify: only the REP multi-recipient flow is unsupported — company
        # scoping, recipient rosters, address pickers, "order for X at Y".
        # That flow is out of scope entirely on Shopify (see the module intro
        # to bulk_order_parser and app_config.BULK_ORDER_ROLES): there is no
        # rep role on this deployment, and the app's widget block
        # (shopify-app/.../miraq_widget.liquid) can only ever send
        # data-customer-role="customer" or "guest" — never a rep role — so
        # this branch is a backstop for a role that cannot currently reach
        # here, not a live path.
        #
        # Customer multi-LINE ordering (several products + variants in one
        # message, added to the shopper's own cart) is fully supported and
        # must not be blocked here — it bypasses build_api_calls the same
        # way the rep flow does (see the `_resolve_variant or intent ==
        # Intent.BULK_ORDER` branch below), which is why this check has to
        # happen before that branch rather than relying on the unsupported-
        # call guard further down.
        if (intent == Intent.BULK_ORDER and ECOMMERCE_BACKEND == "shopify"
                and role in BULK_ORDER_ROLES):
            _msg, _sugg = unsupported_message_for(Intent.BULK_ORDER)
            logger.info(
                "Shopify: BULK_ORDER rep multi-recipient flow unsupported "
                f"(role={role!r}) — returning guidance"
            )
            elapsed = round((time.time() - start_time) * 1000)
            return _ft((jsonify({
                "success":     True,
                "bot_message": _msg,
                "intent":      intent.value,
                "products":    [],
                "suggestions": _sugg,
                "session_id":  str(conversation.id),
                "metadata": {
                    "response_time_ms": elapsed,
                    "unsupported_on_shopify": True,
                },
                "flow_state":  FlowState.IDLE.value,
                "pagination":  default_pagination(page),
                "actions":     [],
            }), 200))

        # Bulk order is available to EVERY role, but it means different things.
        # For rep roles it is the full multi-recipient flow: company scope,
        # recipient resolution, address picking. For everyone else it is
        # multi-LINE ordering only — several products and quantities in one
        # message, always shipped to the person placing it.
        #
        # Nothing extra is needed to make that safe here. The parser gates
        # company scope, recipient names and email extraction on the same role
        # set (`_is_rep` in bulk_order_parser) and routes non-rep lines through
        # its self-order branch, and every on-behalf-of step in
        # bulk_order_handler is likewise gated. A non-rep therefore cannot
        # reach another person's roster or address book through this path even
        # when the message names someone — the "for <person>" tail is stripped
        # as part of product-name cleanup and never resolved.
        if intent == Intent.BULK_ORDER and role not in BULK_ORDER_ROLES:
            logger.info(
                "BULK_ORDER: non-rep role %r — proceeding as SELF-scoped "
                "multi-line order (no company/recipient/address resolution)",
                role,
            )
                
        # ── Step 6: Empty order guard ──
        empty_resp = _check_empty_order(intent, entities, conversation, page, start_time)
        if empty_resp:
            return empty_resp

        # ── Step 6.4: Order reporting fork ──
        # Answered from an aggregate endpoint, not the product/API-call path,
        # so it forks before Step 7 builds product calls.
        if intent == Intent.ORDER_STATS_BY_REP:
            _role = user_context.get("role") or user_context.get("user_role")
            stats_resp = handle_order_stats(
                entities, _role, customer_id, conversation, page, start_time,
                user_context=user_context,
            )
            if stats_resp is not None:
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                return _ft(stats_resp)

        # ── Step 6.4b: Admin all-orders list needs a window ──
        # An administrator asking for orders gets the WHOLE store, which on a
        # real catalog is thousands of rows. Same reasoning as the stats
        # report: ask which period rather than silently picking one. Reuses
        # the same picker, flow state, and reply handler.
        if intent == Intent.ORDER_HISTORY:
            _role = user_context.get("role") or user_context.get("user_role")
            if is_order_report_admin(_role):
                if _resume_order_list:
                    # Window already chosen this turn — apply it and continue.
                    # scope must be restored here too: the message rewrite
                    # above always re-derives scope="all" from the literal
                    # string "view all orders", which silently drops "my"
                    # from the ORIGINAL turn (see the comment at the rewrite
                    # site). The real scope rode through the resume dict.
                    entities.date_after  = _resume_order_list.get("date_after")
                    entities.date_before = _resume_order_list.get("date_before")
                    entities.scope       = _resume_order_list.get("scope")
                elif not getattr(entities, "date_after", None) \
                        and not getattr(entities, "date_before", None) \
                        and not getattr(entities, "date_range_resolved", False):
                    list_resp = prompt_for_order_list_range(
                        conversation, user_context, _role, start_time, page,
                        scope=getattr(entities, "scope", None),
                    )
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")
                    return _ft(list_resp)

        # ── Step 6.5: Cart intent fork ──
        if intent in CART_INTENTS or intent == Intent.CHECKOUT:
            cart_resp = handle_cart_intent(
                intent, entities, user_context, conversation, page, start_time
            )
            if cart_resp is not None:
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                return _ft(cart_resp)

        # ── Step 7: Execute API calls ──
        last_product_ctx = user_context.get("last_product")

        # ── Step 6.9: Conversational search refinement (button-based) ───────
        # Typing always CONTINUES the active search — no new-vs-refine guessing.
        # Merge accumulated filters into this turn's entities for product-search
        # intents. Reset happens only via the "New Search" interceptor above.
        _PRODUCT_SEARCH_INTENTS = (
            Intent.PRODUCT_SEARCH, Intent.FILTER_BY_ATTRIBUTE,
            Intent.CATEGORY_BROWSE, Intent.PRODUCT_LIST, Intent.PRODUCT_BY_TAG, Intent.PRODUCT_QUICK_SHIP,
            Intent.MOST_POPULAR,
        )
        _did_refine = False
        _turn_new_snapshot = None
        _active = None

        if _rfn_resolved:
            # Entities were fully merged by refinement choice resolution (Step 1).
            # Skip the normal merge — entities are already the complete filter set.
            _did_refine = True

        elif intent in _PRODUCT_SEARCH_INTENTS:
            _active = user_context.get("active_search")

            if page > 1 and not _active:
                # "Load More" with nothing to continue — don't silently run an
                # unrelated fresh search under what the user thinks is page 2
                # of results they already saw. Say so instead.
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                return _ft((jsonify({
                    "success": True,
                    "bot_message": "I don't have a search to continue loading — what would you like to look for?",
                    "intent": "guided_flow",
                    "products": [],
                    "suggestions": ["New Search"],
                    "session_id": str(conversation.id),
                    "metadata": {"flow_state": FlowState.IDLE.value},
                    "flow_state": FlowState.IDLE.value,
                    "pagination": default_pagination(page),
                }), 200))

            _active_usable = bool(_active) and (page > 1 or active_search_is_fresh(_active))
            if _active_usable:
                conflicts = detect_slot_conflicts(entities, _active)
                if conflicts:
                    # ── Multi-value slot conflict: ask add-or-replace ──────────
                    # Stash everything the resolution handler needs to rebuild
                    # fully-merged entities once the shopper taps a chip.
                    user_context["pending_refinement"] = {
                        "conflicts":           conflicts,
                        "active_search":       _active,
                        "incoming_attributes": dict(entities.attributes),
                        "incoming_tags":       list(entities.tag_slugs),
                        "incoming_categories": list(getattr(entities, "target_category_slugs", set())),
                        "incoming_min_price":  entities.min_price,
                        "incoming_max_price":  entities.max_price,
                        "incoming_or_pairs":   list(getattr(entities, "attr_tag_or_pairs", [])),
                    }
                    conversation.flow_state   = FlowState.AWAITING_REFINEMENT_CHOICE.value
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")
                    db.session.commit()
                    return _ft(build_refinement_prompt(
                        conflicts, str(conversation.id), page, start_time
                    ))
                else:
                    logger.debug(f"[merge_trace] BEFORE merge | tags={entities.tag_slugs} | attrs={dict(entities.attributes)} | or_pairs={entities.attr_tag_or_pairs}")
                    # Snapshot THIS TURN's filters before merge mutates `entities`
                    # in place — needed later if the merged search returns zero
                    # results, so we can tell the shopper what's new vs carried.
                    _turn_new_snapshot = {
                        "attributes":        dict(entities.attributes),
                        "tags":              list(entities.tag_slugs),
                        "categories":        list(getattr(entities, "target_category_slugs", set())),
                        "min_price":         entities.min_price,
                        "max_price":         entities.max_price,
                        "attr_tag_or_pairs": list(getattr(entities, "attr_tag_or_pairs", [])),
                    }
                    merge_into_active_search(entities, _active)
                    _did_refine = True
                    logger.debug(f"[merge_trace] AFTER merge | tags={entities.tag_slugs} | attrs={dict(entities.attributes)} | or_pairs={entities.attr_tag_or_pairs}")
        # ────────────────────────────────────────────────────────────────────

        # Explicit taxonomy signal — runs once, after any active-search merge,
        # so it always sees the full accumulated filter set regardless of
        # whether this turn had an active search to merge into.
        _signal = _detect_explicit_taxonomy_signal(message, store_loader)
        if _signal == 'product_cat':
            or_pairs = getattr(entities, 'attr_tag_or_pairs', [])
            # Category may already be absorbed into an OR pair by
            # _resolve_category_attribute_overlap (which runs earlier, inside
            # classify()'s own consolidate_entities call) — so target_category_slugs
            # can be empty even though the user explicitly said "category".
            # Only recover cat_slugs from OR pairs whose category name actually
            # appears in THIS message — otherwise an unrelated category carried
            # forward from an earlier turn gets silently merged in.
            msg_lower = message.lower()
            collision_cat_slugs = set()
            for op in or_pairs:
                for slug in (op.get('cat_slugs') or []):
                    cat_obj = store_loader.resolve_category(slug) if store_loader else None
                    cat_name = (cat_obj.name if cat_obj else slug.replace('-', ' ')).lower()
                    if cat_name in msg_lower or slug.replace('-', ' ') in msg_lower:
                        collision_cat_slugs.add(slug)
            _matched_cats = set(getattr(entities, 'target_category_slugs', set())) | collision_cat_slugs
            if _matched_cats:
                _cat_bases = {s.rstrip('s') for s in _matched_cats} | _matched_cats
                logger.debug(
                    f"[CAT_GROUP_TRACE] before: category_groups={entities.category_groups} | "
                    f"_matched_cats={_matched_cats}"
                )
                # User said "category" explicitly — restore it as a standalone
                # filter, dropping the OR-with-attribute version entirely.
                # Recovered OR-pair categories are their OWN group, not folded
                # into whatever's already accumulated — a category pulled out
                # of an OR-pair because THIS message named it explicitly is a
                # distinct ask, not a sibling of an earlier turn's category.
                _preserved_groups = [
                    g & _matched_cats for g in entities.category_groups if g & _matched_cats
                ]
                _already_grouped = set().union(*_preserved_groups) if _preserved_groups else set()
                _ungrouped = _matched_cats - _already_grouped

                entities.clear_categories()
                for g in _preserved_groups:
                    entities.add_category_group(g)
                if _ungrouped:
                    entities.add_category_group(_ungrouped)
                logger.debug(f"[CAT_GROUP_TRACE] after: category_groups={entities.category_groups}")

                entities.attr_tag_or_pairs = [
                    op for op in or_pairs
                    if not any(s in (op.get('cat_slugs') or []) for s in _matched_cats)
                ]
                entities.attributes = {
                    k: v for k, v in entities.attributes.items()
                    if str(v).strip().lower().rstrip('s') not in _cat_bases
                }
        elif _signal == 'product_tag' and getattr(entities, 'tag_slugs', None):
            _matched_tags = set(entities.tag_slugs)
            entities.attr_tag_or_pairs = [
                op for op in getattr(entities, 'attr_tag_or_pairs', [])
                if op.get('tag_slug') not in _matched_tags
            ]

        elif _signal and _signal.startswith('pa_'):
            or_pairs = getattr(entities, 'attr_tag_or_pairs', [])

            # Pairs that already match the named taxonomy (the user's explicit choice).
            signal_pairs = [
                op for op in or_pairs
                if op.get('attr_taxonomy') == _signal
                or f"pa_{op.get('attr_taxonomy', '')}" == _signal
            ]
            # Only drop a DIFFERENT-taxonomy pair if it describes the SAME concept
            # as a signal-matching pair (shares tag_slug or overlapping cat_slugs).
            # Pairs with no such overlap are unrelated — e.g. a filter carried
            # forward from a prior turn (different tag/category entirely) — and
            # must be left untouched, even though their taxonomy differs from _signal.
            collision_tags = {p.get('tag_slug') for p in signal_pairs if p.get('tag_slug')}
            collision_cats = {s for p in signal_pairs for s in (p.get('cat_slugs') or [])}

            kept = []
            for op in or_pairs:
                is_signal_match = (
                    op.get('attr_taxonomy') == _signal
                    or f"pa_{op.get('attr_taxonomy', '')}" == _signal
                )
                if is_signal_match:
                    # User named the attribute explicitly (e.g. "countertop
                    # application") — drop the category branch from this pair
                    # so the query filters by the attribute alone, not OR'd
                    # with the category.
                    op = dict(op)
                    op['cat_slugs'] = []
                    kept.append(op)
                    continue
                op_tag = op.get('tag_slug')
                op_cats = set(op.get('cat_slugs') or [])
                collides = (op_tag and op_tag in collision_tags) or bool(op_cats & collision_cats)
                if not collides:
                    kept.append(op)
            entities.attr_tag_or_pairs = kept

            # Resolve any category collision for the signal-matched taxonomy.
            cat_slugs_in_collision = {
                slug
                for op in entities.attr_tag_or_pairs
                if op.get('attr_taxonomy') == _signal or f"pa_{op.get('attr_taxonomy','')}" == _signal
                for slug in (op.get('cat_slugs') or [])
            }
            if cat_slugs_in_collision:
                entities.target_category_slugs -= cat_slugs_in_collision
                if not entities.target_category_slugs:
                    entities.category_name = None

        if _resolve_variant or intent == Intent.BULK_ORDER:
            # Variant resolution uses session-cached variations — no API calls needed.
            # Skipping build_api_calls entirely prevents or_pairs dict-vs-OrPair crashes
            # that occur when catalog_parser populates attr_tag_or_pairs as plain dicts.
            api_calls = []
            all_products_raw, order_data, api_responses, api_calls_to_execute = [], [], [], []
        else:
            logger.info(f"build_api_calls: role={role}, customer_id={customer_id}")
            api_calls = build_api_calls(
                result, page, user_message=message,
                session_id=str(conversation.id), customer_id=customer_id, role=role,
            )
            if customer_id:
                _resolve_user_placeholders(api_calls, customer_id)

            # ── Shopify: unsupported-intent guard ─────────────────────────────
            # Calls carrying a Woo-only surface (shopify_admin stubs, or a
            # leaked admin/custom_plugin call) cannot be fulfilled here. The
            # woo_client backstop already guarantees no request is sent; this
            # turns that into a clear answer instead of an empty-results reply.
            if ECOMMERCE_BACKEND == "shopify":
                _unsupported = find_unsupported_call(api_calls)
                if _unsupported is not None:
                    _msg, _sugg = unsupported_message_for(intent)
                    logger.info(
                        f"Shopify: intent={intent.value} unsupported | "
                        f"surface={getattr(_unsupported, 'surface', '')} | "
                        f"endpoint={_unsupported.endpoint} — returning guidance"
                    )
                    elapsed = round((time.time() - start_time) * 1000)
                    return _ft((jsonify({
                        "success":     True,
                        "bot_message": _msg,
                        "intent":      intent.value,
                        "products":    [],
                        "suggestions": _sugg,
                        "session_id":  str(conversation.id),
                        "metadata": {
                            "response_time_ms": elapsed,
                            "unsupported_on_shopify": True,
                        },
                        "flow_state":  FlowState.IDLE.value,
                        "pagination":  default_pagination(page),
                        "actions":     [],
                    }), 200))

            # ── Propagate resolved_attr_values for variant pre-filtering ──────
            # _build_product_variations stamps resolved_attr_values into the
            # call body so build_variant_prompt knows which colours/sizes the
            # user already specified.  We carry that hint in user_context.
            for _ac in api_calls:
                _rav = (_ac.body or {}).get("resolved_attr_values")
                if _rav:
                    user_context["resolved_attr_values"] = _rav
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")
                    logger.debug(f"[EntityMerge] Stashed resolved_attr_values={_rav} in user_context")
                    break

            logger.debug(
                f"[EntityMerge] user_context resolved_attr_values at Step 7 = "
                f"{user_context.get('resolved_attr_values')}"
            )
            # ──────────────────────────────────────────────────────────────────

            all_products_raw, order_data, api_responses, api_calls_to_execute = (
                _execute_api_calls(intent, api_calls, _resolve_variant)
            )

            log_matched_products(all_products_raw, api_calls_to_execute, intent=intent)

            _LOCK_STATES = {
                FlowState.AWAITING_VARIANT_SELECTION,
                FlowState.AWAITING_CART_CONFIRMATION,
                FlowState.AWAITING_QUANTITY,
            }

            if all_products_raw:
                first_prod = all_products_raw[0]
                user_context["last_product"] = {"id": first_prod.get("id"), "name": first_prod.get("name")}
                # Persist the (consolidated, merged) filter set so the next turn
                # accumulates on top of it. Only on success — never carry a
                # zero-result filter set forward as a refinement base.
                if intent in _PRODUCT_SEARCH_INTENTS:
                    save_active_search(user_context, entities)
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")
                if current_flow_state not in _LOCK_STATES:
                    user_context["pending_product_id"]   = first_prod.get("id")
                    user_context["pending_product_name"] = first_prod.get("name")
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")

        # ── Step 8: Customer intent handlers ──
        cust_resp = _handle_customer_intents(
            intent, entities, confidence, order_data,
            api_calls_to_execute, api_responses, conversation, page, start_time,
        )
        if cust_resp:
            return cust_resp

        # ── Step 8.5: Bulk order intent (from classifier) ──
        if (
            role in BULK_ORDER_ROLES
            and intent in (Intent.QUICK_ORDER, Intent.PLACE_ORDER, Intent.ORDER_ITEM)
            and not user_context.get("order_for_customer_id")
            and customer_id
        ):
            resp = handle_order_for_prompt(conversation, page, start_time)
            if resp:
                return _ft(resp)

        # ── Intercept: PLACE_ORDER with no product specified — ask what to
        # order instead of falling through to a literal text search for the
        # raw command phrase (e.g. "place new order" searching for "place
        # order" as if it were a product name — guaranteed zero results).
        if intent in (Intent.PLACE_ORDER, Intent.QUICK_ORDER) and not entities.product_id and not entities.product_name:
            elapsed = round((time.time() - start_time) * 1000)
            return _ft((jsonify({
                "success": True,
                "bot_message": "What would you like to order? You can tell me a product name.",
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Browse Products", "Browse categories"],
                "session_id": str(conversation.id),
                "metadata": {"response_time_ms": elapsed},
                "flow_state": FlowState.AWAITING_PRODUCT_OR_CATEGORY.value,
                "pagination": default_pagination(page),
            }), 200))
            
        # ── BULK_ORDER trigger (natural language path) ──
        # Handles the case where the classifier or LLM resolved intent=bulk_order
        # from a natural language message (e.g. "place bulk order") rather than
        # the __BULK_CANCEL__ / __PRODUCT_REORDER__ magic string paths.
        #
        # Open to every role: reps get the full multi-recipient flow, everyone
        # else gets self-scoped multi-line ordering (see the note at the
        # non-rep branch above). This previously required a rep role, which
        # left customers seeing a bulk-order button — SHOW_BULK_ORDER_BUTTON
        # has never been role-gated — that did nothing when they typed their
        # order as a sentence.
        if intent == Intent.BULK_ORDER:
            # If the triggering message ALREADY carries the order lines
            # (e.g. "Order Harmony Moon, Adams Grey, Aurora Taupe"), parse it now.
            # Prompting would throw that message away and ask the rep to retype
            # exactly what they just sent.
            if _is_inline_bulk_order(message, store_loader):
                logger.info(
                    "BULK_ORDER: trigger message contains inline order lines — "
                    "parsing directly instead of prompting for input."
                )
                conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value
                resp = handle_bulk_order_input(
                    message, store_loader, conversation, user_context,
                    page, start_time, pre_resolved=entities,
                )
            else:
                resp = handle_bulk_order_trigger(conversation, user_context, page, start_time)
            if resp:
                return _ft(resp)

        # ── Step 9: Route through specialized handlers ──
        # ── Email filter: narrow rep's orders to a specific recipient ──
        # When a rep asks "status of the order I sent to <email>", keep only the
        # orders whose billing email matches. The fetch is widened to 50 in
        # _build_order_tracking when a lookup_email/date filter is present so the
        # target isn't missed beyond the default page size.
        if role in BULK_ORDER_ROLES and getattr(entities, "lookup_email", None) and order_data:
            _want = entities.lookup_email.lower()
            order_data = [
                o for o in order_data
                if (o.get("billing", {}).get("email") or "").lower() == _want
            ]
        resp = handle_order_status(
            intent, entities, order_data, customer_id, str(conversation.id), page, start_time,
            role=user_context.get("role") or user_context.get("user_role"),
        )
        if resp:
            return _ft(resp)

        resp = handle_reorder(intent, entities, order_data, customer_id, str(conversation.id), page, start_time)
        if resp:
            return _ft(resp)

        resp = handle_order_detail(current_flow_state, customer_id, user_context, str(conversation.id), page, start_time)
        if resp:
            return _ft(resp)

        resp = handle_historical_search(intent, entities, order_data, customer_id, str(conversation.id), page, start_time)
        if resp:
            return _ft(resp)

        resp = handle_variant_selection(
            current_flow_state, intent, entities, message,
            customer_id, str(conversation.id), page, start_time,
            user_context, _resolve_variant,
        )
        if resp:
            return _ft(resp)

        resp = handle_quantity_and_variant_check(
            intent, entities, all_products_raw, order_data,
            ORDER_CREATE_INTENTS, str(conversation.id), page, start_time,
            customer_id=customer_id,
            resolved_attr_values=user_context.get("resolved_attr_values"),
        )
        if resp:
            return _ft(resp)

        resp = handle_quick_order(
            intent, entities, all_products_raw, last_product_ctx,
            customer_id, str(conversation.id), page, start_time, ORDER_CREATE_INTENTS,
        )
        if resp:
            if getattr(entities, "product_id", None):
                user_context["pending_product_id"]   = entities.product_id
                user_context["pending_product_name"] = getattr(entities, "product_name", None)
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
            return _ft(resp)

        logger.debug(
            f"[EntityMerge] Passing resolved_attr_values to handle_variation_product = "
            f"{user_context.get('resolved_attr_values')}"
        )

        resp = handle_variation_product(
            intent,
            entities,
            api_responses,
            api_calls_to_execute,
            confidence,
            order_data,
            str(conversation.id),
            page,
            start_time,
            user_context=user_context,
            conversation=conversation,
            resolved_attr_values=user_context.get("resolved_attr_values"),
            customer_id=customer_id,
            role=role,
        )
        if resp:
            return _ft(resp)

        # ── Zero-result safety net for refined searches ──
        # Accumulated filters can AND down to nothing (e.g. beige + marble).
        # When we know what THIS turn added vs what was already active
        # (_turn_new_snapshot), offer a one-tap fix (Pattern A) instead of a
        # flat dump of every active filter. Fall back to the generic message
        # only when that distinction isn't available.
        if _did_refine and not all_products_raw and intent in _PRODUCT_SEARCH_INTENTS:
            if _turn_new_snapshot:
                _active_slots = _active.get("slots", {}) if _active else {}
                user_context["pending_no_results_choice"] = {
                    "turn_new": _turn_new_snapshot,
                    "active":   _active_slots,
                }
                conversation.flow_state   = FlowState.AWAITING_NO_RESULTS_CHOICE.value
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                db.session.commit()
                return _ft(build_no_results_prompt(
                    _turn_new_snapshot, _active_slots,
                    str(conversation.id), page, start_time,
                ))
            else:
                _filters_labeled = describe_active_filters_labeled(entities)
                _elapsed = time.time() - start_time
                return _ft(jsonify({
                    "success": True,
                    "bot_message": (
                        f"No products found for {_filters_labeled}.\n\n"
                        "Tap **New Search** to start over, or try a different filter."
                        if _filters_labeled else
                        "No products match those filters.\n\nTap **New Search** to start over."
                    ),
                    "intent": intent.value,
                    "products": [],
                    "suggestions": ["New Search"],
                    "session_id": str(conversation.id),
                    "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(_elapsed * 1000)},
                    "flow_state": FlowState.IDLE.value,
                    "pagination": default_pagination(page),
                }))

        all_products_raw, resp = handle_empty_results(
            intent, entities, all_products_raw, message,
            str(conversation.id), page, start_time, confidence, store_loader,
        )
        if resp:
            return _ft(resp)

        # ── Step 10: Final response ──
        return _build_final_response(
            intent, entities, confidence, all_products_raw, order_data,
            api_responses, api_calls_to_execute, conversation, page, start_time,
            payload_context=payload_context,
            customer_id=customer_id,
            refinement_summary=(describe_active_filters(entities) if _did_refine else None),
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Pipeline Crash for session {session_id}: {str(e)}", exc_info=True)

        error_msg = Message(
            conversation_id=conversation.id,
            role="bot",
            content="I encountered a system issue while processing that. Can you try again?",
            intent="error",
        )
        db.session.add(error_msg)
        db.session.commit()

        return jsonify({
            "success":     False,
            "session_id":  str(conversation.id),
            "intent":      "error",
            "bot_message": error_msg.content,
            "products":    [],
            "suggestions": ["Start over", "Browse Products"],
        }), 500