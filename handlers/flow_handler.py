"""
handlers/flow_handler.py — Step 0: Multi-turn conversation flow handling.

Handles all FlowState branches: order confirmation, address collection,
price summary display, and order creation from confirmed flow state.
Returns a Flask response if the flow consumed the message, or None to fall through.
"""

import time
from datetime import datetime, timezone

from flask import jsonify

from app_config import (
    WOO_BASE_URL,
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
    get_currency_symbol,
)
from models import WooAPICall
from woo_client import woo_client
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string
from handlers.chat_utils import (
    default_pagination,
    parse_address,
    fetch_unit_price,
    fetch_shipping_address,
    shipping_address_response,
)

logger = get_logger("miraq_chat")


def handle_flow(
    flow_result: dict,
    user_context: dict,
    session_id: str,
    customer_id,
    page: int,
    start_time: float,
    sessions: dict,
):
    """
    Process the result of handle_flow_state().
    Returns a Flask response tuple if the flow consumed this turn, else None.
    """
    if not flow_result:
        return None

    # ── Flow handler consumed the message entirely ──
    if not flow_result.get("pass_through"):
        logger.info(f"Step 0: Flow handler consumed message | new_state={flow_result.get('flow_state', 'idle')}")
        flow_metadata: dict = {
            "flow_state": flow_result.get("flow_state", "idle"),
            "response_time_ms": round((time.time() - start_time) * 1000),
            "provider": "conversation_flow",
        }
        for ctx_key in ("pending_product_name", "pending_product_id", "pending_quantity", "pending_variation_id", "pending_shipping_address"):
            if flow_result.get(ctx_key) is not None:
                flow_metadata[ctx_key] = flow_result[ctx_key]
            elif user_context.get(ctx_key) is not None:
                flow_metadata[ctx_key] = user_context[ctx_key]
        return jsonify({
            "success": True,
            "bot_message": flow_result["bot_message"],
            "intent": "guided_flow",
            "products": [],
            "suggestions": flow_result.get("suggestions", []),
            "session_id": session_id,
            "metadata": flow_metadata,
            "flow_state": flow_result.get("flow_state", "idle"),
            "pagination": default_pagination(page),
        }), 200

    # ── Order confirmed — create the order ──
    if flow_result.get("create_order"):
        return _handle_create_order(flow_result, user_context, session_id, customer_id, page, start_time, sessions)

    # ── Fetch customer address to show shipping confirmation ──
    if flow_result.get("fetch_customer_address"):
        return _handle_fetch_address(flow_result, user_context, session_id, customer_id, page, start_time)

    # ── Shipping address confirmed — show final order summary ──
    if flow_result.get("fetch_price_summary"):
        return _handle_price_summary(flow_result, user_context, session_id, customer_id, page, start_time)

    return None


