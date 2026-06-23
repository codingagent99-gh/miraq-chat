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
from classifier.utils import normalize_for_tag_compare

from models import ExtractedEntities, ClassifiedResult, WooAPICall
from app_config import (
    ORDER_INTENTS,
    CART_INTENTS,
    ORDER_CREATE_INTENTS,
    CLASSIFIER_PROVIDER_TAG,
    BULK_ORDER_ROLES,
    get_currency_symbol,
)
from core.actions import build_add_to_cart, build_open_checkout_panel, build_open_cart_panel
from woo_client import woo_client
from formatters import format_product, format_custom_product, format_category, _entities_to_dict
from response_generator import generate_bot_message, generate_suggestions, _resolve_user_placeholders
from classifier import classify
from api_builder import build_api_calls
from conversation_flow import FlowState, handle_flow_state, is_order_flow, _flow_context_message
from chat_logger import get_logger, sanitize_log_string
from store_registry import get_store_loader
from ecommerce import endpoints
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
from config.store_config import SEMANTIC_AUTO_APPLY_THRESHOLD
from parsers.catalog_parser import parse_csv_message
from parsers.address_parser import extract_address, address_summary
from utils.language_utils import detect_and_translate
from handlers.cart_handler import handle_cart_intent
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

logger = get_logger("miraq_chat")
chat_bp = Blueprint("chat", __name__)


# ══════════════════════════════════════════════════════════════
# ─── MODULE-LEVEL HELPERS ───
# ══════════════════════════════════════════════════════════════

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

    return intent, entities, confidence


