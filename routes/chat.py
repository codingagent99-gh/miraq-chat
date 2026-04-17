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

from models import ExtractedEntities, ClassifiedResult, WooAPICall
from app_config import (
    ORDER_INTENTS,
    ORDER_CREATE_INTENTS,
    CLASSIFIER_PROVIDER_TAG,
    get_currency_symbol,
)
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
from utils.language_utils import detect_and_translate

logger = get_logger("miraq_chat")
chat_bp = Blueprint("chat", __name__)


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


def _finalize_turn(conversation, flask_response):
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

    # 1. Save Bot Message
    bot_msg = Message(
        conversation_id=conversation.id,
        role="bot",
        content=data.get("bot_message", ""),
        intent=data.get("intent", ""),
        metadata_json=data.get("metadata", {}),
    )
    db.session.add(bot_msg)

    # 2. Update Conversation State
    conversation.flow_state = data.get("flow_state", conversation.flow_state)

    context_data = dict(conversation.context_data)

    _WIPE_KEYS = [
        "pending_product_id", "pending_product_name",
        "pending_quantity", "pending_variation_id", "resolved_attributes",
        "pending_shipping_address", "use_existing_address", "use_new_address",
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
            history.append({
                "role": msg.role,
                "message": msg.content,
                "intent": msg.intent,
                "timestamp": msg.created_at.isoformat(),
            })

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
    }

    if intent in (Intent.ORDER_HISTORY, Intent.LAST_ORDER) and order_data:
        response["orders"] = [format_order_for_frontend(o) for o in order_data]
        response["order_pagination"] = build_pagination(page, api_responses, api_calls_to_execute)

    response["flow_state"] = (
        FlowState.AWAITING_ANYTHING_ELSE.value
        if intent in ORDER_CREATE_INTENTS and order_data
        else FlowState.IDLE.value
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

        mock_sessions = {str(session_id): {"history": [], "user_context": user_context}}

        # ── Resolve flow state ──
        try:
            current_flow_state = FlowState(conversation.flow_state)
        except ValueError:
            current_flow_state = FlowState.IDLE

        _wipe_stale_cart(conversation, user_context, current_flow_state)

        # ── Step 0.5: Suggestion retry (early exit) ──
        sr_resp = handle_suggestion_retry(body, message, str(conversation.id), customer_id, page, start_time)
        if sr_resp:
            return _finalize_turn(conversation, sr_resp)

        # ── Step 1: Filter clarification bypass ──
        _skip_classification = False
        bypass_result = None

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
                "pending_variation_id", "pending_shipping_address",
                "use_existing_address", "use_new_address", "resolved_attributes",
            ]
            for k in _persistent_keys:
                if k in flow_result and flow_result[k] is not None:
                    user_context[k] = flow_result[k]
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")
            logger.info(f"[MEMORY TRACE 3] STATE MACHINE returned: {flow_result}")
            logger.info(f"[MEMORY TRACE 4] UPDATED user_context: {user_context}")

        if flow_result and not flow_result.get("pass_through") and not flow_result.get("override_message"):
            resp = handle_flow(flow_result, user_context, str(conversation.id), customer_id, page, start_time, mock_sessions)
            if resp:
                return _finalize_turn(conversation, resp)
        elif flow_result:
            resp = handle_flow(flow_result, user_context, str(conversation.id), customer_id, page, start_time, mock_sessions)
            if resp:
                return _finalize_turn(conversation, resp)

        # ── Step 3: Classify ──
        _resolve_variant = bool(flow_result and flow_result.get("resolve_variant"))
        store_loader = get_store_loader()

        if _skip_classification:
            result = bypass_result
        else:
            result = parse_csv_message(message, store_loader)
            if not result:
                result = classify(message)

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
                sessions=mock_sessions,
            )

            if llm_outcome is not None:
                if not isinstance(llm_outcome, tuple) or not isinstance(llm_outcome[0], tuple):
                    if hasattr(llm_outcome, 'get_data'):
                        return _finalize_turn(conversation, llm_outcome)
                    if isinstance(llm_outcome, tuple) and len(llm_outcome) == 2 and isinstance(llm_outcome[1], int):
                        return _finalize_turn(conversation, llm_outcome)
                if isinstance(llm_outcome, tuple) and len(llm_outcome) == 4:
                    intent, entities, confidence, result = llm_outcome

        # ── Step 5: Semantic clarification ──
        if (entities.semantic_matches
            and current_flow_state != FlowState.AWAITING_FILTER_CLARIFICATION
            and intent not in (Intent.PRODUCT_ATTRIBUTE_INFO, Intent.PRODUCT_VARIATIONS)):
            clarification_resp = build_semantic_clarification(
                entities, user_context, str(conversation.id), page, start_time, flow_result,
            )
            if clarification_resp:
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
                return _finalize_turn(conversation, clarification_resp)

        # ── Step 6: Empty order guard ──
        empty_resp = _check_empty_order(intent, entities, conversation, page, start_time)
        if empty_resp:
            return empty_resp

        # ── Step 7: Execute API calls ──
        api_calls = build_api_calls(result, page, user_message=message, session_id=str(conversation.id), customer_id=customer_id)
        if customer_id:
            _resolve_user_placeholders(api_calls, customer_id)

        last_product_ctx = user_context.get("last_product")
        all_products_raw, order_data, api_responses, api_calls_to_execute = _execute_api_calls(intent, api_calls, _resolve_variant)

        if not _resolve_variant:
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
        resp = handle_reorder(intent, entities, order_data, customer_id, str(conversation.id), page, start_time, mock_sessions)
        if resp:
            return _finalize_turn(conversation, resp)

        resp = handle_order_detail(current_flow_state, customer_id, user_context, str(conversation.id), page, start_time)
        if resp:
            return _finalize_turn(conversation, resp)

        resp = handle_historical_search(intent, entities, order_data, customer_id, str(conversation.id), page, start_time, mock_sessions)
        if resp:
            return _finalize_turn(conversation, resp)

        resp = handle_variant_selection(current_flow_state, intent, entities, message, customer_id, str(conversation.id), page, start_time, mock_sessions, user_context, _resolve_variant)
        if resp:
            return _finalize_turn(conversation, resp)

        resp = handle_quantity_and_variant_check(intent, entities, all_products_raw, order_data, ORDER_CREATE_INTENTS, str(conversation.id), page, start_time, mock_sessions, customer_id=customer_id)
        if resp:
            return _finalize_turn(conversation, resp)

        resp = handle_quick_order(intent, entities, all_products_raw, last_product_ctx, customer_id, str(conversation.id), page, start_time, mock_sessions, ORDER_CREATE_INTENTS)
        if resp:
            if getattr(entities, 'product_id', None):
                user_context["pending_product_id"] = entities.product_id
                user_context["pending_product_name"] = getattr(entities, 'product_name', None)
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
            return _finalize_turn(conversation, resp)

        resp = handle_variation_product(intent, entities, api_responses, api_calls_to_execute, confidence, order_data, str(conversation.id), page, start_time, mock_sessions)
        if resp:
            return _finalize_turn(conversation, resp)

        store_loader = get_store_loader()
        all_products_raw, resp = handle_empty_results(intent, entities, all_products_raw, message, str(conversation.id), page, start_time, confidence, mock_sessions, store_loader)
        if resp:
            return _finalize_turn(conversation, resp)

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