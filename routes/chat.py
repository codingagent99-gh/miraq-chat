"""
Chat endpoint as a Flask Blueprint.
Fully migrated to persistent PostgreSQL storage.
Refactored: business logic extracted into parsers/ and handlers/.
"""

import time
import uuid
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from models import db, Conversation, Message, Intent
from sqlalchemy.orm.attributes import flag_modified
import re
from models import ExtractedEntities, ClassifiedResult, WooAPICall
from app_config import (
    ORDER_INTENTS,
    CART_INTENTS,
    ORDER_CREATE_INTENTS,
    CLASSIFIER_PROVIDER_TAG,
    get_currency_symbol,
    WOO_BASE_URL,
)
from core.actions import build_add_to_cart, build_open_checkout_panel, build_open_cart_panel
from woo_client import woo_client
from formatters import format_product, format_custom_product, format_category, _entities_to_dict
from response_generator import generate_bot_message, generate_suggestions, _resolve_user_placeholders
from classifier import classify
from api_builder import build_api_calls
from conversation_flow import FlowState, handle_flow_state
from chat_logger import get_logger, sanitize_log_string
from store_registry import get_store_loader

from handlers.chat_utils import default_pagination, build_pagination, format_order_for_frontend
from handlers.flow_handler import handle_flow
from handlers.llm_handler import run_llm_fallback
from handlers.order_handler import handle_reorder, handle_order_detail, handle_quick_order, handle_historical_search
from handlers.variant_handler import handle_variant_selection, handle_variation_product, handle_quantity_and_variant_check
from handlers.search_handler import log_matched_products, handle_empty_results
from handlers.suggestion_retry_handler import handle_suggestion_retry
from handlers.filter_clarification_handler import resolve_filter_clarification
from handlers.semantic_clarification_handler import build_semantic_clarification
from parsers.catalog_parser import parse_csv_message
from parsers.address_parser import extract_address, address_summary
from utils.language_utils import detect_and_translate
from handlers.cart_handler import handle_cart_intent
from core.actions import build_propose_checkout_address
logger = get_logger("miraq_chat")
chat_bp = Blueprint("chat", __name__)

def _resolve_variation_slugs(resolved: dict, store_loader) -> list:
    """
    Convert resolved_attributes display names → WC term slugs for the cart payload.
    e.g. {"Colors": "APOLLO Bianco", "Finish": "Matte"}
      → [{"attribute": "pa_colors", "value": "apollobianco"},
         {"attribute": "pa_finish",  "value": "matte"}]
    """
    attr_terms = getattr(store_loader, 'all_attributes_raw', []) if store_loader else []

    # Build lookup: taxonomy → {display_name_lower: slug}
    slug_lookup: dict = {}
    for attr in attr_terms:
        taxonomy = attr.get("taxonomy", "")
        slug_lookup[taxonomy] = {
            term["name"].lower(): term["slug"]
            for term in attr.get("terms", [])
        }

    result = []
    for label, display_value in resolved.items():
        taxonomy = f"pa_{label.lower().replace(' ', '-')}"
        term_map = slug_lookup.get(taxonomy, {})
        # Use matched slug, or fall back to naive slugify (strip spaces/quotes)
        slug = term_map.get(
            str(display_value).lower(),
            re.sub(r'[^a-z0-9]+', '', str(display_value).lower())
        )
        result.append({"attribute": taxonomy, "value": slug})

    return result

