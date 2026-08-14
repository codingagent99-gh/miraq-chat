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
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
    DEFAULT_PER_PAGE,
    DEFAULT_ORDER_PER_PAGE
)

from models import Intent, WooAPICall
from woo_client import woo_client
from formatters import format_product
from response_generator import format_order_detail
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string
from handlers.chat_utils import (
    default_pagination,
    build_variant_prompt,
    _get_safe_options,
)
from ecommerce import endpoints

logger = get_logger("miraq_chat")

def handle_historical_search(intent, entities, order_data, customer_id, session_id, page, start_time):
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

    from api_builder import build_advanced_filter_call as _build_advanced_filter_call
    from api_builder.store_helpers import attr_slug_for_label as _attr_slug_for_label
    
    # 1. Fetch ONLY the exact past purchases matching the criteria
    attr_filters = {
        _attr_slug_for_label(label): val 
        for label, val in entities.attributes.items() 
        if _attr_slug_for_label(label) and val
    }

    seed_call = _build_advanced_filter_call(
        product_id=entities.product_id,
        search_term=entities.product_name if not entities.product_id else None,
        attributes=attr_filters if attr_filters else None,
        tags=list(entities.tag_slugs) if entities.tag_slugs else None,
        or_pairs=list(entities.attr_tag_or_pairs) if entities.attr_tag_or_pairs else None,
        page=page, 
        per_page=DEFAULT_PER_PAGE,
        description="Filter past orders"
    )
    
    # Safely intersect IDs instead of overwriting!
    existing_ids = seed_call.body.get("ids", [])
    if existing_ids:
        # User asked for a specific product ID
        valid_ids = list(set(existing_ids) & set(past_product_ids))
        if not valid_ids:
            elapsed = time.time() - start_time
            p_name = entities.product_name or "that product"
            return jsonify({
                "success": True,
                "bot_message": f"No, I don't see **{p_name}** in your recent order history. 😕",
                "intent": intent.value,
                "products": [],
                "suggestions": [f"Order {p_name}" if entities.product_name else "Browse products", "Show my orders"],
                "session_id": session_id,
                "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
                "pagination": default_pagination(page),
                "flow_state": FlowState.IDLE.value,
            }), 200
        seed_call.body["ids"] = valid_ids
    else:
        # No specific product ID requested, search within all past purchases
        seed_call.body["ids"] = list(set(past_product_ids))

    seed_resp = woo_client.execute(seed_call)
    
    seed_products = []
    _sd = {}
    if seed_resp.get("success"):
        _sd = seed_resp.get("data", {})
        seed_products = _sd.get("products", []) if isinstance(_sd, dict) else (_sd if isinstance(_sd, list) else [])

    if not seed_products:
        filter_parts = []
        if entities.product_name: filter_parts.append(entities.product_name)
        filter_parts.extend(entities.tag_slugs)
        filter_parts.extend(list(entities.attributes.values()))
        filter_str = " ".join(filter_parts).replace("-", " ") or "that description"
        
        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": f"No, I don't see any past purchases matching **{filter_str}**. 😕\n\nTry searching our full catalog!",
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
        
        product_name = fp.get('name', 'Product')
        if var_suffix:
            purchased_items_text.append(f"• {product_name} ({var_suffix})")
        else:
            purchased_items_text.append(f"• {product_name}")
            
        formatted_products.append(fp)
        
    formatted_products = [p for p in formatted_products if p.get("name")]

    _filtered_on_product = bool(entities.product_id or entities.product_name)
    _filtered_on_attrs = bool(
        entities.tag_slugs or entities.attributes or entities.attr_tag_or_pairs
    )

    # Dynamic Yes/No conversational response
    if specific_order_id:
        bot_message = f"Here are the products from order **#{specific_order_id}**! 📦\n\n"
    elif _filtered_on_product:
        p_name = entities.product_name or "that product"
        bot_message = f"Yes, you have ordered **{p_name}** before! Here are the details of your past purchase(s): 🎯\n\n"
    elif _filtered_on_attrs:
        filter_str = " ".join(entities.tag_slugs + list(entities.attributes.values())).replace("-", " ") or "that description"
        bot_message = f"Here are your previous purchases matching **{filter_str}**! 🎯\n\n"
    else:
        bot_message = "Here are your recent purchases: 🎯\n\n"
        
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

    # ── Product order history card ──────────────────────────────────────────
    # handle_historical_search returns its own response and bypasses
    # _build_final_response, which is where the rep-features block lives.
    # The SHOW_PRODUCT_RECENT_ORDERS action must therefore be built HERE.
    from app_config import CUSTOM_ORDER_ROLES, ORDER_REPORT_ADMIN_ROLES
    _actions = []
    _product_id = getattr(entities, "product_id", None)
    _role = ""
    try:
        from flask import request as _req
        _role = (_req.get_json(silent=True) or {}).get("user_context", {}).get("role", "")
    except Exception:
        pass
    _can_view = CUSTOM_ORDER_ROLES | ORDER_REPORT_ADMIN_ROLES
    if _product_id and _role in _can_view:
        try:
            from utils.rep_utils import fetch_product_order_history, format_product_orders_for_action
            _hist = fetch_product_order_history(_product_id, _role)
            if _hist:
                _actions.append({
                    "type": "SHOW_PRODUCT_RECENT_ORDERS",
                    "payload": {"orders": format_product_orders_for_action(_hist)},
                })
                logger.info(f"HISTORICAL_SEARCH | SHOW_PRODUCT_RECENT_ORDERS emitted with {len(_hist)} order(s)")
        except Exception as _e:
            logger.warning(f"HISTORICAL_SEARCH | order history card failed: {_e}")

    elapsed = time.time() - start_time
    logger.info(f"Step 10: Response sent | intent={intent.value} | products_count={len(formatted_products)} | response_time_ms={round(elapsed * 1000)} | flow_state=idle")

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        # Product cards are suppressed when showing order history — the user
        # asked for their past orders, not to browse the product. The order
        # items in the SHOW_PRODUCT_RECENT_ORDERS action already name the
        # product, so the card adds nothing and clutters the response.
        "products": [] if _actions else formatted_products,
        "actions": _actions,
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.IDLE.value, 
            "response_time_ms": round(elapsed * 1000)
        },
        "pagination": pagination,
        "flow_state": FlowState.IDLE.value,
    }), 200
    
