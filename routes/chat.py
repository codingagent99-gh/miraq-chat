"""
Chat endpoint as a Flask Blueprint.
"""

import os
import time
from datetime import datetime, timezone
from typing import List, Dict

from flask import Blueprint, request, jsonify

from app_config import (
    WOO_BASE_URL,
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
    ORDER_INTENTS,
    ORDER_CREATE_INTENTS,
    LLM_FALLBACK_ENABLED,
    LLM_RETRY_ON_EMPTY_RESULTS,
)
from woo_client import woo_client
from formatters import (
    format_product,
    format_custom_product,
    format_variation,
    _filter_variations_by_entities,
    _entities_to_dict,
)
from response_generator import (
    generate_bot_message,
    generate_suggestions,
    build_filters,
    _resolve_user_placeholders,
    INTENT_LABELS,
)
from session_store import sessions
from models import Intent, WooAPICall
from classifier import classify
from api_builder import build_api_calls
from conversation_flow import (
    FlowState,
    handle_flow_state,
    should_disambiguate,
    get_disambiguation_message,
)
from chat_logger import get_logger, sanitize_log_string
from llm_fallback import llm_fallback, llm_retry_search
from store_registry import get_store_loader

logger = get_logger("miraq_chat")

chat_bp = Blueprint("chat", __name__)


# Helper functions imported from other modules (see imports above)