def _handle_create_order(flow_result, user_context, session_id, customer_id, page, start_time, sessions):
    CS = get_currency_symbol()
    pending_product_id = user_context.get("pending_product_id")
    pending_product_name = user_context.get("pending_product_name", "")
    pending_quantity = user_context.get("pending_quantity", 1)
    pending_variation_id = user_context.get("pending_variation_id")

    if not (pending_product_id and customer_id):
        return None

    logger.info(f"Step 0: Order confirmed via flow | product_id={pending_product_id} | quantity={pending_quantity} | variation_id={pending_variation_id}")

    _confirmed_line_item: dict = {"product_id": pending_product_id, "quantity": pending_quantity}
    if pending_variation_id:
        _confirmed_line_item["variation_id"] = pending_variation_id

    resolved_attrs = user_context.get("resolved_attributes", {})
    if resolved_attrs:
        meta_data = [{"key": k, "value": v} for k, v in resolved_attrs.items()]
        _confirmed_line_item["meta_data"] = meta_data

    order_body: dict = {
        "status": "processing",
        "customer_id": customer_id,
        "payment_method": DEFAULT_PAYMENT_METHOD,
        "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
        "set_paid": False,
        "line_items": [_confirmed_line_item],
    }

    _use_new_address = flow_result.get("use_new_address") or user_context.get("use_new_address")
    _use_existing_address = flow_result.get("use_existing_address") or user_context.get("use_existing_address")

    if _use_new_address:
        raw_address = user_context.get("pending_shipping_address", "")
        if raw_address:
            order_body["shipping"] = parse_address(raw_address)
            logger.info(f"Step 0: Including new shipping address | address={order_body['shipping']}")

    elif _use_existing_address:
        try:
            _cust_call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/customers/{customer_id}",
                params={},
                body={},
                description=f"Fetch customer {customer_id} address for order",
            )
            _cust_resp = woo_client.execute(_cust_call)
            if _cust_resp.get("success") and isinstance(_cust_resp.get("data"), dict):
                _cust_data = _cust_resp["data"]
                _shipping = _cust_data.get("shipping", {})
                _billing = _cust_data.get("billing", {})
                if _shipping.get("address_1") or _shipping.get("city"):
                    order_body["shipping"] = _shipping
                    logger.info(f"Step 0: Including existing shipping address | city={_shipping.get('city')}")
                if _billing.get("email") or _billing.get("address_1"):
                    order_body["billing"] = _billing
        except Exception as _exc:
            logger.warning(f"Step 0: Could not fetch customer address for order | error={_exc}")

    order_call = WooAPICall(
        method="POST",
        endpoint=f"{WOO_BASE_URL}/orders",
        params={},
        body=order_body,
        description=f"Create order for '{pending_product_name}' (confirmed via flow)",
    )
    order_resp = woo_client.execute(order_call)

    if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
        created_order = order_resp["data"]
        order_number = created_order.get("number") or created_order.get("id", "N/A")
        total = created_order.get("total", "0.00")

        if float(total) == 0.0 and created_order.get("line_items"):
            line_total = sum(float(item.get("total", "0") or "0") for item in created_order["line_items"])
            if line_total > 0:
                total = str(line_total)

        product_name = pending_product_name or "your item"
        if created_order.get("line_items"):
            product_name = created_order["line_items"][0].get("name") or product_name

        # Prefer currency_symbol from WooCommerce order response, fall back to configured symbol
        currency = created_order.get("currency_symbol") or CS

        bot_message = (
            f"✅ **Order #{order_number} placed successfully!**\n\n"
            f"**Product:** {product_name}\n"
            f"**Quantity:** {pending_quantity}\n"
            f"**Total:** {currency}{float(total):.2f}\n"
            f"**Payment Mode:** {DEFAULT_PAYMENT_METHOD_TITLE}\n"
        )

        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": bot_message,
            "intent": "order",
            "products": [],
            "suggestions": ["Show me more products", "Check my orders", "No, that's all"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_ANYTHING_ELSE.value,
            "pagination": default_pagination(page),
        }), 200

    else:
        error_msg = str(order_resp.get("error", "Unknown"))
        _err_data = order_resp.get("data") or {}
        _err_code = ""
        _err_detail = ""
        if isinstance(_err_data, dict):
            _err_code = str(_err_data.get("code", ""))
            _err_detail = str(_err_data.get("message", ""))
        _err_combined = f"{_err_code} {_err_detail} {error_msg}".lower()

        logger.error(
            f"Step 0: Order creation failed | error={sanitize_log_string(error_msg)} "
            f"| wc_code={_err_code} | wc_detail={sanitize_log_string(_err_detail)}"
        )

        _is_variant_error = any(kw in _err_combined for kw in [
            "invalid_variation", "invalid variation",
            "out of stock", "cannot be purchased",
            "product is not purchasable", "not purchasable",
            "variation", "no longer available",
            "stock", "sold out",
        ])

        if _is_variant_error and pending_product_id:
            if session_id and session_id in sessions:
                sessions[session_id].get("variation_cache", {}).pop(str(pending_product_id), None)
                logger.info(
                    f"Step 0: Cleared variation cache for product_id={pending_product_id} "
                    f"due to order failure (variant likely discontinued)"
                )
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Sorry, it looks like the variant you selected for "
                    f"**{pending_product_name}** is no longer available. "
                    f"Let me show you what's currently in stock — which variant would you like?"
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": [],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                    "pending_product_id": pending_product_id,
                    "pending_product_name": pending_product_name,
                    "pending_quantity": pending_quantity,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pagination": default_pagination(page),
            }), 200

        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "bot_message": (
                "Sorry, I couldn't place the order right now. "
                "This could be a temporary issue — please try again in a moment."
            ),
            "intent": "order",
            "products": [],
            "suggestions": ["Try again", "Show me products"],
            "session_id": session_id,
            "metadata": {
                "flow_state": FlowState.AWAITING_FINAL_CONFIRM.value,
                "pending_product_id": pending_product_id,
                "pending_product_name": pending_product_name,
                "pending_quantity": pending_quantity,
                "pending_variation_id": pending_variation_id,
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_FINAL_CONFIRM.value,
            "pagination": default_pagination(page),
        }), 200