def _build_cart_variation_payload(product_id, variation_id, resolved_attrs, store_loader):
    if not variation_id or not product_id:
        return _resolve_variation_slugs(resolved_attrs, store_loader)

    try:
        var_call = WooAPICall(
            method="GET",
            endpoint=f"{WOO_BASE_URL}/products/{product_id}/variations/{variation_id}",
            params={},
            description=f"Fetch variation {variation_id} for cart payload",
        )
        var_resp = woo_client.execute(var_call)
        if not (var_resp.get("success") and isinstance(var_resp.get("data"), dict)):
            raise ValueError("variation fetch failed")

        var_attrs = var_resp["data"].get("attributes", [])

        attr_terms = getattr(store_loader, "all_attributes_raw", []) if store_loader else []
        slug_lookup: dict = {}
        for attr in attr_terms:
            taxonomy = attr.get("taxonomy", "")
            slug_lookup[taxonomy] = {
                term["name"].lower(): term["slug"]
                for term in attr.get("terms", [])
            }

        # Fixed axes — from the variation itself (always correct)
        fixed = {}
        result = []
        for attr in var_attrs:
            taxonomy = attr.get("slug", "")
            option   = attr.get("option", "")
            term_map = slug_lookup.get(taxonomy, {})
            slug = term_map.get(
                option.lower(),
                re.sub(r"[^a-z0-9]+", "", option.lower()),
            )
            result.append({"attribute": taxonomy, "value": slug})
            fixed[taxonomy] = True

        # Wildcard axes — from user's resolved_attrs (cart item meta)
        for label, display_value in resolved_attrs.items():
            taxonomy = f"pa_{label.lower().replace(' ', '-')}"
            if taxonomy in fixed:
                continue
            term_map = slug_lookup.get(taxonomy, {})
            slug = term_map.get(
                str(display_value).lower(),
                re.sub(r"[^a-z0-9]+", "", str(display_value).lower()),
            )
            result.append({"attribute": taxonomy, "value": slug})

        logger.info(f"Cart variation payload: {result}")
        return result

    except Exception as exc:
        logger.warning(f"_build_cart_variation_payload fallback | error={exc}")
        return _resolve_variation_slugs(resolved_attrs, store_loader)
    
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
            from app_config import WOO_BASE_URL
            from models import WooAPICall
            from woo_client import woo_client as _woo
            cust_call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/customers/{customer_id}",
                params={},
                description="Fetch customer address for PROPOSE_CHECKOUT_ADDRESS",
            )
            cust_resp = _woo.execute(cust_call)
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
    miraq_session = request.headers.get('X-MiraQ-Session')
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
    combined_metadata["products"] = data.get("products", [])
    combined_metadata["categories"] = data.get("categories", [])
    combined_metadata["suggestions"] = data.get("suggestions", [])
    combined_metadata["actions"] = data.get("actions", [])

    # 1. Save Bot Message
    bot_msg = Message(
        conversation_id=conversation.id,
        role="bot",
        content=data.get("bot_message", ""),
        intent=data.get("intent", ""),
        metadata_json=combined_metadata, # Save the bundled data
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

@chat_bp.route('/chat/history', methods=['GET'])
def get_chat_history():
    """Fetches paginated chat history for the frontend to hydrate the UI."""
    miraq_session = request.headers.get('X-MiraQ-Session')
    if not miraq_session:
        return jsonify({"messages": [], "has_more": False}), 200

    try:
        session_uuid = uuid.UUID(miraq_session)
        conversation = Conversation.query.get(session_uuid)

        if not conversation:
            return jsonify({"messages": [], "has_more": False}), 200

        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        offset = (page - 1) * limit

        messages_query = Message.query.filter_by(conversation_id=session_uuid)\
            .order_by(Message.created_at.desc())\
            .limit(limit).offset(offset).all()

        total_messages = Message.query.filter_by(conversation_id=session_uuid).count()
        has_more = (offset + limit) < total_messages

        messages_query.reverse()
        
        history = []
        for msg in messages_query:
            item = {
                "role": msg.role,
                "message": msg.content,
                "intent": msg.intent,
                "timestamp": msg.created_at.isoformat(),
            }
            
            # ---> FIX HERE: Unpack the rich UI data for bot messages <---
            if msg.role == "bot" and msg.metadata_json:
                item["products"] = msg.metadata_json.get("products", [])
                item["categories"] = msg.metadata_json.get("categories", [])
                item["suggestions"] = msg.metadata_json.get("suggestions", [])
                item["actions"] = msg.metadata_json.get("actions", [])
                
                # Separate the actual metadata from our bundled UI arrays
                item["metadata"] = {
                    k: v for k, v in msg.metadata_json.items() 
                    if k not in ["products", "categories", "suggestions", "actions"]
                }
                
            history.append(item)

        return jsonify({
            "messages": history,
            "has_more": has_more,
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
    """Return a response if the order intent has no usable product reference."""
    if intent not in ORDER_CREATE_INTENTS or getattr(entities, 'product_id', None):
        return None

    p_name = (getattr(entities, 'product_name', None) or "").lower().strip()
    s_term = (getattr(entities, 'search_term', None) or "").lower().strip()
    generic = {"", "product", "a product", "the product", "item", "an item", "something", "anything", "order", "some"}

    if p_name not in generic or s_term not in generic:
        return None
    if getattr(entities, 'attributes', {}) or getattr(entities, 'target_category_slugs', set()):
        return None

    logger.info(f"🛑 Caught generic order words | p_name='{p_name}' | s_term='{s_term}'")
    elapsed = time.time() - start_time
    return _finalize_turn(conversation, jsonify({
        "success": True,
        "bot_message": "To place an order, please include the product name! For example, you can type: **'I want to order Plumeria'**.",
        "intent": "clarification_needed",
        "products": [],
        "suggestions": ["Show me the catalog", "Cancel"],
        "session_id": str(conversation.id),
        "metadata": {"confidence": 1.0, "products_count": 0, "response_time_ms": round(elapsed * 1000)},
        "pagination": default_pagination(page),
        "flow_state": FlowState.IDLE.value,
    }))


# ══════════════════════════════════════════════════════════════
# ─── HELPER: Execute API and collect products ───
# ══════════════════════════════════════════════════════════════

def _execute_api_calls(intent, api_calls, _resolve_variant):
    """Execute WooCommerce API calls. Returns (all_products_raw, order_data, api_responses, api_calls_executed)."""
    if _resolve_variant:
        return [], [], [], []

    if intent in ORDER_CREATE_INTENTS:
        api_calls_to_execute = [c for c in api_calls if not (c.method == "POST" and "/orders" in c.endpoint)]
    else:
        api_calls_to_execute = api_calls

    api_responses = woo_client.execute_all(api_calls_to_execute)

    all_products_raw = []
    order_data = []

    def _enrich(prod_list):
        for p in prod_list:
            if "type" not in p:
                p["type"] = "variable" if p.get("variations") else "simple"

    for resp in api_responses:
        if resp.get("success"):
            data = resp.get("data")
            target = order_data if intent in ORDER_INTENTS else all_products_raw
            if isinstance(data, dict) and "products" in data:
                _enrich(data["products"])
                target.extend(data["products"])
            elif isinstance(data, dict) and "orders" in data:   # ← add this
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
):
    """Format products and build the final JSON response."""
    products = []
    categories = []
    suggestions_list = []

    if intent in (Intent.CATEGORY_LIST, Intent.PRODUCT_CATALOG):
        seen_names = set()
        for cat in all_products_raw:
            name = cat.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                categories.append({
                    "id": cat.get("id"),
                    "name": name.replace("&amp;", "&"),
                    "slug": cat.get("slug", ""),
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

    products = [p for p in products if p.get("name")]
    pagination = build_pagination(page, api_responses, api_calls_to_execute)

    if intent in (Intent.CATEGORY_LIST, Intent.PRODUCT_CATALOG):
        bot_message = "Here are our top categories to help you get started!"
        suggestions_list = ["Cancel"]
    else:
        bot_message = generate_bot_message(
            intent, entities, products, confidence, order_data,
            total_items=pagination.get("total_items"), page=page,
        )
        suggestions_list = generate_suggestions(intent, entities, products)

    # ── Determine flow state ──────────────────────────────────────────────────
    #
    # Priority order:
    #   1. Checkout/order success      → AWAITING_ANYTHING_ELSE
    #   2. Single product found during browsing → AWAITING_CART_CONFIRMATION
    #   3. Everything else             → IDLE
    #
    # AWAITING_CART_CONFIRMATION only fires when:
    #   - Intent is a browsing/search intent (not an order intent)
    #   - Exactly one product came back (ambiguous multi-results shouldn't
    #     auto-prompt "add to cart?")
    #   - A product_id is resolvable (so the Yes handler has something to add)
    #
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
        and (
            entities.product_id
            or conversation.context_data.get("pending_product_id")
        )
    )

    if intent in ORDER_CREATE_INTENTS and order_data:
        next_flow_state = FlowState.AWAITING_ANYTHING_ELSE.value

    elif intent in _BROWSING_INTENTS and _single_product_found:
        next_flow_state = FlowState.AWAITING_CART_CONFIRMATION.value
        # Inject confirmation prompt into bot message and suggestions
        product_name = products[0].get("name", "this product")
        bot_message = (
            f"{bot_message}\n\nWould you like to add **{product_name}** to your cart?"
        )
        suggestions_list = ["Yes, add it", "No thanks", "Browse products"]

    else:
        next_flow_state = FlowState.IDLE.value

    # ── Build response ────────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    response = {
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        "products": products,
        "categories": categories,
        "suggestions": suggestions_list,
        "session_id": str(conversation.id),
        "metadata": {
            "confidence": round(confidence, 2),
            "products_count": len(products),
            "categories_count": len(categories),
            "provider": CLASSIFIER_PROVIDER_TAG,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response_time_ms": round(elapsed * 1000),
            "intent_raw": intent.value,
            "entities": _entities_to_dict(entities),
        },
        "pagination": pagination,
        "flow_state": next_flow_state,
    }

    if intent in (Intent.ORDER_HISTORY, Intent.LAST_ORDER) and order_data:
        response["orders"] = [format_order_for_frontend(o) for o in order_data]
        response["order_pagination"] = build_pagination(
            page, api_responses, api_calls_to_execute
        )

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
        elapsed = int((time.time() - start_time) * 1000)
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
            "success": True,
            "bot_message": "Here's what I have on file:\n" + "\n".join(lines),
            "intent": "fetch_customer",
            "products": [],
            "suggestions": [],
            "session_id": str(conversation.id),
            "metadata": {"confidence": round(confidence, 2), "response_time_ms": elapsed},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }))

    if intent == Intent.UPDATE_CUSTOMER:
        elapsed = int((time.time() - start_time) * 1000)
        update_success = False
        for _api_call, _api_resp in zip(api_calls_to_execute, api_responses):
            if _api_call.method == "PUT" and "/customers/" in _api_call.endpoint:
                update_success = _api_resp.get("success", False)
                break
        _update_signal = [{"success": update_success}]

        return _finalize_turn(conversation, jsonify({
            "success": update_success,
            "bot_message": generate_bot_message(intent, entities, [], confidence, _update_signal),
            "intent": intent.value,
            "products": [],
            "suggestions": generate_suggestions(intent, entities, []),
            "session_id": str(conversation.id),
            "metadata": {
                "confidence": round(confidence, 2),
                "products_count": 0,
                "provider": CLASSIFIER_PROVIDER_TAG,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "response_time_ms": elapsed,
                "intent_raw": intent.value,
                "entities": _entities_to_dict(entities),
            },
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }))

    return None