@chat_bp.route("/chat", methods=["POST"])
def chat():
    """
    Main chat endpoint.

    Request:
        POST /chat
        {
            "message": "show affogato chip card",
            "session_id": "session_xxx",
            "user_context": {
                "customer_id": 130,
                "email": "user@example.com"
            }
        }

    Response:
        {
            "success": true,
            "bot_message": "...",
            "intent": "search",
            "products": [...],
            "filters_applied": {...},
            "suggestions": [...],
            "session_id": "...",
            "metadata": {...}
        }
    """
    start_time = time.time()

    # ─── Parse request ───
    body = request.get_json(silent=True)
    if not body:
        logger.warning("POST /chat | Invalid JSON body")
        return jsonify({
            "success": False,
            "bot_message": "Invalid request. Send JSON with 'message' field.",
            "intent": "error",
            "products": [],
            "filters_applied": {},
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": "",
            "metadata": {"error": "Invalid JSON body"},
        }), 400

    message = body.get("message", "").strip()
    session_id = body.get("session_id", "")
    user_context = body.get("user_context", {})
    
    # Log incoming request (sanitize user input to prevent log injection)
    truncated_msg = message[:100] + "..." if len(message) > 100 else message
    sanitized_msg = sanitize_log_string(truncated_msg)
    customer_id = user_context.get("customer_id")
    flow_state = user_context.get("flow_state", "idle")
    logger.info(f'POST /chat | session={session_id} | message="{sanitized_msg}" | customer_id={customer_id} | flow_state={flow_state}')

    if not message:
        logger.warning(f"POST /chat | session={session_id} | Empty message")
        return jsonify({
            "success": False,
            "bot_message": "Please type a message! Try asking about our tiles, categories, or products.",
            "intent": "error",
            "products": [],
            "filters_applied": {},
            "suggestions": [
                "Show me all products",
                "What categories do you have?",
                "Show me marble look tiles",
                "Quick ship tiles",
            ],
            "session_id": session_id,
            "metadata": {"error": "Empty message"},
        }), 400

    # ─── Update session ───
    if session_id:
        if session_id not in sessions:
            sessions[session_id] = {
                "history": [],
                "user_context": user_context,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        sessions[session_id]["history"].append({
            "role": "user",
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ─── Step 0: Check conversation flow state ───
    flow_state_str = user_context.get("flow_state", "idle")
    try:
        current_flow_state = FlowState(flow_state_str)
    except ValueError:
        current_flow_state = FlowState.IDLE
    
    logger.info(f"Step 0: Flow state={current_flow_state.value}")

    # Build context dict from user_context for the flow handler
    flow_context = {
        "pending_product_name": user_context.get("pending_product_name"),
        "pending_product_id": user_context.get("pending_product_id"),
        "pending_quantity": user_context.get("pending_quantity"),
    }

    # If we're in a multi-turn flow, let the flow handler try first
    if current_flow_state != FlowState.IDLE:
        flow_result = handle_flow_state(
            state=current_flow_state,
            message=message,
            entities=flow_context,
            confidence=0.0,
        )
        if flow_result and not flow_result.get("pass_through"):
            # Flow handler consumed the message — return immediately
            logger.info(f"Step 0: Flow handler consumed message | new_state={flow_result.get('flow_state', 'idle')}")
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": flow_result["bot_message"],
                "intent": "guided_flow",
                "products": [],
                "filters_applied": {},
                "suggestions": flow_result.get("suggestions", []),
                "session_id": session_id,
                "metadata": {
                    "flow_state": flow_result.get("flow_state", "idle"),
                    "response_time_ms": round((time.time() - start_time) * 1000),
                    "provider": "conversation_flow",
                },
                "flow_state": flow_result.get("flow_state", "idle"),
            }), 200

        elif flow_result and flow_result.get("override_message"):
            # Flow wants to redirect to a different utterance
            message = flow_result["override_message"]

        elif flow_result and flow_result.get("create_order"):
            # Flow confirmed order — use pending context to create the order
            pending_product_id = user_context.get("pending_product_id")
            pending_product_name = user_context.get("pending_product_name", "")
            pending_quantity = user_context.get("pending_quantity", 1)
            
            if pending_product_id and customer_id:
                logger.info(f"Step 0: Order confirmed via flow | product_id={pending_product_id} | quantity={pending_quantity}")
                
                # Build the order directly
                order_call = WooAPICall(
                    method="POST",
                    endpoint=f"{WOO_BASE_URL}/orders",
                    params={},
                    body={
                        "status": "processing",
                        "customer_id": customer_id,
                        "payment_method": DEFAULT_PAYMENT_METHOD,
                        "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
                        "set_paid": False,
                        "line_items": [{"product_id": pending_product_id, "quantity": pending_quantity}],
                    },
                    description=f"Create order for '{pending_product_name}' (confirmed via flow)",
                )
                order_resp = woo_client.execute(order_call)
                
                if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
                    created_order = order_resp["data"]
                    order_number = created_order.get("number") or created_order.get("id", "N/A")
                    total = created_order.get("total", "0.00")
                    
                    # Use line_items total if order total is 0
                    if float(total) == 0.0 and created_order.get("line_items"):
                        line_total = sum(float(item.get("total", "0") or "0") for item in created_order["line_items"])
                        if line_total > 0:
                            total = str(line_total)
                    
                    product_name = pending_product_name or "your item"
                    if created_order.get("line_items"):
                        product_name = created_order["line_items"][0].get("name") or product_name
                    
                    # Get currency symbol from order response or default to $
                    currency_symbol = created_order.get("currency_symbol", "$")
                    
                    bot_message = (
                        f"✅ **Order #{order_number} placed successfully!**\n\n"
                        f"**Product:** {product_name}\n"
                        f"**Quantity:** {pending_quantity}\n"
                        f"**Total:** {currency_symbol}{float(total):.2f}\n"
                        f"**Payment Mode:** Cash on Delivery\n"
                        f"**Status:** Processing"
                    )
                    
                    elapsed = time.time() - start_time
                    return jsonify({
                        "success": True,
                        "bot_message": bot_message,
                        "intent": "order",
                        "products": [],
                        "filters_applied": {},
                        "suggestions": ["Show me more products", "Check my orders", "No, that's all"],
                        "session_id": session_id,
                        "metadata": {
                            "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                            "response_time_ms": round(elapsed * 1000),
                        },
                        "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                    }), 200
                else:
                    error_msg = str(order_resp.get('error', 'Unknown'))
                    logger.error(f"Step 0: Order creation failed | error={error_msg}")
                    return jsonify({
                        "success": True,
                        "bot_message": "Sorry, I couldn't place the order. Please try again.",
                        "intent": "order",
                        "products": [],
                        "filters_applied": {},
                        "suggestions": ["Try again", "Show me products"],
                        "session_id": session_id,
                        "metadata": {"flow_state": FlowState.IDLE.value},
                        "flow_state": FlowState.IDLE.value,
                    }), 200

    # ─── Step 1: Classify intent ───
    result = classify(message)
    intent = result.intent
    entities = result.entities
    confidence = result.confidence
    
    # Log classification result with key entities
    entity_summary = {
        k: v for k, v in {
            "product_name": entities.product_name,
            "category_name": entities.category_name,
            "product_id": entities.product_id,
            "order_item_name": entities.order_item_name,
            "quantity": entities.quantity,
            "tile_size": entities.tile_size,
            "sample_size": entities.sample_size,
            "finish": entities.finish,
            "color_tone": entities.color_tone,
            "visual": entities.visual,
            "origin": entities.origin,
            "application": entities.application,
            "thickness": entities.thickness,
            "attribute_slug": entities.attribute_slug,
        }.items() if v is not None
    }
    logger.info(f"Step 1: Classified intent={intent.value} | confidence={confidence:.2f} | entities={entity_summary}")

    # ─── Step 1.5: LLM Fallback / Disambiguation check ───
    # Trigger LLM fallback when:
    # 1. Intent is UNKNOWN
    # 2. Confidence is below threshold
    # 3. Search intent but missing both product_name AND category_id
    # 4. Order create intent but missing both order_item_name AND product_name
    
    should_try_llm = False
    llm_trigger_reason = None
    
    if intent.value == "unknown":
        should_try_llm = True
        llm_trigger_reason = "unknown_intent"
    elif should_disambiguate(intent.value, confidence):
        should_try_llm = True
        llm_trigger_reason = "low_confidence"
    elif intent == Intent.PRODUCT_SEARCH and entities.product_name is None and entities.category_id is None:
        should_try_llm = True
        llm_trigger_reason = "missing_entities"
    elif intent in ORDER_CREATE_INTENTS and entities.order_item_name is None and entities.product_name is None:
        should_try_llm = True
        llm_trigger_reason = "missing_entities"
    
    if should_try_llm and LLM_FALLBACK_ENABLED:
        # Try LLM fallback
        store_loader = get_store_loader()
        session_history = None
        if session_id and session_id in sessions:
            session_history = sessions[session_id].get("history", [])
        
        llm_result = llm_fallback(
            user_message=message,
            original_intent=intent.value,
            original_confidence=confidence,
            trigger_reason=llm_trigger_reason,
            session_id=session_id,
            store_loader=store_loader,
            session_history=session_history,
        )
        
        if llm_result.get("success"):
            fallback_type = llm_result.get("fallback_type")
            
            if fallback_type == "conversational":
                # LLM handled it as general Q&A - return response directly
                elapsed = time.time() - start_time
                llm_metadata = llm_result.get("metadata", {})
                llm_metadata["response_time_ms"] = round(elapsed * 1000)
                
                if session_id and session_id in sessions:
                    sessions[session_id]["history"].append({
                        "role": "bot",
                        "message": llm_result["bot_message"],
                        "intent": "conversational",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                
                return jsonify({
                    "success": True,
                    "bot_message": llm_result["bot_message"],
                    "intent": "conversational",
                    "products": [],
                    "filters_applied": {},
                    "suggestions": [],
                    "session_id": session_id,
                    "metadata": llm_metadata,
                }), 200
            
            elif fallback_type in ["intent_resolved", "entity_extracted"]:
                # LLM resolved the intent or extracted entities
                # Re-inject into pipeline at Step 2 by rebuilding the ClassifiedResult
                from models import ClassifiedResult, ExtractedEntities
                
                # Convert LLM entities to ExtractedEntities object
                llm_entities_dict = llm_result.get("entities", {})
                new_entities = ExtractedEntities()
                
                # Map LLM entity fields to ExtractedEntities fields
                entity_field_map = {
                    "product_name": "product_name",
                    "category_name": "category_name",
                    "finish": "finish",
                    "color_tone": "color_tone",
                    "tile_size": "tile_size",
                    "application": "application",
                    "visual": "visual",
                }
                
                for llm_field, entity_field in entity_field_map.items():
                    if llm_field in llm_entities_dict:
                        setattr(new_entities, entity_field, llm_entities_dict[llm_field])
                
                # Merge with original entities if entity_extracted
                if fallback_type == "entity_extracted":
                    # Keep original entities and merge with new ones
                    for entity_field in entity_field_map.values():
                        new_val = getattr(new_entities, entity_field)
                        orig_val = getattr(entities, entity_field)
                        if new_val is None and orig_val is not None:
                            setattr(new_entities, entity_field, orig_val)
                
                # Map intent string to Intent enum
                llm_intent_str = llm_result.get("intent", "unknown")
                try:
                    new_intent = Intent(llm_intent_str)
                except ValueError:
                    # If LLM returned invalid intent, try to map common variations
                    intent_mapping = {
                        "search": Intent.PRODUCT_SEARCH,
                        "browse": Intent.CATEGORY_BROWSE,
                        "filter": Intent.PRODUCT_LIST,
                        "order": Intent.QUICK_ORDER,
                    }
                    new_intent = intent_mapping.get(llm_intent_str, Intent.PRODUCT_LIST)
                
                # Update intent, entities, and confidence
                intent = new_intent
                entities = new_entities
                confidence = llm_result.get("confidence", 0.70)
                
                # Create new ClassifiedResult for API builder
                result = ClassifiedResult(
                    intent=intent,
                    entities=entities,
                    confidence=confidence,
                )
                
                logger.info(
                    f"Step 1.5: LLM fallback applied | new_intent={intent.value} | "
                    f"new_confidence={confidence:.2f} | fallback_type={fallback_type}"
                )
                
                # Continue to Step 2 with updated intent/entities
                # (fall through to rest of pipeline)
            else:
                # Unknown fallback type - treat as failure
                llm_result["success"] = False
        
        # If LLM failed or returned error, fall back to disambiguation menu
        if not llm_result.get("success"):
            disambig = get_disambiguation_message()
            elapsed = time.time() - start_time
            logger.info(f"Step 1.5: LLM failed, returning disambiguation | confidence={confidence:.2f}")
            return jsonify({
                "success": True,
                "bot_message": disambig["bot_message"],
                "intent": "disambiguation",
                "products": [],
                "filters_applied": {},
                "suggestions": disambig["suggestions"],
                "session_id": session_id,
                "metadata": {
                    "flow_state": disambig["flow_state"],
                    "confidence": round(confidence, 2),
                    "original_intent": intent.value,
                    "response_time_ms": round((time.time() - start_time) * 1000),
                    "provider": "conversation_flow",
                    "llm_error": llm_result.get("error", "LLM fallback failed"),
                },
                "flow_state": disambig["flow_state"],
            }), 200
    
    elif should_try_llm and not LLM_FALLBACK_ENABLED:
        # LLM is disabled, use old disambiguation menu
        disambig = get_disambiguation_message()
        elapsed = time.time() - start_time
        logger.info(f"Step 1.5: Low confidence, returning disambiguation (LLM disabled) | confidence={confidence:.2f}")
        return jsonify({
            "success": True,
            "bot_message": disambig["bot_message"],
            "intent": "disambiguation",
            "products": [],
            "filters_applied": {},
            "suggestions": disambig["suggestions"],
            "session_id": session_id,
            "metadata": {
                "flow_state": disambig["flow_state"],
                "confidence": round(confidence, 2),
                "original_intent": intent.value,
                "response_time_ms": round((time.time() - start_time) * 1000),
                "provider": "conversation_flow",
            },
            "flow_state": disambig["flow_state"],
        }), 200

    # ─── Step 2: Build API calls ───
    api_calls = build_api_calls(result)
    logger.info(f"Step 2: Built {len(api_calls)} API call(s) | endpoints={[f'{c.method} {c.endpoint.split('/')[-1]}' for c in api_calls]}")

    # ─── Step 2.5: Resolve user context placeholders ───
    customer_id = user_context.get("customer_id")
    if customer_id:
        logger.info(f"Step 2.5: Resolved customer_id={customer_id}")
        _resolve_user_placeholders(api_calls, customer_id)

    # ─── Step 2.6: Extract last_product context (for "order this" resolution) ───
    # The frontend sends the last displayed product so vague order phrases
    # like "order this" / "buy it" resolve correctly without a product search.
    last_product_ctx = user_context.get("last_product")  # {id, name} or None
    
    if last_product_ctx and last_product_ctx.get("id"):
        logger.info(f"Step 2.6: last_product_ctx found: id={last_product_ctx.get('id')}, name=\"{sanitize_log_string(last_product_ctx.get('name', ''))}\"")
    else:
        logger.info("Step 2.6: No last_product_ctx")

    # ─── Step 3: Execute API calls ───
    all_products_raw = []
    order_data = []
    
    # BUG FIX: For order-create intents, skip POST /orders calls from api_builder
    # since Step 3.6 will handle order creation. This prevents duplicate orders.
    filtered_api_calls = []
    if intent in ORDER_CREATE_INTENTS:
        for call in api_calls:
            # Skip POST /orders calls - Step 3.6 will create the order
            if call.method == "POST" and "/orders" in call.endpoint:
                logger.info(f"Step 3: Skipping POST /orders call from api_builder (intent={intent.value}) - Step 3.6 will handle order creation")
                continue
            filtered_api_calls.append(call)
        api_calls_to_execute = filtered_api_calls
    else:
        api_calls_to_execute = api_calls
    
    api_responses = woo_client.execute_all(api_calls_to_execute)

    for resp in api_responses:
        if resp.get("success"):
            data = resp.get("data")
            # Handle custom API response format (has "products" key)
            if isinstance(data, dict) and "products" in data:
                if intent in ORDER_INTENTS:
                    order_data.extend(data["products"])
                else:
                    all_products_raw.extend(data["products"])
            elif isinstance(data, list):
                if intent in ORDER_INTENTS:
                    order_data.extend(data)
                else:
                    all_products_raw.extend(data)
            elif isinstance(data, dict):
                if intent in ORDER_INTENTS:
                    order_data.append(data)
                else:
                    all_products_raw.append(data)
        else:
            # Log API call failure (sanitize error message to prevent log injection)
            error_msg = sanitize_log_string(str(resp.get('error', 'Unknown')))
            logger.warning(f"Step 3: API call failed | error={error_msg}")
    
    logger.info(f"Step 3: API execution complete | all_products_raw count={len(all_products_raw)} | order_data count={len(order_data)}")

    # ─── Step 3.5: REORDER step 2 — create new order from last order's line_items ───
    if intent == Intent.REORDER and order_data:
        source_order = order_data[0]
        source_line_items = source_order.get("line_items", [])
        logger.info(f"Step 3.5: Reorder attempt | source_order_id={source_order.get('id')} | line_items_count={len(source_line_items)}")
        if source_line_items and customer_id:
            new_line_items = [
                {
                    "product_id": item["product_id"],
                    "quantity": item.get("quantity", 1),
                    **({"variation_id": item["variation_id"]} if item.get("variation_id") else {}),
                }
                for item in source_line_items
                if item.get("product_id")
            ]
            if new_line_items:
                reorder_call = WooAPICall(
                    method="POST",
                    endpoint=f"{WOO_BASE_URL}/orders",
                    params={},
                    body={
                        "status": "processing",
                        "customer_id": customer_id,
                        "payment_method": DEFAULT_PAYMENT_METHOD,
                        "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
                        "set_paid": False,
                        "line_items": new_line_items,
                    },
                    description="Create reorder from last order line items (COD, on-hold)",
                )
                reorder_resp = woo_client.execute(reorder_call)
                if reorder_resp.get("success") and isinstance(reorder_resp.get("data"), dict):
                    order_data.append(reorder_resp["data"])
                    new_order = reorder_resp["data"]
                    logger.info(f"Step 3.5: Reorder created successfully | order_id={new_order.get('id')} | order_number={new_order.get('number')}")
                else:
                    error_msg = sanitize_log_string(str(reorder_resp.get('error', 'Unknown')))
                    logger.warning(f"Step 3.5: Reorder failed | error={error_msg}")

    # ─── Step 3.6: QUICK_ORDER / ORDER_ITEM / PLACE_ORDER — create order from matched product ───
    if intent in (Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER) and customer_id and entities.quantity:
        # Resolve which product to order:
        # Priority 1 — product returned by the search API call this turn
        # Priority 2 — last_product sent by the frontend (handles "order this" / "buy it")
        _order_product_id = None
        _order_product_name = None

        if all_products_raw:
            _p = all_products_raw[0]
            _order_product_id = _p.get("id")
            _order_product_name = _p.get("name", str(_order_product_id))
            logger.info(f"Step 3.6: Using all_products_raw → product_id={_order_product_id}, product_name=\"{sanitize_log_string(_order_product_name)}\"")
        elif last_product_ctx and last_product_ctx.get("id"):
            _order_product_id = last_product_ctx["id"]
            _order_product_name = last_product_ctx.get("name", str(last_product_ctx["id"]))
            logger.info(f"Step 3.6: Using last_product_ctx → product_id={_order_product_id}, product_name=\"{sanitize_log_string(_order_product_name)}\"")
            # Inject a minimal product dict so bot message and response include the product info
            all_products_raw.append({
                "id": _order_product_id,
                "name": _order_product_name,
                "price": "",
                "regular_price": "",
                "sale_price": "",
                "slug": "",
                "sku": "",
                "permalink": "",
                "on_sale": False,
                "stock_status": "instock",
                "total_sales": 0,
                "description": "",
                "short_description": "",
                "images": [],
                "categories": [],
                "tags": [],
                "attributes": [],
                "variations": [],
                "type": "simple",
                "average_rating": "0.00",
                "rating_count": 0,
                "weight": "",
                "dimensions": {"length": "", "width": "", "height": ""},
            })
            logger.info(f"Step 3.6: Injected minimal product dict into all_products_raw (count={len(all_products_raw)})")
        else:
            logger.warning("Step 3.6: No product found to order (all_products_raw empty, no last_product_ctx)")

        if _order_product_id:
            logger.info(f"Step 3.6: Creating WooCommerce order | product_id={_order_product_id} | quantity={entities.quantity or 1} | customer_id={customer_id}")
            order_call = WooAPICall(
                method="POST",
                endpoint=f"{WOO_BASE_URL}/orders",
                params={},
                body={
                    "status": "processing",
                    "customer_id": customer_id,
                    "payment_method": DEFAULT_PAYMENT_METHOD,
                    "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
                    "set_paid": False,
                    "line_items": [{"product_id": _order_product_id, "quantity": entities.quantity or 1}],
                },
                description=f"Create order for product '{_order_product_name}' (COD, processing)",
            )
            order_resp = woo_client.execute(order_call)
            if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
                order_data.append(order_resp["data"])
                created_order = order_resp["data"]
                line_items_summary = [
                    f"{item.get('name', 'Unknown')} x{item.get('quantity', 1)}"
                    for item in created_order.get("line_items", [])
                ]
                logger.info(
                    f"Step 3.6: WooCommerce order created | order_id={created_order.get('id')} | "
                    f"order_number={created_order.get('number')} | total=${created_order.get('total', '0.00')} | "
                    f"line_items={line_items_summary}"
                )
            else:
                error_msg = sanitize_log_string(str(order_resp.get('error', 'Unknown')))
                logger.error(f"Step 3.6: WooCommerce order creation failed | error={error_msg}")
        else:
            logger.warning("Step 3.6: Skipped order creation (no product_id resolved)")

    # ─── Step 3.7: Variation product handling ───
    # When api_builder issued GET /products/{id} + GET /products/{id}/variations,
    # we need to separate, filter, and format them properly.
    VARIATION_INTENTS = {Intent.PRODUCT_SEARCH, Intent.PRODUCT_DETAIL, Intent.PRODUCT_VARIATIONS}

    if intent in VARIATION_INTENTS and entities.product_id:
        parent_product_raw = None
        variations_raw = []

        for resp in api_responses:
            if not resp.get("success"):
                continue
            data = resp.get("data")
            if isinstance(data, dict) and data.get("id") == entities.product_id:
                parent_product_raw = data
            elif isinstance(data, list) and data and data[0].get("parent_id") is not None:
                variations_raw = data

        if parent_product_raw:
            parent_formatted = format_product(parent_product_raw)
            has_attributes = any([
                entities.finish, entities.color_tone, entities.tile_size,
                entities.thickness, entities.visual, entities.origin,
            ])

            if variations_raw and has_attributes:
                # Filter variations to only those matching user's attributes
                filtered_vars = _filter_variations_by_entities(variations_raw, entities)
                variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]
                products = [parent_formatted] + variation_products
            elif variations_raw:
                # No attribute filter — return parent + all variations
                variation_products = [format_variation(v, parent_product_raw) for v in variations_raw]
                products = [parent_formatted] + variation_products
            else:
                # Simple product or variations call not made
                products = [parent_formatted]

            bot_message = generate_bot_message(intent, entities, products, confidence, order_data)
            suggestions = generate_suggestions(intent, entities, products)
            filters = build_filters(intent, entities, api_calls)
            elapsed = time.time() - start_time
            metadata = {
                "confidence": round(confidence, 2),
                "products_count": len(products),
                "provider": "wgc_intent_classifier",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "response_time_ms": round(elapsed * 1000),
                "intent_raw": intent.value,
                "entities": _entities_to_dict(entities),
                "variations_found": len(variations_raw),
                "variations_matched": len(products) - 1 if variations_raw else 0,
            }
            if session_id and session_id in sessions:
                sessions[session_id]["history"].append({
                    "role": "bot", "message": bot_message, "intent": intent.value,
                    "products_count": len(products),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            return jsonify({
                "success": True,
                "bot_message": bot_message,
                "intent": INTENT_LABELS.get(intent, "unknown"),
                "products": products,
                "filters_applied": filters,
                "suggestions": suggestions,
                "session_id": session_id,
                "metadata": metadata,
            }), 200

    # ─── Step 3.8: LLM Retry on Empty Search Results ───
    # When API returns 0 products for search/filter intents, try LLM for suggestions
    SEARCH_FILTER_INTENTS = {
        Intent.PRODUCT_SEARCH,
        Intent.PRODUCT_LIST,
        Intent.CATEGORY_BROWSE,
        Intent.FILTER_BY_FINISH,
        Intent.FILTER_BY_SIZE,
        Intent.FILTER_BY_COLOR,
        Intent.FILTER_BY_APPLICATION,
        Intent.PRODUCT_BY_VISUAL,
        Intent.PRODUCT_BY_ORIGIN,
    }
    
    if (
        intent in SEARCH_FILTER_INTENTS
        and len(all_products_raw) == 0
        and LLM_RETRY_ON_EMPTY_RESULTS
        and LLM_FALLBACK_ENABLED
    ):
        logger.info(f"Step 3.8: Empty search results, trying LLM retry | intent={intent.value}")
        
        store_loader = get_store_loader()
        entities_dict = {
            "product_name": entities.product_name,
            "category_name": entities.category_name,
            "finish": entities.finish,
            "color_tone": entities.color_tone,
            "tile_size": entities.tile_size,
            "application": entities.application,
            "visual": entities.visual,
        }
        
        llm_retry_result = llm_retry_search(
            user_message=message,
            original_intent=intent.value,
            entities=entities_dict,
            session_id=session_id,
            store_loader=store_loader,
        )
        
        if llm_retry_result.get("success"):
            retry_type = llm_retry_result.get("retry_type")
            
            if retry_type == "corrected_search" and llm_retry_result.get("corrected_term"):
                # LLM suggested a corrected search term - retry the search
                corrected_term = llm_retry_result["corrected_term"]
                logger.info(f"Step 3.8: LLM suggested correction | corrected_term={corrected_term}")
                
                # Re-classify with corrected term
                corrected_result = classify(corrected_term)
                
                # Rebuild API calls with corrected entities
                corrected_api_calls = build_api_calls(corrected_result)
                corrected_responses = woo_client.execute_all(corrected_api_calls)
                
                # Extract products from corrected search
                corrected_products_raw = []
                for resp in corrected_responses:
                    if resp.get("success"):
                        data = resp.get("data")
                        if isinstance(data, dict) and "products" in data:
                            corrected_products_raw.extend(data["products"])
                        elif isinstance(data, list):
                            corrected_products_raw.extend(data)
                        elif isinstance(data, dict):
                            corrected_products_raw.append(data)
                
                if corrected_products_raw:
                    # Success! Use corrected results
                    all_products_raw = corrected_products_raw
                    logger.info(f"Step 3.8: LLM retry successful | found {len(all_products_raw)} products")
                else:
                    # Still no results - use suggestion message
                    logger.info("Step 3.8: LLM retry still returned 0 products")
            
            # If we still have no products, use LLM suggestion message
            if len(all_products_raw) == 0 and llm_retry_result.get("suggestion_message"):
                suggestion_msg = llm_retry_result["suggestion_message"]
                elapsed = time.time() - start_time
                llm_metadata = llm_retry_result.get("metadata", {})
                llm_metadata["response_time_ms"] = round(elapsed * 1000)
                llm_metadata["original_intent"] = intent.value
                llm_metadata["confidence"] = round(confidence, 2)
                
                if session_id and session_id in sessions:
                    sessions[session_id]["history"].append({
                        "role": "bot",
                        "message": suggestion_msg,
                        "intent": intent.value,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                
                return jsonify({
                    "success": True,
                    "bot_message": suggestion_msg,
                    "intent": INTENT_LABELS.get(intent, "unknown"),
                    "products": [],
                    "filters_applied": {},
                    "suggestions": [],
                    "session_id": session_id,
                    "metadata": llm_metadata,
                }), 200

    # ─── Step 4: Format products ───
    products = []
    for p in all_products_raw:
        # Detect custom API format by checking for "featured_image" key
        if "featured_image" in p:
            products.append(format_custom_product(p))
        else:
            products.append(format_product(p))

    # Filter out private/draft products
    products = [p for p in products if p.get("name")]
    logger.info(f"Step 4: Formatted {len(products)} products")

    # ─── Step 5: Generate bot message ───
    bot_message = generate_bot_message(intent, entities, products, confidence, order_data)
    
    # Log product name and total for order intents (critical for debugging "your item" / $0.00 issues)
    if intent in ORDER_CREATE_INTENTS and order_data:
        placed_order = order_data[-1]
        # Extract product name used in bot message
        if products:
            used_product_name = products[0]["name"]
        elif placed_order.get("line_items"):
            used_product_name = placed_order["line_items"][0].get("name") or "your item"
        else:
            used_product_name = "your item"
        
        # Extract total with error handling
        total_str = placed_order.get("total", "0.00")
        try:
            total = float(total_str) if total_str else 0.0
        except (ValueError, TypeError):
            total = 0.0
            logger.warning(f"Step 5: Invalid total value '{total_str}', defaulting to 0.00")
        
        if total == 0.0 and placed_order.get("line_items"):
            try:
                line_total = sum(
                    float(item.get("total") or 0)
                    for item in placed_order["line_items"]
                )
                if line_total > 0:
                    total = line_total
                    logger.warning(f"Step 5: Order total was $0.00, used line_item total=${line_total:.2f} instead")
            except (ValueError, TypeError) as e:
                logger.warning(f"Step 5: Error calculating line_item total: {e}")
        
        logger.info(f"Step 5: Bot message generated | product_name=\"{sanitize_log_string(used_product_name)}\" | total=${total:.2f}")
        
        # Warn if fallback to "your item" was used
        if used_product_name == "your item":
            logger.warning("Step 5: Used fallback 'your item' - no product name available from products[] or line_items[]")
        
        # Warn if $0.00 total detected
        if total == 0.0:
            logger.warning("Step 5: Order total is $0.00 - possible pricing issue")

    # ─── Step 6: Generate suggestions ───
    suggestions = generate_suggestions(intent, entities, products)

    # ─── Step 7: Build filters ───
    filters = build_filters(intent, entities, api_calls)

    # ─── Step 8: Build metadata ───
    elapsed = time.time() - start_time
    metadata = {
        "confidence": round(confidence, 2),
        "products_count": len(products),
        "provider": "wgc_intent_classifier",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_time_ms": round(elapsed * 1000),
        "intent_raw": intent.value,
        "entities": _entities_to_dict(entities),
    }

    # ─── Step 9: Update session history ───
    if session_id and session_id in sessions:
        sessions[session_id]["history"].append({
            "role": "bot",
            "message": bot_message,
            "intent": intent.value,
            "products_count": len(products),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ─── Step 10: Build response ───
    response = {
        "success": True,
        "bot_message": bot_message,
        "intent": INTENT_LABELS.get(intent, "unknown"),
        "products": products,
        "filters_applied": filters,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": metadata,
    }

    # ─── Step 5.5: Detect when quantity is needed for ordering ───
    if intent in ORDER_CREATE_INTENTS and not entities.quantity and products:
        # We found the product but no quantity — ask
        product = products[0]
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": f"Sure, I can order **{product['name']}** for you! How many do you need? 🛒",
            "intent": INTENT_LABELS.get(intent, "order"),
            "products": products[:1],
            "filters_applied": {},
            "suggestions": ["1", "5", "10", "25"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_QUANTITY.value,
                "pending_product_name": product["name"],
                "pending_product_id": product.get("id"),
                "response_time_ms": round((time.time() - start_time) * 1000),
            },
            "flow_state": FlowState.AWAITING_QUANTITY.value,
        }), 200

    # ─── Step 10.5: After successful response, add "anything else?" flow ───
    # Append to the response dict before returning:
    if intent in ORDER_CREATE_INTENTS and order_data:
        response["flow_state"] = FlowState.AWAITING_ANYTHING_ELSE.value
    else:
        response["flow_state"] = FlowState.IDLE.value
    
    # ─── Step 10: Log final response summary ───
    logger.info(
        f"Step 10: Response sent | intent={INTENT_LABELS.get(intent, 'unknown')} | "
        f"products_count={len(products)} | response_time_ms={metadata['response_time_ms']} | "
        f"flow_state={response['flow_state']}"
    )
        
    return jsonify(response), 200
