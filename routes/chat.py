"""
Chat endpoint as a Flask Blueprint.
"""

import time
import uuid
from datetime import datetime, timezone
import os
from flask import Blueprint, request, jsonify

from app_config import (
    ORDER_INTENTS,
    ORDER_CREATE_INTENTS,
    CLASSIFIER_PROVIDER_TAG,
    get_currency_symbol,
)
from woo_client import woo_client
from formatters import format_product, format_custom_product, format_category, _entities_to_dict
from response_generator import generate_bot_message, generate_suggestions, _resolve_user_placeholders, INTENT_LABELS
from session_store import sessions, touch_session
from models import Intent, WooAPICall
from classifier import classify
from api_builder import build_api_calls
from conversation_flow import FlowState, handle_flow_state
from chat_logger import get_logger, sanitize_log_string
from store_registry import get_store_loader

from handlers.chat_utils import default_pagination, build_pagination, format_order_for_frontend
from handlers.flow_handler import handle_flow
from handlers.llm_handler import run_llm_fallback
from handlers.order_handler import handle_reorder, handle_order_detail, handle_quick_order
from handlers.variant_handler import handle_variant_selection, handle_variation_product, handle_quantity_and_variant_check
from handlers.search_handler import log_matched_products, handle_empty_results

logger = get_logger("miraq_chat")
chat_bp = Blueprint("chat", __name__)


