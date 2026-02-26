"""
handlers/order_handler.py — Steps 3.5, 3.5b, 3.6: Order handling.

Step 3.5  — REORDER: create new order from last order's line items.
Step 3.5b — AWAITING_ORDER_DETAIL: fetch and display a specific order.
Step 3.6  — QUICK_ORDER/ORDER_ITEM/PLACE_ORDER: create order from matched product.

Each public function returns a Flask response or None to fall through.
"""

import time

from flask import jsonify

from app_config import (
    WOO_BASE_URL,
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
)
from models import Intent, WooAPICall
from woo_client import woo_client
from formatters import format_product
from response_generator import INTENT_LABELS, format_order_detail
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string
from handlers.chat_utils import (
    default_pagination,
    build_variant_prompt,
    fetch_shipping_address,
)

logger = get_logger("miraq_chat")


def handle_reorder(intent, order_data, customer_id, session_id):
    """Step 3.5: Create a new order from the last order's line items."""
    if not (intent == Intent.REORDER and order_data):
        return

    source_order = order_data[0]
    source_line_items = source_order.get("line_items", [])
    logger.info(f"Step 3.5: Reorder attempt | source_order_id={source_order.get('id')} | line_items_count={len(source_line_items)}")

    if not (source_line_items and customer_id):
        return

    new_line_items = [
        {
            "product_id": item["product_id"],
            "quantity": item.get("quantity", 1),
            **({"variation_id": item["variation_id"]} if item.get("variation_id") else {}),
        }
        for item in source_line_items
        if item.get("product_id")
    ]
    if not new_line_items:
        return

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
        new_order = reorder_resp["data"]
        order_data.append(new_order)
        logger.info(f"Step 3.5: Reorder created successfully | order_id={new_order.get('id')} | order_number={new_order.get('number')}")
    else:
        error_msg = sanitize_log_string(str(reorder_resp.get('error', 'Unknown')))
        logger.warning(f"Step 3.5: Reorder failed | error={error_msg}")


def handle_order_detail(current_flow_state, customer_id, user_context, session_id, page, start_time):
    """Step 3.5b: Fetch and display a specific order's details."""
    if not (current_flow_state == FlowState.AWAITING_ORDER_DETAIL and customer_id):
        return None

    _detail_order_id = user_context.get("pending_order_id")
    logger.info(f"Step 3.5b: Fetching order detail | order_id={_detail_order_id}")
    if not _detail_order_id:
        return None

    detail_call = WooAPICall(
        method="GET",
        endpoint=f"{WOO_BASE_URL}/orders/{_detail_order_id}",
        params={},
        description=f"Fetch order #{_detail_order_id} detail",
    )
    detail_resp = woo_client.execute(detail_call)
    elapsed = time.time() - start_time

    if detail_resp.get("success") and isinstance(detail_resp.get("data"), dict):
        bot_message = format_order_detail(detail_resp["data"])
        logger.info(f"Step 3.5b: Order detail fetched | order_id={_detail_order_id}")
    else:
        bot_message = f"Sorry, I couldn't find details for order #{_detail_order_id}. Please try again."
        logger.warning(f"Step 3.5b: Failed to fetch order detail | order_id={_detail_order_id}")

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "order_detail",
        "products": [],
        "suggestions": ["Show my orders", "Place a new order", "No, that's all"],
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
        "pagination": default_pagination(page),
    }), 200