def handle_reorder(intent, entities, order_data, customer_id, session_id, page, start_time):
    """Step 3.5: Create a new order from the last order's line items."""
    if intent != Intent.REORDER:
        return None

    # INTERCEPT: If they didn't provide an order ID and didn't explicitly say "last order"
    if not entities.order_id and not getattr(entities, 'explicit_last_order', False):
        elapsed = time.time() - start_time
        bot_msg = "Which order would you like to reorder? 🔄\n\nPlease provide the order number (e.g., #12345), or simply say 'my last order'."
        
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
    
    logger.debug(f"[DEBUG] source_order keys: {list(source_order.keys())}")
    logger.debug(f"[DEBUG] source_order shipping: {source_order.get('shipping')}")
    logger.debug(f"[DEBUG] source_order billing: {source_order.get('billing')}")
        
    # Security check to ensure they own the order they are trying to reorder!
    def _extract_id(val) -> str:
        return str(val).split("/")[-1] if val else ""

    if _extract_id(source_order.get("customer_id")) != _extract_id(customer_id):
        logger.warning(f"Step 3.5: Reorder failed | Unauthorized access attempt for order #{source_order.get('id')}")
        return None

    source_line_items = source_order.get("line_items", [])
    logger.info(f"Step 3.5: Reorder attempt | source_order_id={source_order.get('id')} | line_items_count={len(source_line_items)}")

    if not (source_line_items and customer_id):
        return None

    # Check Stock Status Before Reordering!
    product_ids = [item["product_id"] for item in source_line_items if item.get("product_id")]

    from app_config import ECOMMERCE_BACKEND

    out_of_stock_items = []

    if product_ids and ECOMMERCE_BACKEND == "shopify":
        # Shopify has no check_stock endpoint wired, but the store loader
        # already holds availableForSale for every variant in memory — so the
        # same protection the Woo path gets costs nothing here. Without this,
        # Shopify reorders of sold-out items went through silently.
        # Availability is keyed by variant where the line item has one
        # (variant-level stock is authoritative); otherwise any in-stock
        # variant keeps the product orderable.
        try:
            from store_registry import get_store_loader
            _loader = get_store_loader()
            _avail = {}
            for _p in (getattr(_loader, "products", None) or []):
                _p_gid = _p.get("_shopify_gid") or _p.get("id")
                _p_any = False
                for _v in (_p.get("variations") or []):
                    _v_gid = _v.get("_shopify_gid") or _v.get("id")
                    _v_in  = bool(_v.get("in_stock"))
                    if _v_gid:
                        _avail[str(_v_gid)] = _v_in
                    _p_any = _p_any or _v_in
                if _p_gid:
                    _avail[str(_p_gid)] = _p_any

            for item in source_line_items:
                _check_id = item.get("variation_id") or item.get("product_id")
                # Unknown ids (e.g. a product deleted since the order) are
                # NOT treated as out of stock — Shopify re-validates at
                # checkout, and blocking on a cache miss would be worse UX
                # than letting the order attempt proceed.
                if _avail.get(str(_check_id)) is False:
                    out_of_stock_items.append(item.get("name", "An item"))
        except Exception as _stock_exc:
            logger.warning(
                f"Step 3.5: Shopify availability check skipped | error={_stock_exc}"
            )

    if product_ids and ECOMMERCE_BACKEND != "shopify":
        stock_call = endpoints.check_stock(
            product_ids=product_ids,
            description="Check stock status for reorder items",
        )
        stock_resp = woo_client.execute(stock_call)

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
                    
    # Shared by both backends: Woo fills out_of_stock_items from the live
    # check_stock call above, Shopify from the in-memory availability map.
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

    from app_config import ECOMMERCE_BACKEND
    if ECOMMERCE_BACKEND == "shopify":
        from api_builder.shopify_orders_executor import ShopifyOrdersExecutor
        from models import WooAPICall
        reorder_call = WooAPICall(
            method="POST",
            endpoint="orders",
            params={},
            body={
                "_op":         "create_order",
                "customer_id": str(customer_id),
                "line_items":  new_line_items,
            },
            surface="shopify_orders",
            description="Create Shopify reorder",
        )
        reorder_resp = ShopifyOrdersExecutor().execute(reorder_call)
    else:
        reorder_call = endpoints.create_order(
            payload={
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
    
    elapsed = time.time() - start_time
    if reorder_resp.get("success") and isinstance(reorder_resp.get("data"), dict):
        new_order = reorder_resp["data"]
        logger.info(f"Step 3.5: Reorder created successfully | order_id={new_order.get('id')} | order_number={new_order.get('number')}")
        bot_message = (
            f"🔄 **Reorder placed!** (new order #{new_order.get('number') or new_order.get('id')})\n\n"
            + "\n".join(f"  • {i.get('name','Item')} × {i.get('quantity',1)}" for i in source_line_items if i.get("product_id"))
            + "\n\nYour order has been created and is being processed. ✅"
        )
        suggestions = ["Show my orders", "Track my order"]
    else:
        error_msg = sanitize_log_string(str(reorder_resp.get('error', 'Unknown')))
        logger.warning(f"Step 3.5: Reorder creation failed | error={error_msg}")
        bot_message = (
            f"🔄 **Items identified** (from order #{source_order.get('number') or source_order.get('id')})\n\n"
            + "\n".join(f"  • {i.get('name','Item')} × {i.get('quantity',1)}" for i in source_line_items if i.get("product_id"))
            + "\n\n⚠️ The new order could not be created automatically. Please place the order manually or contact support."
        )
        suggestions = ["Show my orders", "Browse products", "Contact support"]

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": intent.value,
        "products": [],
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200

def handle_order_status(intent, entities, order_data, customer_id, session_id, page, start_time,
                        role=None):
    """Step 3.5d: Handle ORDER_STATUS/ORDER_TRACKING when no specific order ID was given.

    When the user asks about an order without providing a number, api_builder
    falls back to list_cs_orders (filtered by date if available). chat.py may
    further narrow the result by lookup_email (a rep asking about the order they
    sent to a specific recipient). This handler interprets the resulting list:
      - 0 results → clear no-match message, naming the email/period searched
      - 1 result  → return None so _build_final_response calls format_order_detail
      - 2+ results → tappable order cards, AWAITING_ORDER_DETAIL (tap re-enters
                     as "show me order #N" and resolves to a single detail)
    """
    if intent not in (Intent.ORDER_STATUS, Intent.ORDER_TRACKING):
        return None
    # Specific order_id was present — the direct fetch already ran (this is also
    # the path taken when the user taps an order card, which re-enters as
    # "show me order #N"). Let _build_final_response call format_order_detail.
    if getattr(entities, "order_id", None):
        return None

    elapsed      = time.time() - start_time
    lookup_email = getattr(entities, "lookup_email", None)
    date_after   = getattr(entities, "date_after", None)

    # Build the human-readable period phrase once (exception-safe helper).
    period = None
    if date_after:
        from response_generator import _describe_date_period
        period = _describe_date_period(date_after)

    # ── 0 results ──────────────────────────────────────────────────────────
    # Name the email and/or period so the message reflects what was actually
    # searched — "no orders for X from the last week", not a generic miss.
    if not order_data:
        if lookup_email and period:
            msg = f"I couldn't find any orders for **{lookup_email}** from {period}."
        elif lookup_email:
            msg = f"I couldn't find any orders for **{lookup_email}**."
        elif period:
            msg = f"I couldn't find any orders from {period}."
        else:
            msg = "I couldn't find any recent orders on your account."
        return jsonify({
            "success": True,
            "bot_message": msg,
            "intent": intent.value,
            "products": [],
            "suggestions": ["Show my recent orders", "Browse products"],
            "session_id": session_id,
            "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": round(elapsed * 1000)},
            "pagination": default_pagination(page),
            "flow_state": FlowState.IDLE.value,
        }), 200

    # ── 1 result ───────────────────────────────────────────────────────────
    # Single match — fall through to _build_final_response which will call
    # format_order_detail(order_data[0]) for ORDER_STATUS/TRACKING.
    if len(order_data) == 1:
        return None

    # ── 2+ results ─────────────────────────────────────────────────────────
    # Present tappable order cards. AWAITING_ORDER_DETAIL is safe here: tapping
    # a card sends "show me order #N", which re-enters the pipeline, resolves
    # via extract_order_id -> fetch_order, and exits to flow_state IDLE.
    # (Bot text is suppressed by the frontend when order cards render; the copy
    # below is an accessibility/fallback string only.)
    if lookup_email and period:
        bot_msg = f"I found {len(order_data)} orders for **{lookup_email}** from {period}. Tap one to see its status."
    elif lookup_email:
        bot_msg = f"I found {len(order_data)} orders for **{lookup_email}**. Tap one to see its status."
    elif period:
        bot_msg = f"I found {len(order_data)} orders from {period}. Tap one to see its status."
    else:
        bot_msg = f"I found {len(order_data)} recent orders. Tap one to see its status."

    from handlers.chat_utils import format_order_for_frontend
    from app_config import is_order_report_admin
    return jsonify({
        "success": True,
        "bot_message": bot_msg,
        "intent": intent.value,
        "products": [],
        "orders": [format_order_for_frontend(o) for o in order_data],
        "suggestions": [],
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.AWAITING_ORDER_DETAIL.value,
            "response_time_ms": round(elapsed * 1000),
            # Decided here, not in the widget: the export carries customer
            # names, emails and addresses, and the browser has no basis to
            # authorise that. The plugin re-checks capabilities on the fetch
            # itself, so this only governs whether the control is OFFERED.
            "allow_order_download": is_order_report_admin(role),
        },
        "pagination": default_pagination(page),
        "flow_state": FlowState.AWAITING_ORDER_DETAIL.value,
    }), 200