@chat_bp.route("/chat", methods=["POST"])
def chat():
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
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": "",
            "metadata": {"error": "Invalid JSON body"},
            "pagination": default_pagination(),
        }), 400

    message = body.get("message", "").strip()
    session_id = body.get("session_id", "").strip()
    if not session_id:
        session_id = f"auto-{uuid.uuid4().hex}"
        logger.info(f"POST /chat | No session_id received — generated: {session_id}")

    user_context = body.get("user_context", {})
    page = int(body.get("page", 1))

    truncated_msg = message[:100] + "..." if len(message) > 100 else message
    sanitized_msg = sanitize_log_string(truncated_msg)
    customer_id = user_context.get("customer_id")
    flow_state = user_context.get("flow_state", "idle")
    logger.info(f'POST /chat | session={session_id} | message="{sanitized_msg}" | customer_id={customer_id} | flow_state={flow_state}')

    if not message:
        logger.warning(f"POST /chat | session={session_id} | Empty message")
        return jsonify({
            "success": False,
            "bot_message": "Please type a message! Try asking about our products, categories, or your orders.",
            "intent": "error",
            "products": [],
            "suggestions": ["Show me all products", "What categories do you have?"],
            "session_id": session_id,
            "metadata": {"error": "Empty message"},
            "pagination": default_pagination(page),
        }), 400

    # ─── Update session ───
    if session_id:
        if session_id not in sessions:
            sessions[session_id] = {
                "history": [],
                "user_context": user_context,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_active": datetime.now(timezone.utc).timestamp(),
            }
        sessions[session_id]["history"].append({
            "role": "user",
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        touch_session(session_id)

    # ─── Step 0.5: Suggestion retry intercept ───
    # When the user taps a filter_suggestion chip, the frontend sends the suggestion
    # params back as `suggestion_retry`. We bypass the classifier entirely and build
    # ExtractedEntities directly from the suggestion, then jump to Step 2.
    _suggestion_retry = body.get("suggestion_retry")
    if _suggestion_retry:
        from models import ExtractedEntities, ClassifiedResult
        from store_registry import get_store_loader as _get_loader

        _sr_label = _suggestion_retry.get("label", "suggestion retry")
        logger.info(
            f"Step 0.5: Suggestion retry | session={session_id} | label={_sr_label!r} | "
            f"tag_slugs={_suggestion_retry.get('tag_slugs')} | "
            f"category_slug={_suggestion_retry.get('category_slug')!r}"
        )

        _loader = _get_loader()
        _sr_entities = ExtractedEntities()

        # Resolve category slug -> name + id
        _cat_slug = _suggestion_retry.get("category_slug", "")
        if _cat_slug and _loader:
            _cat_entry = _loader.category_by_slug.get(_cat_slug)
            if _cat_entry:
                _sr_entities.category_name = _cat_entry["name"]
                _sr_entities.category_id = _cat_entry["id"]

        # Resolve extra category slugs -> extra_category_ids
        for _extra_slug in (_suggestion_retry.get("extra_category_slugs") or []):
            if _loader:
                _extra_entry = _loader.category_by_slug.get(_extra_slug)
                if _extra_entry:
                    _sr_entities.extra_category_ids.append(_extra_entry["id"])

        # Tag slugs -> tag_slugs + tag_ids
        for _tslug in (_suggestion_retry.get("tag_slugs") or []):
            _sr_entities.tag_slugs.append(_tslug)
            if _loader:
                _tid = _loader.get_tag_id_by_slug(_tslug)
                if _tid:
                    _sr_entities.tag_ids.append(_tid)

        # Attributes
        _sr_entities.attributes = dict(_suggestion_retry.get("attributes") or {})

        _sr_intent = Intent.FILTER_BY_ATTRIBUTE
        _sr_confidence = 1.0
        _sr_result = ClassifiedResult(
            intent=_sr_intent,
            entities=_sr_entities,
            confidence=_sr_confidence,
        )

        # Jump straight to Step 2 - build API calls from suggestion entities
        _sr_customer_id = user_context.get("customer_id")
        _sr_api_calls = build_api_calls(_sr_result, page, user_message=message, session_id=session_id, customer_id=_sr_customer_id)
        _sr_endpoint_summary = [f"{c.method} {c.endpoint.split('/')[-1]}" for c in _sr_api_calls]
        logger.info(
            f"Step 0.5: Built {len(_sr_api_calls)} API call(s) | "
            f"endpoints={_sr_endpoint_summary}"
        )

        if _sr_customer_id:
            _resolve_user_placeholders(_sr_api_calls, _sr_customer_id)

        _sr_responses = woo_client.execute_all(_sr_api_calls)
        _sr_products_raw = []
        for _r in _sr_responses:
            if _r.get("success"):
                _d = _r.get("data")
                if isinstance(_d, dict) and "products" in _d:
                    _sr_products_raw.extend(_d["products"])
                elif isinstance(_d, list):
                    _sr_products_raw.extend(_d)
                elif isinstance(_d, dict):
                    _sr_products_raw.append(_d)

        logger.info(f"Step 0.5: Suggestion retry returned {len(_sr_products_raw)} products")

        _sr_formatted = []
        for _p in _sr_products_raw:
            if _p.get("parent_id"):
                continue
            if "featured_image" in _p:
                _sr_formatted.append(format_custom_product(_p))
            else:
                _sr_formatted.append(format_product(_p))
        _sr_formatted = [p for p in _sr_formatted if p.get("name")]

        _sr_bot_message = (
            f"Here are results for **{_sr_label}**:"
            if _sr_formatted
            else f"No products found for **{_sr_label}** either. Try a different filter."
        )
        _sr_pagination = build_pagination(page, _sr_responses, _sr_api_calls)
        elapsed = time.time() - start_time

        if session_id and session_id in sessions:
            sessions[session_id]["history"].append({
                "role": "bot",
                "message": _sr_bot_message,
                "intent": "suggestion_retry",
                "products_count": len(_sr_formatted),
            })

        return jsonify({
            "success": True,
            "bot_message": _sr_bot_message,
            "intent": "filter",
            "products": _sr_formatted,
            "suggestions": [],
            "filter_suggestions": [],
            "session_id": session_id,
            "metadata": {
                "confidence": 1.0,
                "products_count": len(_sr_formatted),
                "provider": CLASSIFIER_PROVIDER_TAG,
                "response_time_ms": round(elapsed * 1000),
                "intent_raw": "suggestion_retry",
                "suggestion_label": _sr_label,
            },
            "pagination": _sr_pagination,
            "flow_state": FlowState.IDLE.value,
        }), 200

    # ─── Step 0: Check conversation flow state ───
    flow_state_str = user_context.get("flow_state", "idle")
    try:
        current_flow_state = FlowState(flow_state_str)
    except ValueError:
        current_flow_state = FlowState.IDLE
    logger.info(f"Step 0: Flow state={current_flow_state.value}")

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
            state=current_flow_state,
            message=message,
            entities=flow_context,
            confidence=0.0,
        )
        if flow_result and flow_result.get("override_message"):
            message = flow_result["override_message"]

    # Delegate all flow-state branches to flow_handler
    if flow_result and not flow_result.get("pass_through") and not flow_result.get("override_message"):
        resp = handle_flow(flow_result, user_context, session_id, customer_id, page, start_time, sessions)
        if resp:
            return resp

    # Check for flow-triggered actions (create_order, fetch_customer_address, fetch_price_summary)
    if flow_result:
        resp = handle_flow(flow_result, user_context, session_id, customer_id, page, start_time, sessions)
        if resp:
            return resp

    # Capture resolve_variant flag
    _resolve_variant = bool(flow_result and flow_result.get("resolve_variant"))

    # ─── Steps 1–3: Classify + API execution (skipped in variant resolution mode) ───
    if _resolve_variant:
        from models import ExtractedEntities
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        confidence = 1.0
        result = None
        api_calls = []
        api_calls_to_execute = []
        api_responses = []
        all_products_raw = []
        order_data = []
        customer_id = user_context.get("customer_id")
        last_product_ctx = None
        logger.info("Steps 1-3: Skipped (variant resolution mode — Step 3.55 will handle)")
    else:
        # ─── Step 1: Classify intent ───
        result = classify(message)
        intent = result.intent
        entities = result.entities
        confidence = result.confidence

        entity_summary = {
            k: v for k, v in {
                "product_name": entities.product_name,
                "category_name": entities.category_name,
                "product_id": entities.product_id,
                "order_item_name": entities.order_item_name,
                "quantity": entities.quantity,
                "attributes": entities.attributes or None,
                "tag_slugs": entities.tag_slugs or None,  # ← ADD THIS
            }.items() if v is not None
        }
        logger.info(f"Step 1: Classified intent={intent.value} | confidence={confidence:.2f} | entities={entity_summary}")

        # ─── Step 1.5: LLM Fallback ───
        store_loader = get_store_loader()
        session_history = sessions.get(session_id, {}).get("history") if session_id else None

        llm_outcome = run_llm_fallback(
            message=message,
            intent=intent,
            entities=entities,
            confidence=confidence,
            session_id=session_id,
            session_history=session_history,
            store_loader=store_loader,
            page=page,
            start_time=start_time,
            order_create_intents=ORDER_CREATE_INTENTS,
            user_context=user_context,
            sessions=sessions,
        )

        if llm_outcome is not None:
            # Flask response — return directly (conversational or disambiguation)
            if not isinstance(llm_outcome, tuple) or not isinstance(llm_outcome[0], tuple):
                if hasattr(llm_outcome, 'get_data'):  # it's a Response object
                    return llm_outcome
                if isinstance(llm_outcome, tuple) and len(llm_outcome) == 2 and isinstance(llm_outcome[1], int):
                    return llm_outcome
            # (intent, entities, confidence, result) tuple — updated classification
            if isinstance(llm_outcome, tuple) and len(llm_outcome) == 4:
                intent, entities, confidence, result = llm_outcome

        # ─── Step 2: Build API calls ───
        # Resolve customer_id before build so UPDATE_CUSTOMER gets the correct ID
        customer_id = user_context.get("customer_id")
        api_calls = build_api_calls(result, page, user_message=message, session_id=session_id, customer_id=customer_id)
        endpoint_summary = [f"{c.method} {c.endpoint.split('/')[-1]}" for c in api_calls]
        logger.info(f"Step 2: Built {len(api_calls)} API call(s) | endpoints={endpoint_summary}")

        if customer_id:
            logger.info(f"Step 2.5: Resolved customer_id={customer_id}")
            _resolve_user_placeholders(api_calls, customer_id)

        last_product_ctx = user_context.get("last_product")
        if last_product_ctx and last_product_ctx.get("id"):
            logger.info(f"Step 2.6: last_product_ctx found: id={last_product_ctx.get('id')}, name=\"{sanitize_log_string(last_product_ctx.get('name', ''))}\"")
        else:
            logger.info("Step 2.6: No last_product_ctx")
            
        # ─── Step 2.7: OFFLINE / DRY RUN INTERCEPT ───
        # Stop here and return the constructed parameters instead of calling WooCommerce
        if body.get("dry_run") or os.getenv("DRY_RUN", "false").lower() == "true":
            elapsed = time.time() - start_time
            logger.info(f"Step 2.7: Dry run return | intent={intent.value}")
            return jsonify({
                "success": True,
                "bot_message": "Offline Mode: API calls built using local JSON data.",
                "intent": intent.value,
                "extracted_entities": _entities_to_dict(entities),
                "constructed_api_calls": [
                    {
                        "description": call.description,
                        "method": call.method,
                        "endpoint": call.endpoint.split('/')[-1], # Shortened for easy reading
                        "params": call.params,
                        "body": call.body
                    } for call in api_calls
                ],
                "metadata": {
                    "confidence": round(confidence, 2),
                    "response_time_ms": round(elapsed * 1000),
                    "data_source": "local_json_files"
                }
            }), 200

        # ─── Step 3: Execute API calls ───
        all_products_raw = []
        order_data = []

        filtered_api_calls = []
        if intent in ORDER_CREATE_INTENTS:
            for call in api_calls:
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
                if isinstance(data, dict) and "products" in data:
                    (order_data if intent in ORDER_INTENTS else all_products_raw).extend(data["products"])
                elif isinstance(data, list):
                    (order_data if intent in ORDER_INTENTS else all_products_raw).extend(data)
                elif isinstance(data, dict):
                    (order_data if intent in ORDER_INTENTS else all_products_raw).append(data)
            else:
                error_msg = sanitize_log_string(str(resp.get('error', 'Unknown')))
                logger.warning(f"Step 3: API call failed | error={error_msg}")

        logger.info(f"Step 3: API execution complete | all_products_raw count={len(all_products_raw)} | order_data count={len(order_data)}")

        # ─── Step 3.1: Debug — log matched product context ───
        log_matched_products(all_products_raw, api_calls_to_execute)
    
    # After Step 3.1
    if intent == Intent.FETCH_CUSTOMER:
        elapsed = int((time.time() - start_time) * 1000)
        customer_raw = order_data[0] if order_data else {}
        
        # Pull requested fields from the API response
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
        bot_message = "Here's what I have on file:\n" + "\n".join(lines)
        
        return jsonify({
            "success": True,
            "bot_message": bot_message,
            "intent": "fetch_customer",
            "products": [],
            "suggestions": [],
            "session_id": session_id,
            "metadata": {"confidence": round(confidence, 2), "response_time_ms": elapsed},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200

    # ─── Step 3.2: Customer update response ───
    if intent == Intent.UPDATE_CUSTOMER:
        elapsed = int((time.time() - start_time) * 1000)
        # Find the PUT /customers response specifically — don't trust a fallback GET
        update_success = False
        for _api_call, _api_resp in zip(api_calls_to_execute, api_responses):
            if _api_call.method == "PUT" and "/customers/" in _api_call.endpoint:
                update_success = _api_resp.get("success", False)
                break
        _update_signal = [{"success": update_success}]
        bot_message = generate_bot_message(intent, entities, [], confidence, _update_signal)
        suggestions = generate_suggestions(intent, entities, [])
        metadata = {
            "confidence":       round(confidence, 2),
            "products_count":   0,
            "provider":         CLASSIFIER_PROVIDER_TAG,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "response_time_ms": elapsed,
            "intent_raw":       intent.value,
            "entities":         _entities_to_dict(entities),
        }
        logger.info(f"Step 10: Response sent | intent=update_customer | success={update_success} | response_time_ms={elapsed} | flow_state=idle")
        return jsonify({
            "success":     update_success,
            "bot_message": bot_message,
            "intent":      INTENT_LABELS.get(intent, "update_customer"),
            "products":    [],
            "suggestions": suggestions,
            "session_id":  session_id,
            "metadata":    metadata,
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
        }), 200

    # ─── Step 3.5: Reorder ───
    handle_reorder(intent, order_data, customer_id, session_id)

    # ─── Step 3.5b: Order detail ───
    resp = handle_order_detail(current_flow_state, customer_id, user_context, session_id, page, start_time)
    if resp:
        return resp

    # ─── Step 3.55: Variant selection ───
    resp = handle_variant_selection(
        current_flow_state, intent, entities, message, customer_id,
        session_id, page, start_time, sessions, user_context, _resolve_variant,
    )
    if resp:
        return resp

    # ─── Step 3.6: Quick order ───
    resp = handle_quick_order(
        intent, entities, all_products_raw, last_product_ctx,
        customer_id, session_id, page, start_time, sessions, ORDER_CREATE_INTENTS,
    )
    if resp:
        return resp

    # ─── Step 3.7: Variation product handling ───
    resp = handle_variation_product(
        intent, entities, api_responses, api_calls_to_execute,
        confidence, order_data, session_id, page, start_time, sessions,
    )
    if resp:
        return resp

    # ─── Step 3.8: Empty result handling ───
    store_loader = get_store_loader()
    all_products_raw, resp = handle_empty_results(
        intent, entities, all_products_raw, message,
        session_id, page, start_time, confidence, sessions, store_loader,
    )
    if resp:
        return resp

    # ─── Step 4: Format products ───
    products = []
    if intent == Intent.CATEGORY_LIST:
        seen_names = set()
        for cat in all_products_raw:
            name = cat.get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                products.append(format_category(cat))
    else:
        for p in all_products_raw:
            if p.get("parent_id"):
                continue
            # Custom API returns attributes as a dict: {"pa_colors": [...]}
            # Standard WC returns attributes as a list: [{"name": "Colors", "options": [...]}]
            if isinstance(p.get("attributes"), dict):
                products.append(format_custom_product(p))
            else:
                products.append(format_product(p))
    
    products = [p for p in products if p.get("name")]
    logger.info(f"Step 4: Formatted {len(products)} products")

    # ─── Step 5: Generate bot message ───
    _pagination_data = build_pagination(page, api_responses, api_calls_to_execute)
    _total_items_for_msg = _pagination_data.get("total_items")
    bot_message = generate_bot_message(intent, entities, products, confidence, order_data, total_items=_total_items_for_msg)

    if intent in ORDER_CREATE_INTENTS and order_data:
        placed_order = order_data[-1]
        used_product_name = (
            products[0]["name"] if products
            else (placed_order["line_items"][0].get("name") or "your item") if placed_order.get("line_items")
            else "your item"
        )
        total_str = placed_order.get("total", "0.00")
        try:
            total = float(total_str) if total_str else 0.0
        except (ValueError, TypeError):
            total = 0.0
            logger.warning(f"Step 5: Invalid total value '{total_str}', defaulting to 0.00")

        if total == 0.0 and placed_order.get("line_items"):
            try:
                line_total = sum(float(item.get("total") or 0) for item in placed_order["line_items"])
                if line_total > 0:
                    total = line_total
                    logger.warning(f"Step 5: Order total was {get_currency_symbol()}0.00, used line_item total={get_currency_symbol()}{line_total:.2f} instead")
            except (ValueError, TypeError) as e:
                logger.warning(f"Step 5: Error calculating line_item total: {e}")

        logger.info(f"Step 5: Bot message generated | product_name=\"{sanitize_log_string(used_product_name)}\" | total={get_currency_symbol()}{total:.2f}")
        if used_product_name == "your item":
            logger.warning("Step 5: Used fallback 'your item' - no product name available")
        if total == 0.0:
            logger.warning(f"Step 5: Order total is {get_currency_symbol()}0.00 - possible pricing issue")

    # ─── Step 5.5: Quantity / variant still needed? ───
    resp = handle_quantity_and_variant_check(
        intent, entities, all_products_raw, order_data,
        ORDER_CREATE_INTENTS, session_id, page, start_time, sessions,
    )
    if resp:
        return resp

    # ─── Step 6: Generate suggestions ───
    suggestions = generate_suggestions(intent, entities, products)

    # ─── Step 8: Build metadata ───
    elapsed = time.time() - start_time
    metadata = {
        "confidence": round(confidence, 2),
        "products_count": len(products),
        "provider": CLASSIFIER_PROVIDER_TAG,
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

    # ─── Step 10: Build and return response ───
    response = {
        "success": True,
        "bot_message": bot_message,
        "intent": INTENT_LABELS.get(intent, "unknown"),
        "products": products,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": metadata,
        "pagination": _pagination_data,
    }

    # Attach structured order data and pagination for ORDER_HISTORY / LAST_ORDER
    if intent in (Intent.ORDER_HISTORY, Intent.LAST_ORDER) and order_data:
        response["orders"] = [format_order_for_frontend(o) for o in order_data]
        response["order_pagination"] = build_pagination(page, api_responses, api_calls_to_execute)

    # Set flow state
    response["flow_state"] = (
        FlowState.AWAITING_ANYTHING_ELSE.value
        if intent in ORDER_CREATE_INTENTS and order_data
        else FlowState.IDLE.value
    )

    logger.info(
        f"Step 10: Response sent | intent={INTENT_LABELS.get(intent, 'unknown')} | "
        f"products_count={len(products)} | response_time_ms={metadata['response_time_ms']} | "
        f"flow_state={response['flow_state']}"
    )

    return jsonify(response), 200