# ══════════════════════════════════════════════════════════════
# ─── MAIN CHAT PIPELINE ───
# ══════════════════════════════════════════════════════════════

@chat_bp.route("/chat", methods=["POST"])
def chat():
    start_time = time.time()

    # ── Parse request ──
    body = request.get_json(silent=True)
    if not body:
        logger.warning("POST /chat | Invalid JSON body")
        return jsonify({
            "success": False,
            "bot_message": "Invalid request. Send JSON with 'message' field.",
            "intent": "error", "products": [],
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": "", "metadata": {"error": "Invalid JSON body"},
            "pagination": default_pagination(),
        }), 400

    message = body.get("message", "").strip()
    page = int(body.get("page", 1))

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
    session_id = resolve_session_id()
    conversation = Conversation.query.get(session_id)
    if not conversation:
        conversation = Conversation(id=session_id)
        db.session.add(conversation)
        db.session.commit()

    payload_context = body.get("user_context", {})
    if payload_context.get("customer_id") and not conversation.customer_id:
        conversation.customer_id = payload_context.get("customer_id")
        db.session.commit()

    customer_id = conversation.customer_id
    user_context = conversation.context_data or {}

    logger.info(f"[MEMORY TRACE 1] INCOMING from Frontend Payload: {payload_context}")
    logger.info(f"[MEMORY TRACE 2] LOADED from Postgres DB: {user_context}")

    if user_context is not conversation.context_data:
        conversation.context_data = user_context

    truncated_msg = message[:100] + "..." if len(message) > 100 else message
    logger.info(f'POST /chat | session={session_id} | message="{sanitize_log_string(truncated_msg)}" | customer_id={customer_id} | flow_state={conversation.flow_state}')

    if not message:
        return jsonify({
            "success": False,
            "bot_message": "Please type a message! Try asking about our products, categories, or your orders.",
            "intent": "error", "products": [],
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": str(conversation.id), "metadata": {"error": "Empty message"},
            "pagination": default_pagination(page),
        }), 400

    try:
        # ── Save user message ──
        user_msg = Message(conversation_id=conversation.id, role="user", content=message)
        db.session.add(user_msg)
        db.session.commit()

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

        # ── Step 0.5: Suggestion retry (early exit) ──
        sr_resp = handle_suggestion_retry(body, message, str(conversation.id), customer_id, page, start_time)
        if sr_resp:
            return _ft(sr_resp)

        # ── Step 1: Filter clarification bypass ──
        _skip_classification = False
        bypass_result = None

        forced_search_match = re.match(r"(?i)^no\s*-\s*search\s*for\s*['\"](.*?)['\"]$", message)

        if current_flow_state == FlowState.AWAITING_FILTER_CLARIFICATION:
            pending_semantic = user_context.get("pending_semantic_match")
            if pending_semantic:
                clarification_result = resolve_filter_clarification(message, user_context, pending_semantic)
                if clarification_result:
                    current_flow_state = FlowState.IDLE
                    user_context["flow_state"] = FlowState.IDLE.value
                    conversation.context_data = user_context
                    bypass_result = clarification_result
                    _skip_classification = True
                    
        elif forced_search_match:
            # Issue 2 Fix: Catch pagination/retry clicks that send the rejection string outside the flow state
            extracted_term = forced_search_match.group(1)
            logger.info(f"Intercepted explicit forced search string. Term: '{extracted_term}'")
            bypass_entities = ExtractedEntities(search_term=extracted_term)
            bypass_result = ClassifiedResult(
                intent=Intent.PRODUCT_SEARCH,
                entities=bypass_entities,
                confidence=1.0
            )
            _skip_classification = True

        # ── Step 2: Conversation flow state machine ──
        flow_context = {
            "pending_product_name": user_context.get("pending_product_name"),
            "pending_product_id": user_context.get("pending_product_id"),
            "pending_quantity": user_context.get("pending_quantity"),
            "pending_variation_id": user_context.get("pending_variation_id"),
            "resolved_attributes": user_context.get("resolved_attributes"),
        }

        flow_result = None
        if current_flow_state != FlowState.IDLE:
            flow_result = handle_flow_state(
                state=current_flow_state, message=message,
                entities=flow_context, confidence=0.0,
            )
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
            logger.info(f"[MEMORY TRACE 3] STATE MACHINE returned: {flow_result}")
            logger.info(f"[MEMORY TRACE 4] UPDATED user_context: {user_context}")

        # ── Cart confirmation PROMPT intercept (after AWAITING_QUANTITY) ──
        if flow_result and flow_result.get("action") == "prompt_cart_confirmation":
            pid       = user_context.get("pending_product_id")
            vid       = user_context.get("pending_variation_id")
            qty       = user_context.get("pending_quantity") or 1
            name      = user_context.get("pending_product_name", "item")
            resolved  = user_context.get("resolved_attributes") or {}
            variant_label = " / ".join(str(v) for v in resolved.values()) if resolved else ""
            variant_suffix = f" ({variant_label})" if variant_label else ""

            elapsed = round((time.time() - start_time) * 1000)
            return _finalize_turn(conversation, jsonify({
                "success":     True,
                "bot_message": f"Got it — add **{name}**{variant_suffix} ×{qty} to your cart?",
                "intent":      "guided_flow",
                "products":    [],
                "suggestions": ["Yes, add it", "No thanks", "Browse products"],
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
            }))
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # ── Cart confirmation intercept — MUST be before handle_flow ──
        # handle_flow crashes on any flow_result without "bot_message".
        # Action-based results are owned here and must never reach it.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if flow_result and flow_result.get("action") == "confirm_add_to_cart":
            pid   = user_context.get("pending_product_id")
            vid   = user_context.get("pending_variation_id")
            qty   = user_context.get("pending_quantity") or 1
            name  = user_context.get("pending_product_name", "item")
            resolved = user_context.get("resolved_attributes") or {}
            variation_attributes = _resolve_variation_slugs(resolved, get_store_loader())

            if pid:
                elapsed = round((time.time() - start_time) * 1000)
                actions = [build_add_to_cart(
                    product_id   = pid,
                    quantity     = qty,
                    variation_id = vid,
                    variation    = variation_attributes,
                )]
                # Open the CART panel so the user can review what was added
                # before proceeding to checkout. (Previously this opened the
                # checkout panel directly, skipping cart review and producing
                # a duplicate checkout CTA alongside the suggestion chip.)
                actions.append(build_open_cart_panel())

                bot_msg = f"✅ Added **{name}** ×{qty} to your cart. Opening your cart so you can review…"
                # Single, unambiguous next-step suggestion set. "Proceed to
                # checkout" is the ONLY checkout entry-point now — the
                # OPEN_CHECKOUT_PANEL action is no longer auto-emitted here.
                suggestions = ["Proceed to checkout", "Continue shopping", "View cart"]

                return _ft(jsonify({
                    "success":     True,
                    "bot_message": bot_msg,
                    "intent":      Intent.ADD_TO_CART.value,
                    "suggestions": suggestions,
                    "session_id":  str(conversation.id),
                    "pagination":  default_pagination(page),
                    "flow_state":  FlowState.IDLE.value,
                    "actions":     actions,
                }))
                
        if flow_result and flow_result.get("action") == "decline_add_to_cart":
            elapsed = round((time.time() - start_time) * 1000)
            return _ft(jsonify({
                "success":     True,
                "bot_message": "No problem! What else are you looking for?",
                "intent":      "browse",
                "products":    [],
                "suggestions": ["Show me products", "View categories", "View cart"],
                "session_id":  str(conversation.id),
                "metadata":    {"response_time_ms": elapsed},
                "pagination":  default_pagination(page),
                "flow_state":  FlowState.IDLE.value,
            }))

        # ── Flow router ──────────────────────────────────────────────────
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

        # else: fall through to classifier

        # ── Step 3: Classify ──
        _resolve_variant = bool(flow_result and flow_result.get("resolve_variant"))
        store_loader = get_store_loader()

        if _skip_classification:
            result = bypass_result
        else:
            result = parse_csv_message(message, store_loader)

        intent = result.intent
        entities = result.entities
        confidence = result.confidence

        # Lock in variant state
        if current_flow_state == FlowState.AWAITING_VARIANT_SELECTION:
            _resolve_variant = True
            intent = Intent.QUICK_ORDER
            result.intent = intent
            entities.product_id = user_context.get("pending_product_id")
            entities.product_name = user_context.get("pending_product_name")
            confidence = 1.0

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
                    if hasattr(llm_outcome, 'get_data'):
                        return _ft(llm_outcome)
                    if isinstance(llm_outcome, tuple) and len(llm_outcome) == 2 and isinstance(llm_outcome[1], int):
                        return _ft(llm_outcome)
                if isinstance(llm_outcome, tuple) and len(llm_outcome) == 4:
                    intent, entities, confidence, result = llm_outcome

        # ── Step 5: Semantic clarification ──
        
        if (entities.semantic_matches
            and current_flow_state != FlowState.AWAITING_FILTER_CLARIFICATION
            and intent not in (
                Intent.PRODUCT_ATTRIBUTE_INFO, Intent.PRODUCT_VARIATIONS,
                Intent.QUICK_ORDER, Intent.PLACE_ORDER, Intent.ORDER_ITEM,
            )):
            clarification_resp = build_semantic_clarification(
                entities, user_context, str(conversation.id), page, start_time, flow_result,
            )
            if clarification_resp:
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                return _ft(clarification_resp)

        # ── Step 6: Empty order guard ──
        empty_resp = _check_empty_order(intent, entities, conversation, page, start_time)
        if empty_resp:
            return empty_resp
        
        # ── Step 6.5: Cart intent fork ──
        if intent in CART_INTENTS or intent == Intent.CHECKOUT:
            # handle_cart_intent will now call woo_cart.py
            cart_resp = handle_cart_intent(
                intent, entities, user_context, conversation, page, start_time
            )
            if cart_resp is not None:
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                return _ft(cart_resp)
            
        # ── Step 7: Execute API calls ──
        last_product_ctx = user_context.get("last_product")

        if _resolve_variant:
            # Variant resolution uses session-cached variations — no API calls needed.
            # Skipping build_api_calls entirely prevents or_pairs dict-vs-OrPair crashes
            # that occur when catalog_parser populates attr_tag_or_pairs as plain dicts.
            api_calls = []
            all_products_raw, order_data, api_responses, api_calls_to_execute = [], [], [], []
        else:
            role = payload_context.get("role")
            logger.info(f"build_api_calls: role={role}, customer_id={customer_id}")
            api_calls = build_api_calls(result, page, user_message=message, session_id=str(conversation.id), customer_id=customer_id, role=role)
            if customer_id:
                _resolve_user_placeholders(api_calls, customer_id)

            all_products_raw, order_data, api_responses, api_calls_to_execute = _execute_api_calls(intent, api_calls, _resolve_variant)

            log_matched_products(all_products_raw, api_calls_to_execute)

            if all_products_raw:
                first_prod = all_products_raw[0]
                user_context["last_product"] = {"id": first_prod.get("id"), "name": first_prod.get("name")}
                if current_flow_state != FlowState.AWAITING_VARIANT_SELECTION:
                    user_context["pending_product_id"] = first_prod.get("id")
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

        # ── Step 9: Route through specialized handlers ──
        resp = handle_reorder(intent, entities, order_data, customer_id, str(conversation.id), page, start_time)
        if resp:
            return _ft(resp)

        resp = handle_order_detail(current_flow_state, customer_id, user_context, str(conversation.id), page, start_time)
        if resp:
            return _ft(resp)

        resp = handle_historical_search(intent, entities, order_data, customer_id, str(conversation.id), page, start_time)
        if resp:
            return _ft(resp)

        resp = handle_variant_selection(current_flow_state, intent, entities, message, customer_id, str(conversation.id), page, start_time, user_context, _resolve_variant)
        if resp:
            return _ft(resp)

        resp = handle_quantity_and_variant_check(intent, entities, all_products_raw, order_data, ORDER_CREATE_INTENTS, str(conversation.id), page, start_time, customer_id=customer_id)
        if resp:
            return _ft(resp)

        resp = handle_quick_order(intent, entities, all_products_raw, last_product_ctx, customer_id, str(conversation.id), page, start_time, ORDER_CREATE_INTENTS)
        if resp:
            if getattr(entities, 'product_id', None):
                user_context["pending_product_id"] = entities.product_id
                user_context["pending_product_name"] = getattr(entities, 'product_name', None)
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
            return _ft(resp)

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
            )
        if resp:
            return _ft(resp)

        store_loader = get_store_loader()
        all_products_raw, resp = handle_empty_results(intent, entities, all_products_raw, message, str(conversation.id), page, start_time, confidence, store_loader)
        if resp:
            return _ft(resp)

        # ── Step 10: Final response ──
        return _build_final_response(
            intent, entities, confidence, all_products_raw, order_data,
            api_responses, api_calls_to_execute, conversation, page, start_time,
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Pipeline Crash for session {session_id}: {str(e)}")

        error_msg = Message(
            conversation_id=conversation.id,
            role="bot",
            content="I encountered a system issue while processing that. Can you try again?",
            intent="error",
        )
        db.session.add(error_msg)
        db.session.commit()

        return jsonify({
            "success": False,
            "session_id": str(conversation.id),
            "intent": "error",
            "bot_message": error_msg.content,
            "products": [],
            "suggestions": ["Start over", "Show me all products"],
        }), 500