def handle_quick_order(
    intent,
    entities,
    all_products_raw,
    last_product_ctx,
    customer_id,
    session_id,
    page,
    start_time,
    sessions,
    order_create_intents,
):
    """Step 3.6: Resolve product and proceed to shipping for QUICK_ORDER / ORDER_ITEM / PLACE_ORDER."""
    if not (intent in (Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER) and customer_id and entities.quantity):
        return None

    _order_product_id = None
    _order_product_name = None
    _order_product_raw = None

    _parent_products_raw = [p for p in all_products_raw if not p.get("parent_id")]
    _prefetched_variations = [p for p in all_products_raw if p.get("parent_id")]

    if _parent_products_raw:
        _p = _parent_products_raw[0]
        _order_product_id = _p.get("id")
        _order_product_name = _p.get("name", str(_order_product_id))
        _order_product_raw = _p
        logger.info(f"Step 3.6: Using all_products_raw → product_id={_order_product_id}, product_name=\"{sanitize_log_string(_order_product_name)}\"")
    elif last_product_ctx and last_product_ctx.get("id"):
        _order_product_id = last_product_ctx["id"]
        _order_product_name = last_product_ctx.get("name", str(last_product_ctx["id"]))
        logger.info(f"Step 3.6: Using last_product_ctx → product_id={_order_product_id}, product_name=\"{sanitize_log_string(_order_product_name)}\"")
        _injected = {
            "id": _order_product_id,
            "name": _order_product_name,
            "price": "", "regular_price": "", "sale_price": "",
            "slug": "", "sku": "", "permalink": "",
            "on_sale": False, "stock_status": "instock",
            "total_sales": 0, "description": "", "short_description": "",
            "images": [], "categories": [], "tags": [], "attributes": [],
            "variations": [], "type": "simple",
            "average_rating": "0.00", "rating_count": 0,
            "weight": "", "dimensions": {"length": "", "width": "", "height": ""},
        }
        all_products_raw.append(_injected)
        _order_product_raw = _injected
        logger.info(f"Step 3.6: Injected minimal product dict into all_products_raw (count={len(all_products_raw)})")
    else:
        logger.warning("Step 3.6: No product found to order (all_products_raw empty, no last_product_ctx)")

    if not _order_product_id:
        logger.warning("Step 3.6: Skipped order creation (no product_id resolved)")
        return None

    _order_variation_id = entities.variation_id
    _product_type = (_order_product_raw or {}).get("type", "simple")

    if _product_type == "variable":
        has_attrs = bool(entities.attributes)

        if not _order_variation_id and not has_attrs:
            logger.info(f"Step 3.6: Variable product with no variant info | product_id={_order_product_id}")
            prompt_msg = build_variant_prompt(_order_product_raw or {}, _order_product_name)

            if session_id and session_id in sessions:
                _pfv = _prefetched_variations or []
                sessions[session_id].setdefault("variation_cache", {})[str(_order_product_id)] = {
                    "variations": _pfv,
                    "parent_raw": _order_product_raw or {},
                }
                logger.info(f"Step 3.6: Cached {len(_pfv)} variations for product_id={_order_product_id} in session")

            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": prompt_msg,
                "intent": INTENT_LABELS.get(intent, "order"),
                "products": [format_product(_order_product_raw)] if _order_product_raw else [],
                "suggestions": [],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                    "pending_product_id": _order_product_id,
                    "pending_product_name": _order_product_name,
                    "pending_quantity": entities.quantity,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pagination": default_pagination(page),
            }), 200

        elif not _order_variation_id and has_attrs:
            logger.info(f"Step 3.6: Variable product with attributes, resolving variation | product_id={_order_product_id}")
            from formatters import _filter_variations_by_entities

            if _prefetched_variations:
                all_variations = _prefetched_variations
                logger.info(f"Step 3.6: Using {len(all_variations)} pre-fetched variations")
            else:
                var_call = WooAPICall(
                    method="GET",
                    endpoint=f"{WOO_BASE_URL}/products/{_order_product_id}/variations",
                    params={"per_page": 100, "status": "publish"},
                    description=f"Fetch variations for order resolution of '{_order_product_name}'",
                )
                var_resp = woo_client.execute(var_call)
                all_variations = var_resp.get("data", []) if var_resp.get("success") else []

            if all_variations:
                matched = _filter_variations_by_entities(all_variations, entities)
                if len(matched) == 1:
                    _order_variation_id = matched[0]["id"]
                    logger.info(f"Step 3.6: Resolved variation_id={_order_variation_id} from attributes")
                else:
                    logger.info(f"Step 3.6: Attributes matched {len(matched)} variations, asking user")
                    if len(matched) > 1 and len(matched) < len(all_variations):
                        variation_labels = [
                            " / ".join(a.get("option", "") for a in v.get("attributes", []) if a.get("option"))
                            for v in matched
                        ]
                        prompt_msg = (
                            f"I found **{len(matched)}** variants of **{_order_product_name}** matching your description:\n\n"
                            + "\n".join(f"• {lbl}" for lbl in variation_labels if lbl)
                            + "\n\nWhich one would you like?"
                        )
                    else:
                        prompt_msg = build_variant_prompt(_order_product_raw or {}, _order_product_name)
                    elapsed = time.time() - start_time
                    return jsonify({
                        "success": True,
                        "bot_message": prompt_msg,
                        "intent": INTENT_LABELS.get(intent, "order"),
                        "products": [format_product(_order_product_raw)] if _order_product_raw else [],
                        "suggestions": [],
                        "session_id": session_id,
                        "metadata": {
                            "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                            "pending_product_id": _order_product_id,
                            "pending_product_name": _order_product_name,
                            "pending_quantity": entities.quantity,
                            "response_time_ms": round(elapsed * 1000),
                        },
                        "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                        "pagination": default_pagination(page),
                    }), 200

    # Simple product or resolved variation — proceed to shipping
    logger.info(f"Step 3.6: Product resolved, proceeding to shipping | product_id={_order_product_id} | variation_id={_order_variation_id} | quantity={entities.quantity}")

    shipping_address = fetch_shipping_address(customer_id, "Step 3.6")
    has_address = bool(shipping_address and (shipping_address.get("address_1") or shipping_address.get("city")))

    base_meta = {
        "pending_product_id": _order_product_id,
        "pending_product_name": _order_product_name,
        "pending_quantity": entities.quantity,
        "pending_variation_id": _order_variation_id,
        "response_time_ms": round((time.time() - start_time) * 1000),
    }

    if has_address:
        addr_parts = [p for p in [
            shipping_address.get("address_1", ""), shipping_address.get("address_2", ""),
            shipping_address.get("city", ""), shipping_address.get("state", ""),
            shipping_address.get("postcode", ""), shipping_address.get("country", ""),
        ] if p]
        addr_display = ", ".join(addr_parts)
        return jsonify({
            "success": True,
            "bot_message": (
                f"Your shipping address on file:\n\n📦 **{addr_display}**\n\n"
                "Would you like to ship to this address, or use a different one?"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Yes, use this address", "Change address", "Cancel"],
            "session_id": session_id,
            "metadata": {**base_meta, "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value},
            "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
            "pagination": default_pagination(page),
        }), 200
    else:
        return jsonify({
            "success": True,
            "bot_message": "No shipping address is on file. Please type your shipping address (street, city, state, zip code):",
            "intent": "guided_flow",
            "products": [],
            "suggestions": [],
            "session_id": session_id,
            "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
            "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
            "pagination": default_pagination(page),
        }), 200