def _dispatch_bulk_action(action, message, role, store_loader, conversation, user_context, page, start_time):
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
            message, conversation, user_context, page, start_time
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

        _is_shopify = isinstance(vid, str) and vid.startswith("gid://")
        if _is_shopify:
            from core.actions import build_shopify_add_to_cart
            actions = [build_shopify_add_to_cart(
                variant_gid=vid,
                quantity=qty,
                name=name,
            )]
        else:
            variation_attributes = endpoints.build_cart_variation_payload(
                product_id=pid,
                variant_id=vid,
                resolved_attrs=resolved,
                store_loader=store_loader,
            )
            actions = [build_add_to_cart(
                product_id=pid,
                quantity=qty,
                name=name,
                variation_id=vid,
                variation=variation_attributes,
            )]
        actions.append(build_open_cart_panel())

        return jsonify({
            "success":     True,
            "bot_message": f"✅ Added **{name}** ×{qty} to your cart. Opening your cart so you can review…",
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
            "suggestions": ["Show me products", "View categories", "View cart"],
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

        # Fetch the customer's saved billing/shipping for the "existing_on_file" field
        existing = None
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
# ─── DATABASE SESSION HELPERS ───
# ══════════════════════════════════════════════════════════════

def resolve_session_id():
    """Resolves the chat session ID from X-MiraQ-Session header or generates a new one."""
    miraq_session = request.headers.get("X-MiraQ-Session")
    if miraq_session:
        try:
            return uuid.UUID(miraq_session)
        except ValueError:
            logger.warning(f"Invalid X-MiraQ-Session format received: {miraq_session}")
    return uuid.uuid4()


def _finalize_turn(
    conversation,
    flask_response,
    *,
    _proposal_message=None,
    _proposal_customer_id=None,
    _proposal_flow_state=None,
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

    combined_metadata = data.get("metadata", {}).copy()
    combined_metadata["products"]    = data.get("products", [])
    combined_metadata["categories"]  = data.get("categories", [])
    combined_metadata["suggestions"] = data.get("suggestions", [])
    combined_metadata["actions"]     = data.get("actions", [])

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
                "message":   msg.content,
                "intent":    msg.intent,
                "timestamp": msg.created_at.isoformat(),
            }
            if msg.role == "bot" and msg.metadata_json:
                item["products"]    = msg.metadata_json.get("products", [])
                item["categories"]  = msg.metadata_json.get("categories", [])
                item["suggestions"] = msg.metadata_json.get("suggestions", [])
                item["actions"]     = msg.metadata_json.get("actions", [])
                item["metadata"]    = {
                    k: v for k, v in msg.metadata_json.items()
                    if k not in ("products", "categories", "suggestions", "actions")
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
    woo_calls           = [c for c in api_calls_to_execute
                           if getattr(c, "surface", "") not in ("shopify_graphql", "shopify_orders")]

    api_responses = woo_client.execute_all(woo_calls)

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
                products.append(format_custom_product(p))
            else:
                products.append(format_product(p))

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
        response["orders"]            = [format_order_for_frontend(o) for o in order_data]
        response["order_pagination"]  = build_pagination(page, api_responses, api_calls_to_execute)

    _sr_ctx     = payload_context or {}
    _sr_role    = _sr_ctx.get("role", "")
    _sr_actions = response.get("actions", [])

    if _sr_role in BULK_ORDER_ROLES and customer_id and products:
        _sr_actions.append({"type": "SHOW_RECENTLY_ORDERED_BUTTON", "payload": {}})

        # Product order history — only when a specific product resolved
        _searched_product_id = getattr(entities, "product_id", None)
        # if not _searched_product_id and len(products) == 1:
        #     _searched_product_id = products[0].get("id")

        if _searched_product_id:
            _recent_orders = _fetch_product_order_history(_searched_product_id, _sr_role)
            if _recent_orders:
                _sr_actions.append({
                    "type": "SHOW_PRODUCT_RECENT_ORDERS",
                    "payload": {
                        "orders": _format_product_orders_for_action(_recent_orders),
                    },
                })

    if customer_id:
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
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": "", "metadata": {"error": "Invalid JSON body"},
            "pagination": default_pagination(),
        }), 400

    message = body.get("message", "").strip()
    page    = int(body.get("page", 1))

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
            "suggestions": ["Show me all products", "What categories do you have?"],
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
            """Local alias: wraps _finalize_turn with address-proposal context."""
            return _finalize_turn(
                conversation, resp,
                _proposal_message=message,
                _proposal_customer_id=customer_id,
                _proposal_flow_state=current_flow_state,
            )

        # ── Resolve flow state ──
        try:
            current_flow_state = FlowState(conversation.flow_state)
        except ValueError:
            current_flow_state = FlowState.IDLE

        _wipe_stale_cart(conversation, user_context, current_flow_state)

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

        # ── Early action dispatch ──────────────────────────────────────────
        # Bulk/rep flow actions short-circuit here before classification so
        # replies like "Yes, confirm" or "Change address" are never misrouted
        # to the product-search or update_customer classifier paths.
        if _flow_action:
            resp = _dispatch_bulk_action(
                _flow_action, message, role, store_loader,
                conversation, user_context, page, start_time,
            )
            if resp is not None:
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

        # ── Step 4.5: Size-type ambiguity check ──────────────────────────
        # "Size" on the storefront maps to three distinct WooCommerce
        # attributes (Sample Size, Tile Size, Chip Size). If extraction
        # independently populated 2+ of these with the SAME value, that's
        # one ambiguous value, not three separate filters — ask which
        # taxonomy the user meant instead of silently OR-ing all of them.
        _SIZE_TYPE_LABELS = ['sample size', 'tile size', 'chip size']
        _size_norm_list = [normalize_for_tag_compare(l) for l in _SIZE_TYPE_LABELS]
        _size_keys_present = [
            k for k in entities.attributes
            if any(normalize_for_tag_compare(k.replace("-", " ")) == s for s in _size_norm_list)
        ]
        if len(_size_keys_present) > 1:
            _values = {entities.attributes[k] for k in _size_keys_present}
            if len(_values) == 1:
                _shared_value = _values.pop()
                _candidates = []
                for k in _size_keys_present:
                    if attr_slug_for_label(k):  # validity check only
                        _candidates.append({
                            "type": "attribute",
                            "taxonomy": k,
                            "slug": _shared_value,
                            "suggested_name": k.title(),
                            "user_text": _shared_value,
                            "is_negative": False,
                            "score": 1.0,
                        })
                if len(_candidates) > 1:
                    for k in _size_keys_present:
                        del entities.attributes[k]
                    entities.semantic_matches = [_candidates]

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
                _m = _sem_groups[0][0]
                apply_semantic_match(entities, _m)
                entities.semantic_matches = []
                entities.search_term = None  # discard noise leftover (e.g. "filter")
                _auto_applied = True
                logger.info(
                    f"[SemanticAutoApply] score={_m.get('score', 0):.4f} >= {SEMANTIC_AUTO_APPLY_THRESHOLD}"
                    f" | applied {_m['type']}:{_m['suggested_name']}"
                )
                _resolve_tag_attribute_overlap(entities)


            if not _auto_applied:
                clarification_resp = build_semantic_clarification(
                    entities, user_context, str(conversation.id), page, start_time, flow_result,
                )
                if clarification_resp:
                    conversation.context_data = user_context
                    flag_modified(conversation, "context_data")
                    return _ft(clarification_resp)

        if intent == Intent.BULK_ORDER and role not in BULK_ORDER_ROLES:
            intent = Intent.QUICK_ORDER
            if result is not None:
                result.intent = intent
                
        # ── Step 6: Empty order guard ──
        empty_resp = _check_empty_order(intent, entities, conversation, page, start_time)
        if empty_resp:
            return empty_resp

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
            Intent.CATEGORY_BROWSE, Intent.PRODUCT_LIST, Intent.PRODUCT_BY_TAG,
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
            if _active and active_search_is_fresh(_active):
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
                # User said "category" explicitly — restore it as a standalone filter,
                # dropping the OR-with-attribute version entirely.
                entities.target_category_slugs = _matched_cats
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
                "suggestions": ["Show me products", "Browse categories"],
                "session_id": str(conversation.id),
                "metadata": {"response_time_ms": elapsed},
                "flow_state": FlowState.AWAITING_PRODUCT_OR_CATEGORY.value,
                "pagination": default_pagination(page),
            }), 200))
            
        # ── BULK_ORDER trigger (natural language path) ──
        # Handles the case where the classifier or LLM resolved intent=bulk_order
        # from a natural language message (e.g. "place bulk order") rather than
        # the __BULK_CANCEL__ / __PRODUCT_REORDER__ magic string paths.
        if intent == Intent.BULK_ORDER and role in BULK_ORDER_ROLES:
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
        resp = handle_order_status(intent, entities, order_data, customer_id, str(conversation.id), page, start_time)
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
            "suggestions": ["Start over", "Show me all products"],
        }), 500