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
    CUSTOM_API_BASE_URL
)

# From your api_builder.py (Line 12)
from config.settings import DEFAULT_PER_PAGE, DEFAULT_ORDER_PER_PAGE
from datetime import datetime, timezone
from models import Intent, WooAPICall
from woo_client import woo_client
from formatters import format_product
from response_generator import format_order_detail
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string
from handlers.chat_utils import (
    default_pagination,
    build_variant_prompt,
    fetch_shipping_address,
)

logger = get_logger("miraq_chat")

def handle_historical_search(intent, entities, order_data, customer_id, session_id, page, start_time, sessions):
    """Step 3.5c: Filter past orders by specific attributes or tags."""
    if intent != Intent.HISTORICAL_SEARCH:
        return None

    if not customer_id:
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": "I'd love to check your past orders! 🔍\n\nPlease log in to view your history.",
            "intent": intent.value,
            "products": [],
            "suggestions": ["Show all products"],
            "session_id": session_id,
            "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200

    # If they asked for a specific order ID, filter the order_data list!
    specific_order_id = getattr(entities, 'order_id', None)
    if specific_order_id:
        order_data = [o for o in order_data if str(o.get('id')) == str(specific_order_id) or str(o.get('number')) == str(specific_order_id)]
        
        if not order_data:
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": f"I couldn't find order #{specific_order_id} in your recent history. Please check the order number and try again!",
                "intent": intent.value,
                "products": [],
                "suggestions": ["Show my orders", "Browse products"],
                "session_id": session_id,
                "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
                "pagination": default_pagination(page),
                "flow_state": FlowState.IDLE.value,
            }), 200

    # Existing limit logic (only applies if they DIDN'T provide a specific order ID)
    limit = getattr(entities, 'order_count', None)
    if limit and not specific_order_id:
        order_data = order_data[:limit]

    past_product_ids = []
    ordered_variations = {}
    for o in order_data:
        for item in o.get("line_items", []):
            pid = item.get("product_id")
            vid = item.get("variation_id")
            if pid:
                past_product_ids.append(pid)
                if vid:
                    ordered_variations[pid] = vid

    if not past_product_ids:
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": "It looks like you don't have any past orders yet. Try searching for a specific style or color!",
            "intent": intent.value,
            "products": [],
            "suggestions": ["Browse categories", "Show all products"],
            "session_id": session_id,
            "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200

    from api_builder import _build_advanced_filter_call, _attr_slug_for_label
    
    # 1. Fetch ONLY the exact past purchases matching the criteria
    attr_filters = {
        _attr_slug_for_label(label): val 
        for label, val in entities.attributes.items() 
        if _attr_slug_for_label(label) and val
    }

    seed_call = _build_advanced_filter_call(
        attributes=attr_filters if attr_filters else None,
        tags=list(entities.tag_slugs) if entities.tag_slugs else None,
        or_pairs=list(entities.attr_tag_or_pairs) if entities.attr_tag_or_pairs else None,
        page=page, 
        per_page=DEFAULT_PER_PAGE,
        description="Filter past orders"
    )
    seed_call.body["ids"] = list(set(past_product_ids))
    seed_resp = woo_client.execute(seed_call)
    
    seed_products = []
    _sd = {}
    if seed_resp.get("success"):
        _sd = seed_resp.get("data", {})
        seed_products = _sd.get("products", []) if isinstance(_sd, dict) else (_sd if isinstance(_sd, list) else [])

    if not seed_products:
        filter_str = " ".join(entities.tag_slugs + list(entities.attributes.values())).replace("-", " ") or "that description"
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": f"I couldn't find any past purchases matching **{filter_str}**. Try searching our full catalog!",
            "intent": intent.value,
            "products": [],
            "suggestions": ["Show all products"],
            "session_id": session_id,
            "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200
        
    
    from formatters import format_product, format_custom_product
    formatted_products = []
    
    # 🚀 NEW: Create a list to hold the text descriptions of what they actually bought
    purchased_items_text = []

    for p in seed_products:
        if "featured_image" in p:
            fp = format_custom_product(p)
        else:
            fp = format_product(p)
            
        pid = p.get("id")
        ordered_vid = ordered_variations.get(pid)
        
        var_suffix = ""
        if ordered_vid and p.get("variations"):
            for var in p["variations"]:
                if var.get("id") == ordered_vid:
                    # Extract the variation attributes (e.g., "Charcoal / 12x24")
                    var_attrs = var.get("attributes", {})
                    if isinstance(var_attrs, dict):
                        var_suffix = " / ".join(str(v) for v in var_attrs.values() if v)
                    elif isinstance(var_attrs, list):
                        var_suffix = " / ".join(str(a.get("option", "")) for a in var_attrs if isinstance(a, dict) and a.get("option"))
                    break
        
        # 🚀 FIX: Add the specific variation to our text list, but leave the product card alone!
        product_name = fp.get('name', 'Product')
        if var_suffix:
            purchased_items_text.append(f"• {product_name} ({var_suffix})")
        else:
            purchased_items_text.append(f"• {product_name}")
            
        formatted_products.append(fp)
        
    formatted_products = [p for p in formatted_products if p.get("name")]

    # 🚀 FIX: Generate the bot message and append the list of specific variations!
    if specific_order_id:
        bot_message = f"Here are the products from order **#{specific_order_id}**! 📦\n\n"
    else:
        filter_str = " ".join(entities.tag_slugs + list(entities.attributes.values())).replace("-", " ") or "that description"
        bot_message = f"Here are your previous purchases matching **{filter_str}**! 🎯\n\n"
        
    if purchased_items_text:
        bot_message += "You specifically ordered:\n" + "\n".join(purchased_items_text) + "\n\n"

    from response_generator import generate_suggestions
    suggestions = generate_suggestions(intent, entities, formatted_products)
    
    total_pages_calc = _sd.get("pages", 1) if isinstance(_sd, dict) else 1
    
    pagination = {
        "page": page,
        "per_page": DEFAULT_PER_PAGE,
        "total_pages": total_pages_calc,
        "total_items": _sd.get("total", len(formatted_products)) if isinstance(_sd, dict) else len(formatted_products),
        "has_more": page < total_pages_calc
    }

    elapsed = time.time() - start_time
    logger.info(f"Step 10: Response sent | intent={intent.value} | products_count={len(formatted_products)} | response_time_ms={round(elapsed * 1000)} | flow_state=idle")

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        "products": formatted_products,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.IDLE.value, 
            "response_time_ms": round(elapsed * 1000)
        },
        "pagination": pagination,
        "flow_state": FlowState.IDLE.value,
    }), 200
    