def _handle_fetch_address(flow_result, user_context, session_id, customer_id, page, start_time):
    pending_product_id = user_context.get("pending_product_id")
    pending_product_name = user_context.get("pending_product_name", "")
    pending_quantity = flow_result.get("pending_quantity") or user_context.get("pending_quantity", 1)
    pending_variation_id = user_context.get("pending_variation_id")

    shipping_address = fetch_shipping_address(customer_id, "Step 0") if customer_id else None

    has_address = bool(
        shipping_address
        and (shipping_address.get("address_1") or shipping_address.get("city"))
    )

    base_meta = {
        "pending_product_name": pending_product_name,
        "pending_product_id": pending_product_id,
        "pending_quantity": pending_quantity,
        "pending_variation_id": pending_variation_id,
        "response_time_ms": round((time.time() - start_time) * 1000),
    }

    if has_address:
        addr_parts = [
            p for p in [
                shipping_address.get("address_1", ""),
                shipping_address.get("address_2", ""),
                shipping_address.get("city", ""),
                shipping_address.get("state", ""),
                shipping_address.get("postcode", ""),
                shipping_address.get("country", ""),
            ] if p
        ]
        addr_display = ", ".join(addr_parts)
        logger.info(f"Step 0: Showing shipping address to user | address={addr_display}")
        return jsonify({
            "success": True,
            "bot_message": (
                f"Your shipping address on file:\n\n"
                f"📦 **{addr_display}**\n\n"
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
        logger.info("Step 0: No shipping address on file — prompting user to enter one")
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


def _handle_price_summary(flow_result, user_context, session_id, customer_id, page, start_time):
    CS = get_currency_symbol()
    pending_product_id = user_context.get("pending_product_id")
    pending_product_name = user_context.get("pending_product_name", "the product")
    pending_quantity = user_context.get("pending_quantity", 1)
    pending_variation_id = user_context.get("pending_variation_id")

    _price_display = fetch_unit_price(pending_product_id, pending_variation_id)

    _variant_label = ""
    if pending_variation_id and pending_product_id:
        try:
            var_call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/products/{pending_product_id}/variations/{pending_variation_id}",
                params={},
                description=f"Fetch variation {pending_variation_id} for summary label",
            )
            var_resp = woo_client.execute(var_call)
            if var_resp.get("success") and isinstance(var_resp.get("data"), dict):
                var_data = var_resp["data"]
                _variant_label = " / ".join(
                    a.get("option", "") for a in var_data.get("attributes", []) if a.get("option")
                )
        except Exception:
            pass

    try:
        _total = float(_price_display) * int(pending_quantity)
        _total_display = f"{CS}{_total:.2f}"
    except (ValueError, TypeError):
        _total_display = "N/A"

    _product_line = f"{pending_product_name} ({_variant_label})" if _variant_label else pending_product_name

    logger.info(
        f"Step 0: Final confirmation summary | product={_product_line} | "
        f"qty={pending_quantity} | unit_price={_price_display} | total={_total_display}"
    )

    base_meta = {
        "pending_product_name": pending_product_name,
        "pending_product_id": pending_product_id,
        "pending_quantity": pending_quantity,
        "pending_variation_id": pending_variation_id,
        "flow_state": FlowState.AWAITING_FINAL_CONFIRM.value,
        "response_time_ms": round((time.time() - start_time) * 1000),
    }
    if flow_result.get("use_existing_address"):
        base_meta["use_existing_address"] = True
    if flow_result.get("use_new_address"):
        base_meta["use_new_address"] = True
    if user_context.get("pending_shipping_address"):
        base_meta["pending_shipping_address"] = user_context["pending_shipping_address"]

    return jsonify({
        "success": True,
        "bot_message": (
            f"📋 **Order Summary**\n\n"
            f"**Product:** {_product_line}\n"
            f"**Quantity:** {pending_quantity}\n"
            f"**Unit Price:** {CS}{_price_display}\n"
            f"**Estimated Total:** {_total_display}\n"
            f"**Payment:** {DEFAULT_PAYMENT_METHOD_TITLE}\n\n"
            f"Shall I place this order? ✅"
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm order", "No, cancel"],
        "session_id": session_id,
        "metadata": base_meta,
        "flow_state": FlowState.AWAITING_FINAL_CONFIRM.value,
        "pagination": default_pagination(page),
    }), 200