def handle_order_detail(current_flow_state, customer_id, user_context, session_id, page, start_time):
    """Step 3.5b: Fetch and display a specific order's details."""
    if not (current_flow_state == FlowState.AWAITING_ORDER_DETAIL and customer_id):
        return None

    _detail_order_id = user_context.get("pending_order_id")
    logger.info(f"Step 3.5b: Fetching order detail | order_id={_detail_order_id}")
    if not _detail_order_id:
        return None

    detail_call = endpoints.fetch_order(
        order_id=_detail_order_id,
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
    _is_shopify = bool(_order_product_raw.get("_shopify_gid"))
    _out_of_stock = (
        (not _order_product_raw.get("in_stock"))   # Shopify: trust in_stock boolean
        if _is_shopify
        else _order_product_raw.get("stock_status") == "outofstock"  # WooCommerce: unchanged
    )
    if _order_product_raw and _out_of_stock:
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
                    "resolved_attributes": getattr(entities, 'attributes', {}),
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
                var_call = endpoints.list_variants(
                    product_id=_order_product_id,
                    per_page=100,
                    status="publish",
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
                            " / ".join(_get_safe_options(v.get("attributes", [])).values())
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
                            "resolved_attributes": getattr(entities, 'attributes', {}),
                            "response_time_ms": round(elapsed * 1000),
                        },
                        "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                        "pagination": default_pagination(page),
                    }), 200

    # Simple product or resolved variation — go to cart confirmation
    logger.info(f"Step 3.6: Product resolved, proceeding to cart confirmation | product_id={_order_product_id} | variation_id={_order_variation_id} | quantity={entities.quantity}")

    quantity = entities.quantity or 1
    variant_label = ""
    if _order_variation_id and _order_product_raw:
        attrs = (_order_product_raw or {}).get("attributes", [])
        if isinstance(attrs, list):
            variant_label = " / ".join(
                a.get("option", "") for a in attrs if isinstance(a, dict) and a.get("option")
            )

    variant_suffix = f" ({variant_label})" if variant_label else ""
    cart_msg = f"Got it — add **{_order_product_name}**{variant_suffix} ×{quantity} to your cart?"

    elapsed = time.time() - start_time
    return jsonify({
        "success": True,
        "bot_message": cart_msg,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, add it", "No thanks"],
        "session_id": session_id,
        "metadata": {
            "pending_product_id": _order_product_id,
            "pending_product_name": _order_product_name,
            "pending_quantity": quantity,
            "pending_variation_id": _order_variation_id,
            "resolved_attributes": getattr(entities, 'attributes', {}),
            "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state": FlowState.AWAITING_CART_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200