def handle_reorder(intent, entities, order_data, customer_id, session_id, page, start_time, sessions):
    """Step 3.5: Create a new order from the last order's line items."""
    if intent != Intent.REORDER:
        return None

    # 🚀 THE INTERCEPT: If they didn't provide an order ID and didn't explicitly say "last order"
    if not entities.order_id and not getattr(entities, 'explicit_last_order', False):
        elapsed = time.time() - start_time
        bot_msg = "Which order would you like to reorder? 🔄\n\nPlease provide the order number (e.g., #12345), or simply say 'my last order'."
        
        if session_id and session_id in sessions:
            sessions[session_id]["history"].append({
                "role": "bot",
                "message": bot_msg,
                "intent": "guided_flow",
                "products_count": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            
        return jsonify({
            "success": True,
            "bot_message": bot_msg,
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["My last order", "Cancel"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_REORDER_ID.value,
                "response_time_ms": round(elapsed * 1000)
            },
            "flow_state": FlowState.AWAITING_REORDER_ID.value,
            "pagination": default_pagination(page)
        }), 200

    # ─── Standard Reorder Logic ───
    if not order_data:
        return None

    source_order = order_data[0]
    
    # Security check to ensure they own the order they are trying to reorder!
    if source_order.get("customer_id") != customer_id:
        logger.warning(f"Step 3.5: Reorder failed | Unauthorized access attempt for order #{source_order.get('id')}")
        return None

    source_line_items = source_order.get("line_items", [])
    logger.info(f"Step 3.5: Reorder attempt | source_order_id={source_order.get('id')} | line_items_count={len(source_line_items)}")

    if not (source_line_items and customer_id):
        return None

    # Check Stock Status Before Reordering!
    product_ids = [item["product_id"] for item in source_line_items if item.get("product_id")]
    
    if product_ids:
        stock_call = WooAPICall(
            method="POST",
            endpoint=f"{CUSTOM_API_BASE_URL}/products-advanced-new",
            params={},
            body={"ids": product_ids, "per_page": len(product_ids)},
            description="Check stock status for reorder items"
        )
        stock_resp = woo_client.execute(stock_call)
        
        out_of_stock_items = []
        if stock_resp.get("success"):
            data = stock_resp.get("data", {})
            current_products = data.get("products", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            
            # Map product AND variation IDs to their current live stock status
            stock_map = {}
            for p in current_products:
                stock_map[p["id"]] = p.get("stock_status", "instock")
                for var in p.get("variations", []):
                    stock_map[var["id"]] = var.get("stock_status", "instock")
                    
            # Check every item in the past order against the live stock map
            for item in source_line_items:
                check_id = item.get("variation_id") or item.get("product_id")
                if stock_map.get(check_id) == "outofstock":
                    out_of_stock_items.append(item.get("name", "An item"))
                    
        if out_of_stock_items:
            if start_time is None: 
                start_time = time.time()
                
            elapsed = time.time() - start_time
            oos_names = ", ".join(out_of_stock_items)
            
            # Block the order and notify the user!
            return jsonify({
                "success": True,
                "bot_message": f"I'm sorry, but I cannot duplicate this order because the following items are currently out of stock: **{oos_names}**.",
                "intent": intent.value,
                "products": [],
                "suggestions": ["Show my orders", "Browse products", "Cancel"],
                "session_id": session_id,
                "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
                "flow_state": FlowState.IDLE.value,
                "pagination": default_pagination(page)
            }), 200


    # ─── Standard Reorder Logic (Only executes if everything is in stock) ───
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
        return None

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
        description="Create reorder from last order line items",
    )
    reorder_resp = woo_client.execute(reorder_call)
    
    if reorder_resp.get("success") and isinstance(reorder_resp.get("data"), dict):
        new_order = reorder_resp["data"]
        order_data.append(new_order)
        logger.info(f"Step 3.5: Reorder created successfully | order_id={new_order.get('id')} | order_number={new_order.get('number')}")
    else:
        error_msg = sanitize_log_string(str(reorder_resp.get('error', 'Unknown')))
        logger.warning(f"Step 3.5: Reorder failed | error={error_msg}")
        
    return None # Fall through so chat.py formats the success/failure message!

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
    
    # ── OUT OF STOCK INTERCEPT ──
    if _order_product_raw and _order_product_raw.get("stock_status") == "outofstock":
        elapsed = time.time() - start_time
        from formatters import format_product
        return jsonify({
            "success": True,
            "bot_message": f"I'm so sorry, but **{_order_product_name}** is currently out of stock!",
            "intent": intent.value,
            "products": [format_product(_order_product_raw)],
            "suggestions": ["Show similar products", "Browse categories"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.IDLE.value,
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    _order_variation_id = entities.variation_id

    _order_variation_id = entities.variation_id
    _product_type = (_order_product_raw or {}).get("type", "simple")

    if _product_type == "variable":
        has_attrs = bool(entities.attributes)

        if not _order_variation_id and not has_attrs:
            logger.info(f"Step 3.6: Variable product with no variant info | product_id={_order_product_id}")
            prompt_msg = build_variant_prompt(_order_product_raw or {}, _order_product_name, getattr(entities, 'attributes', {}))

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
                "intent": intent.value,
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
                        prompt_msg = build_variant_prompt(_order_product_raw or {}, _order_product_name, getattr(entities, 'attributes', {}))
                    elapsed = time.time() - start_time
                    return jsonify({
                        "success": True,
                        "bot_message": prompt_msg,
                        "intent": intent.value,
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