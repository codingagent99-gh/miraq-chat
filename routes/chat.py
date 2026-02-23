"""
Chat endpoint as a Flask Blueprint.
"""

import os
import re as _re
import time
from datetime import datetime, timezone
from typing import List, Dict
from config.settings import DEFAULT_PER_PAGE  # add this import at top of file alongside existing imports

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
    format_category,
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
from session_store import sessions, touch_session
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

_TOKEN_OVERLAP_THRESHOLD = 0.5
_STRIP_QUOTES_RE = _re.compile(r'["\'\u201c\u201d\u2018\u2019]')
_TOKENIZE_RE = _re.compile(r'[\w/]+')


def _score_variation_against_text(var: dict, user_text_clean: str, user_tokens: set) -> int:
    """Score how well a variation's attribute options match the user's cleaned message.

    Returns a non-negative integer score:
    * +2 for each attribute option whose cleaned string is found verbatim in *user_text_clean*.
    * +1 for each attribute option that has >=50% token overlap with *user_tokens*, or whose
      cleaned string contains at least one significant (len>=2) user token as a substring.
    * +1 bonus when the option tokens are a full subset of user_tokens AND the option token
      count exactly matches the number of matching user tokens — rewards specificity so that
      "WATERFALL Havana Linear" scores higher than "WATERFALL Havana" when user says
      "waterfall havana linear".
    """
    score = 0
    for attr in var.get("attributes", []):
        opt = attr.get("option", "").lower()
        if not opt:
            continue
        opt_clean = _STRIP_QUOTES_RE.sub('', opt)
        opt_tokens = set(_TOKENIZE_RE.findall(opt_clean))
        if opt_clean in user_text_clean:
            score += 2
            # Bonus: reward more specific (longer) options that fully match
            # e.g. "waterfall havana linear" beats "waterfall havana"
            if opt_tokens and opt_tokens.issubset(user_tokens):
                score += len(opt_tokens)
        else:
            if opt_tokens:
                overlap = opt_tokens & user_tokens
                if len(overlap) >= max(1, len(opt_tokens) * _TOKEN_OVERLAP_THRESHOLD):
                    score += 1
                elif any(len(t) >= 2 and t in opt_clean for t in user_tokens):
                    score += 1
    return score

def parse_address(text: str) -> dict:
    """Parse a free-text address string into WooCommerce shipping fields."""
    parts = [p.strip() for p in text.split(",")]
    address: dict = {"country": "US"}
    if len(parts) >= 1:
        address["address_1"] = parts[0]
    if len(parts) >= 2:
        address["city"] = parts[1]
    if len(parts) >= 3:
        state_zip = parts[2].strip().split()
        if len(state_zip) >= 2:
            address["state"] = state_zip[0]
            address["postcode"] = state_zip[1]
        elif len(state_zip) == 1:
            address["state"] = state_zip[0]
    if len(parts) >= 4:
        address["postcode"] = parts[3].strip()
    return address


def _default_pagination(page: int = 1) -> dict:
    """Return a default pagination object for responses without product lists."""
    return {
        "page": page,
        "per_page": 0,
        "total_items": 0,
        "total_pages": 1,
        "has_more": False,
    }


def _build_variant_prompt(product_raw: dict, product_name: str) -> str:
    """Build a variant selection prompt message from the product's variation attributes."""
    attrs = product_raw.get("attributes", [])
    variation_attrs = [a for a in attrs if isinstance(a, dict) and a.get("variation")]
    if not variation_attrs:
        return (
            f"I'd love to order **{product_name}** for you! "
            "Which variant would you like? Please specify the options you'd like."
        )
    lines = [f"I'd love to order **{product_name}** for you! But first, I need to know which variant you'd like. 🎨\n\n**Available options:**"]
    for attr in variation_attrs:
        name = attr.get("name", "")
        options = attr.get("options", [])
        if options:
            lines.append(f"�� **{name}:** {', '.join(options)}")
    lines.append("\nWhich combination would you like?")
    return "\n".join(lines)


def _build_pagination(page: int, api_responses: list, api_calls: list) -> dict:
    """Build pagination object from API responses and call params."""
    total_items = None
    total_pages = None
    per_page = DEFAULT_PER_PAGE

    # Extract per_page from the first API call's params
    if api_calls:
        per_page = int(api_calls[0].params.get("per_page", DEFAULT_PER_PAGE))

    # Extract total/total_pages from the first successful response
    for resp in api_responses:
        if resp.get("success"):
            raw_total = resp.get("total")
            raw_total_pages = resp.get("total_pages")
            if raw_total is not None:
                try:
                    total_items = int(raw_total)
                except (ValueError, TypeError):
                    pass
            if raw_total_pages is not None:
                try:
                    total_pages = int(raw_total_pages)
                except (ValueError, TypeError):
                    pass
            break

    has_more = (page < total_pages) if total_pages is not None else False
    return {
        "page": page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_more": has_more,
    }

def _fetch_unit_price(product_id, variation_id=None) -> str:
    """Fetch the unit price for a product or variation. Returns price string or 'N/A'."""
    try:
        if variation_id and product_id:
            call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/products/{product_id}/variations/{variation_id}",
                params={},
                description=f"Fetch variation {variation_id} price",
            )
            resp = woo_client.execute(call)
            if resp.get("success") and isinstance(resp.get("data"), dict):
                d = resp["data"]
                return d.get("sale_price") or d.get("price") or d.get("regular_price") or "N/A"
        elif product_id:
            call = WooAPICall(
                method="GET",
                endpoint=f"{WOO_BASE_URL}/products/{product_id}",
                params={},
                description=f"Fetch product {product_id} price",
            )
            resp = woo_client.execute(call)
            if resp.get("success") and isinstance(resp.get("data"), dict):
                d = resp["data"]
                return d.get("sale_price") or d.get("price") or d.get("regular_price") or "N/A"
    except Exception as exc:
        logger.warning(f"_fetch_unit_price failed | error={exc}")
    return "N/A"


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
            "pagination": _default_pagination(),
        }), 400

    message = body.get("message", "").strip()
    session_id = body.get("session_id", "").strip()
    # Generate a session_id if the frontend didn't send one
    if not session_id:
        import uuid
        session_id = f"auto-{uuid.uuid4().hex}"
        logger.info(f"POST /chat | No session_id received — generated: {session_id}")
    user_context = body.get("user_context", {})
    page = int(body.get("page", 1))
    
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
            "pagination": _default_pagination(page),
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
        "pending_variation_id": user_context.get("pending_variation_id"),
        "resolved_attributes": user_context.get("resolved_attributes"),
    }

    # If we're in a multi-turn flow, let the flow handler try first
    flow_result = None
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
            flow_metadata: dict = {
                "flow_state": flow_result.get("flow_state", "idle"),
                "response_time_ms": round((time.time() - start_time) * 1000),
                "provider": "conversation_flow",
            }
            # Propagate pending context so the frontend can send it back on the next turn
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
                "filters_applied": {},
                "suggestions": flow_result.get("suggestions", []),
                "session_id": session_id,
                "metadata": flow_metadata,
                "flow_state": flow_result.get("flow_state", "idle"),
                "pagination": _default_pagination(page),
            }), 200

        elif flow_result and flow_result.get("override_message"):
            # Flow wants to redirect to a different utterance
            message = flow_result["override_message"]

        elif flow_result and flow_result.get("create_order"):
            # Flow confirmed order — use pending context to create the order
            pending_product_id = user_context.get("pending_product_id")
            pending_product_name = user_context.get("pending_product_name", "")
            pending_quantity = user_context.get("pending_quantity", 1)
            pending_variation_id = user_context.get("pending_variation_id")
            
            if pending_product_id and customer_id:
                logger.info(f"Step 0: Order confirmed via flow | product_id={pending_product_id} | quantity={pending_quantity} | variation_id={pending_variation_id}")
                
                # Build line item; include variation_id for variable products
                _confirmed_line_item: dict = {"product_id": pending_product_id, "quantity": pending_quantity}
                if pending_variation_id:
                    _confirmed_line_item["variation_id"] = pending_variation_id

                # Build order body; include shipping override if user provided a new address
                order_body: dict = {
                    "status": "processing",
                    "customer_id": customer_id,
                    "payment_method": DEFAULT_PAYMENT_METHOD,
                    "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
                    "set_paid": False,
                    "line_items": [_confirmed_line_item],
                }
                # Check both flow_result flags and user_context flags for address handling
                _use_new_address = flow_result.get("use_new_address") or user_context.get("use_new_address")
                if _use_new_address:
                    raw_address = user_context.get("pending_shipping_address", "")
                    if raw_address:
                        order_body["shipping"] = parse_address(raw_address)
                        logger.info(f"Step 0: Including shipping override | address={order_body['shipping']}")

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
                        "pagination": _default_pagination(page),
                    }), 200
                else:
                    error_msg = str(order_resp.get("error", "Unknown"))
                    # WooCommerce returns structured errors in the data field on failure
                    _err_data = order_resp.get("data") or {}
                    _err_code = ""
                    _err_detail = ""
                    if isinstance(_err_data, dict):
                        _err_code = str(_err_data.get("code", ""))
                        _err_detail = str(_err_data.get("message", ""))
                    # Also check error string itself for WooCommerce error codes
                    _err_combined = f"{_err_code} {_err_detail} {error_msg}".lower()

                    logger.error(
                        f"Step 0: Order creation failed | error={sanitize_log_string(error_msg)} "
                        f"| wc_code={_err_code} | wc_detail={sanitize_log_string(_err_detail)}"
                    )

                    # ── Detect discontinued/invalid variation ──
                    _is_variant_error = any(kw in _err_combined for kw in [
                        "invalid_variation", "invalid variation",
                        "out of stock", "cannot be purchased",
                        "product is not purchasable", "not purchasable",
                        "variation", "no longer available",
                        "stock", "sold out",
                    ])

                    if _is_variant_error and pending_product_id:
                        # Clear the stale variation cache so next turn re-fetches from WooCommerce
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
                            "filters_applied": {},
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
                            "pagination": _default_pagination(page),
                        }), 200

                    # ── Generic order failure ──
                    elapsed = time.time() - start_time
                    return jsonify({
                        "success": True,
                        "bot_message": (
                            "Sorry, I couldn't place the order right now. "
                            "This could be a temporary issue — please try again in a moment."
                        ),
                        "intent": "order",
                        "products": [],
                        "filters_applied": {},
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
                        "pagination": _default_pagination(page),
                    }), 200

        elif flow_result and flow_result.get("fetch_customer_address"):
            # User confirmed order or provided quantity — fetch their shipping address
            pending_product_id = user_context.get("pending_product_id")
            pending_product_name = user_context.get("pending_product_name", "")
            # Quantity may come from the flow_result (AWAITING_QUANTITY) or user_context
            pending_quantity = flow_result.get("pending_quantity") or user_context.get("pending_quantity", 1)
            pending_variation_id = user_context.get("pending_variation_id")

            shipping_address = None
            if customer_id:
                try:
                    cust_call = WooAPICall(
                        method="GET",
                        endpoint=f"{WOO_BASE_URL}/customers/{customer_id}",
                        params={},
                        body={},
                        description=f"Fetch customer {customer_id} shipping address",
                    )
                    cust_resp = woo_client.execute(cust_call)
                    if cust_resp.get("success") and isinstance(cust_resp.get("data"), dict):
                        shipping_address = cust_resp["data"].get("shipping", {})
                except Exception as exc:
                    logger.warning(f"Step 0: Could not fetch customer address | error={exc}")

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
                    "filters_applied": {},
                    "suggestions": ["Yes, use this address", "Change address", "Cancel"],
                    "session_id": session_id,
                    "metadata": {**base_meta, "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value},
                    "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
                    "pagination": _default_pagination(page),
                }), 200
            else:
                logger.info("Step 0: No shipping address on file — prompting user to enter one")
                return jsonify({
                    "success": True,
                    "bot_message": "No shipping address is on file. Please type your shipping address (street, city, state, zip code):",
                    "intent": "guided_flow",
                    "products": [],
                    "filters_applied": {},
                    "suggestions": [],
                    "session_id": session_id,
                    "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
                    "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                    "pagination": _default_pagination(page),
                }), 200

        elif flow_result and flow_result.get("fetch_price_summary"):
            # Shipping address confirmed — fetch price and show final order summary
            pending_product_id = user_context.get("pending_product_id")
            pending_product_name = user_context.get("pending_product_name", "the product")
            pending_quantity = user_context.get("pending_quantity", 1)
            pending_variation_id = user_context.get("pending_variation_id")

            _price_display = _fetch_unit_price(pending_product_id, pending_variation_id)

            # Build variant label if we have a variation_id
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

            # Calculate total
            try:
                _total = float(_price_display) * int(pending_quantity)
                _total_display = f"${_total:.2f}"
            except (ValueError, TypeError):
                _total_display = "N/A"

            # Build the product description line
            if _variant_label:
                _product_line = f"{pending_product_name} ({_variant_label})"
            else:
                _product_line = pending_product_name

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
            # Carry forward address info so create_order handler knows which to use
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
                    f"**Unit Price:** ${_price_display}\n"
                    f"**Estimated Total:** {_total_display}\n"
                    f"**Payment:** Cash on Delivery\n\n"
                    f"Shall I place this order? ✅"
                ),
                "intent": "guided_flow",
                "products": [],
                "filters_applied": {},
                "suggestions": ["Yes, confirm order", "No, cancel"],
                "session_id": session_id,
                "metadata": base_meta,
                "flow_state": FlowState.AWAITING_FINAL_CONFIRM.value,
                "pagination": _default_pagination(page),
            }), 200

    # Capture resolve_variant flag from flow handler (set when in AWAITING_VARIANT_SELECTION)
    _resolve_variant = bool(flow_result and flow_result.get("resolve_variant"))

    if _resolve_variant:
        # Skip Steps 1–3 entirely: Step 3.55 is self-contained and only needs
        # the raw user message to score variations. Running the classifier here
        # re-interprets "allspice beleza" as product_search, building wrong API calls.
        from models import ExtractedEntities
        intent = Intent.QUICK_ORDER
        entities = ExtractedEntities()
        confidence = 1.0
        result = None  # not used in variant resolution path
        api_calls = []
        api_calls_to_execute = []
        api_responses = []
        all_products_raw = []
        order_data = []
        # Preserve customer_id (needed by Step 3.55)
        customer_id = user_context.get("customer_id")
        last_product_ctx = None
        logger.info("Steps 1-3: Skipped (variant resolution mode — Step 3.55 will handle)")
    else:
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
            }.items() if v is not None
        }
        logger.info(f"Step 1: Classified intent={intent.value} | confidence={confidence:.2f} | entities={entity_summary}")

        # ─── Step 1.5: LLM Fallback / Disambiguation check ───
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
            last_product_ctx_check = user_context.get("last_product")
            if not (last_product_ctx_check and last_product_ctx_check.get("id")):
                should_try_llm = True
                llm_trigger_reason = "missing_entities"
        
        if should_try_llm and LLM_FALLBACK_ENABLED and not _resolve_variant:
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
                        "pagination": _default_pagination(page),
                    }), 200
                
                elif fallback_type in ["intent_resolved", "entity_extracted"]:
                    from models import ClassifiedResult, ExtractedEntities
                    
                    llm_entities_dict = llm_result.get("entities", {})
                    new_entities = ExtractedEntities()
                    
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
                    
                    if fallback_type == "entity_extracted":
                        for entity_field in entity_field_map.values():
                            new_val = getattr(new_entities, entity_field)
                            orig_val = getattr(entities, entity_field)
                            if new_val is None and orig_val is not None:
                                setattr(new_entities, entity_field, orig_val)
                    
                    llm_intent_str = llm_result.get("intent", "unknown")
                    try:
                        new_intent = Intent(llm_intent_str)
                    except ValueError:
                        intent_mapping = {
                            "search": Intent.PRODUCT_SEARCH,
                            "product_search": Intent.PRODUCT_SEARCH,
                            "browse": Intent.CATEGORY_BROWSE,
                            "category_browse": Intent.CATEGORY_BROWSE,
                            "filter": Intent.PRODUCT_LIST,
                            "filter_by_finish": Intent.FILTER_BY_FINISH,
                            "filter_by_color": Intent.FILTER_BY_COLOR,
                            "filter_by_size": Intent.FILTER_BY_SIZE,
                            "filter_by_application": Intent.FILTER_BY_APPLICATION,
                            "filter_by_material": Intent.FILTER_BY_MATERIAL,
                            "general_question": Intent.PRODUCT_LIST,
                            "order_inquiry": Intent.ORDER_HISTORY,
                            "order_history": Intent.ORDER_HISTORY,
                            "check_orders": Intent.ORDER_HISTORY,
                            "my_orders": Intent.ORDER_HISTORY,
                            "order_status": Intent.ORDER_STATUS,
                            "order_tracking": Intent.ORDER_TRACKING,
                            "last_order": Intent.LAST_ORDER,
                            "reorder": Intent.REORDER,
                            "order": Intent.QUICK_ORDER,
                            "place_order": Intent.PLACE_ORDER,
                            "quick_order": Intent.QUICK_ORDER,
                            "order_item": Intent.ORDER_ITEM,
                            "discount_inquiry": Intent.DISCOUNT_INQUIRY,
                            "promotions": Intent.PROMOTIONS,
                            "clearance": Intent.CLEARANCE_PRODUCTS,
                            "greeting": Intent.GREETING,
                        }
                        new_intent = intent_mapping.get(llm_intent_str, Intent.PRODUCT_LIST)

                        if llm_intent_str not in intent_mapping:
                            logger.warning(
                                f"Step 1.5: Unmapped LLM intent '{llm_intent_str}' — "
                                f"falling back to PRODUCT_LIST. Consider adding it to intent_mapping."
                            )
                                            
                    intent = new_intent
                    entities = new_entities
                    confidence = llm_result.get("confidence", 0.70)
                    
                    result = ClassifiedResult(
                        intent=intent,
                        entities=entities,
                        confidence=confidence,
                    )
                    
                    logger.info(
                        f"Step 1.5: LLM fallback applied | new_intent={intent.value} | "
                        f"new_confidence={confidence:.2f} | fallback_type={fallback_type}"
                    )
                else:
                    llm_result["success"] = False
            
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
                    "pagination": _default_pagination(page),
                }), 200
        
        elif should_try_llm and not LLM_FALLBACK_ENABLED and not _resolve_variant:
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
                "pagination": _default_pagination(page),
            }), 200

        # ─── Step 2: Build API calls ───
        api_calls = build_api_calls(result, page)
        endpoint_summary = [f"{c.method} {c.endpoint.split('/')[-1]}" for c in api_calls]
        logger.info(f"Step 2: Built {len(api_calls)} API call(s) | endpoints={endpoint_summary}")
        # ─── Step 2.5: Resolve user context placeholders ───
        customer_id = user_context.get("customer_id")
        if customer_id:
            logger.info(f"Step 2.5: Resolved customer_id={customer_id}")
            _resolve_user_placeholders(api_calls, customer_id)

        # ─── Step 2.6: Extract last_product context (for "order this" resolution) ───
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

    # ─── Step 3.55: AWAITING_VARIANT_SELECTION — resolve variant from user response ───
    if current_flow_state == FlowState.AWAITING_VARIANT_SELECTION and customer_id:
        _var_product_id = user_context.get("pending_product_id")
        _var_product_name = user_context.get("pending_product_name", "the product")
        _var_quantity = user_context.get("pending_quantity")
        logger.info(f"Step 3.55: Variant selection response | pending_product_id={_var_product_id} | pending_quantity={_var_quantity}")

        if _var_product_id:
            # ── Use session-side variation cache — avoids re-fetching from WooCommerce each turn ──
            _session_data = sessions.get(session_id, {})
            _var_cache = _session_data.get("variation_cache", {}).get(str(_var_product_id))
            if _var_cache:
                all_variations = _var_cache["variations"]
                logger.info(f"Step 3.55: Using session-cached variations ({len(all_variations)}) — skipping API call")
                _variations_loaded = True
            else:
                var_call = WooAPICall(
                    method="GET",
                    endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}/variations",
                    params={"per_page": 100, "status": "publish"},
                    description=f"Fetch variations for variant selection of '{_var_product_name}'",
                )
                var_resp = woo_client.execute(var_call)
                _variations_loaded = var_resp.get("success") and isinstance(var_resp.get("data"), list)
                if _variations_loaded:
                    all_variations = var_resp["data"]
            if _variations_loaded:

                # ── Pre-filter using resolved attributes from prior turns ──
                prev_resolved = user_context.get("resolved_attributes", {})
                # ── Pre-filter refinement: if the current message specifies a more specific
                #    value for an already-resolved attribute (e.g. "WATERFALL Havana Linear"
                #    refining a previously resolved "WATERFALL Havana"), update prev_resolved
                #    to the more specific value before filtering. This prevents the broader
                #    substring match from letting both the plain and Linear variants through. ──
                if prev_resolved:
                    user_msg_lower = message.lower()
                    user_msg_clean = _STRIP_QUOTES_RE.sub('', user_msg_lower)
                    refined_resolved = dict(prev_resolved)
                    for attr_name, attr_val in prev_resolved.items():
                        # Collect all distinct option values for this attribute across all variations
                        candidate_options = set()
                        for var in all_variations:
                            for a in var.get("attributes", []):
                                if a.get("name", "").lower() == attr_name.lower():
                                    opt = a.get("option", "")
                                    if opt:
                                        candidate_options.add(opt)
                        # Find options that are more specific than the current resolved value
                        # i.e. they contain the current value as a substring AND are longer
                        # AND appear in the current user message
                        current_val_lower = attr_val.lower()
                        for opt in candidate_options:
                            opt_lower = opt.lower()
                            opt_clean = _STRIP_QUOTES_RE.sub('', opt_lower)
                            if (
                                current_val_lower in opt_lower        # more specific
                                and opt_lower != current_val_lower    # actually different
                                and opt_clean in user_msg_clean       # user mentioned it
                            ):
                                refined_resolved[attr_name] = opt
                                logger.info(
                                    f"Step 3.55: Refined resolved attribute "
                                    f"{attr_name}: '{attr_val}' → '{opt}' based on current message"
                                )
                                break
                    prev_resolved = refined_resolved

                if prev_resolved:
                    pre_filtered = []
                    for var in all_variations:
                        var_attrs = {
                            a.get("name", "").lower(): a.get("option", "").lower()
                            for a in var.get("attributes", [])
                        }
                        matches_all = True
                        for attr_name, attr_val in prev_resolved.items():
                            var_opt = var_attrs.get(attr_name.lower(), "")
                            if attr_val.lower() not in var_opt:
                                matches_all = False
                                break
                        if matches_all:
                            pre_filtered.append(var)
                    if pre_filtered:
                        logger.info(f"Step 3.55: Pre-filtered {len(all_variations)} → {len(pre_filtered)} using resolved_attributes={prev_resolved}")
                        all_variations = pre_filtered

                if _resolve_variant:
                    user_text_lower = message.lower()
                    user_text_clean = _STRIP_QUOTES_RE.sub('', user_text_lower)
                    user_tokens = set(_TOKENIZE_RE.findall(user_text_clean))
                    scores = [
                        (var, _score_variation_against_text(var, user_text_clean, user_tokens))
                        for var in all_variations
                        if var.get("attributes")
                    ]
                    max_score = max((s for _, s in scores), default=0)
                    if max_score > 0:
                        matched = [var for var, s in scores if s == max_score]
                    else:
                        matched = all_variations
                else:
                    matched = _filter_variations_by_entities(all_variations, entities)

                    if len(matched) != 1:
                        user_text_lower = message.lower()
                        candidates = matched if len(matched) > 1 else all_variations
                        text_matched = []
                        for var in candidates:
                            var_attrs = var.get("attributes", [])
                            if not var_attrs:
                                continue
                            all_options_found = all(
                                a.get("option", "").lower() in user_text_lower
                                for a in var_attrs
                                if a.get("option")
                            )
                            if all_options_found:
                                text_matched.append(var)
                        if text_matched and len(text_matched) < len(candidates):
                            matched = text_matched

                if len(matched) == 1:
                    # ── Variant resolved — DO NOT place order. Enter confirmation flow. ──
                    _resolved_variation = matched[0]
                    _resolved_variation_id = _resolved_variation["id"]
                    logger.info(f"Step 3.55: Resolved to variation_id={_resolved_variation_id}")

                    # Build variant label and get price for display
                    _variant_label = " / ".join(
                        a.get("option", "") for a in _resolved_variation.get("attributes", []) if a.get("option")
                    )
                    _variant_price = _resolved_variation.get("sale_price") or _resolved_variation.get("price") or _resolved_variation.get("regular_price") or ""

                    if not _var_quantity:
                        # Quantity missing — ask for quantity, show what was selected + price
                        logger.info(f"Step 3.55: Variant resolved, asking for quantity | price={_variant_price}")
                        _price_line = f"\n**Unit Price:** ${_variant_price}" if _variant_price else ""
                        elapsed = time.time() - start_time
                        return jsonify({
                            "success": True,
                            "bot_message": (
                                f"Great choice! Here's what you selected:\n\n"
                                f"**Product:** {_var_product_name}\n"
                                f"**Variant:** {_variant_label}"
                                f"{_price_line}\n\n"
                                f"How many would you like to order? 🛒"
                            ),
                            "intent": "guided_flow",
                            "products": [],
                            "filters_applied": {},
                            "suggestions": ["1", "5", "10", "25"],
                            "session_id": session_id,
                            "metadata": {
                                "flow_state": FlowState.AWAITING_QUANTITY.value,
                                "pending_product_id": _var_product_id,
                                "pending_product_name": _var_product_name,
                                "pending_variation_id": _resolved_variation_id,
                                "response_time_ms": round(elapsed * 1000),
                            },
                            "flow_state": FlowState.AWAITING_QUANTITY.value,
                            "pagination": _default_pagination(page),
                        }), 200

                    # Quantity known — go straight to shipping address
                    logger.info(f"Step 3.55: Variant resolved with quantity={_var_quantity}, proceeding to shipping")
                    # Fetch customer address
                    shipping_address = None
                    try:
                        cust_call = WooAPICall(
                            method="GET",
                            endpoint=f"{WOO_BASE_URL}/customers/{customer_id}",
                            params={},
                            body={},
                            description=f"Fetch customer {customer_id} shipping address (from Step 3.55)",
                        )
                        cust_resp = woo_client.execute(cust_call)
                        if cust_resp.get("success") and isinstance(cust_resp.get("data"), dict):
                            shipping_address = cust_resp["data"].get("shipping", {})
                    except Exception as exc:
                        logger.warning(f"Step 3.55: Could not fetch customer address | error={exc}")

                    has_address = bool(
                        shipping_address
                        and (shipping_address.get("address_1") or shipping_address.get("city"))
                    )

                    base_meta = {
                        "pending_product_id": _var_product_id,
                        "pending_product_name": _var_product_name,
                        "pending_quantity": _var_quantity,
                        "pending_variation_id": _resolved_variation_id,
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
                        elapsed = time.time() - start_time
                        return jsonify({
                            "success": True,
                            "bot_message": (
                                f"Your shipping address on file:\n\n"
                                f"📦 **{addr_display}**\n\n"
                                "Would you like to ship to this address, or use a different one?"
                            ),
                            "intent": "guided_flow",
                            "products": [],
                            "filters_applied": {},
                            "suggestions": ["Yes, use this address", "Change address", "Cancel"],
                            "session_id": session_id,
                            "metadata": {**base_meta, "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value},
                            "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
                            "pagination": _default_pagination(page),
                        }), 200
                    else:
                        elapsed = time.time() - start_time
                        return jsonify({
                            "success": True,
                            "bot_message": "No shipping address is on file. Please type your shipping address (street, city, state, zip code):",
                            "intent": "guided_flow",
                            "products": [],
                            "filters_applied": {},
                            "suggestions": [],
                            "session_id": session_id,
                            "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
                            "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                            "pagination": _default_pagination(page),
                        }), 200

                else:
                    # Multiple or no exact match — ask user to narrow down or re-select
                    logger.info(f"Step 3.55: Could not resolve to single variation | matched={len(matched)} of {len(all_variations)}")
                    # Collect which attributes have been pinned down
                    resolved_attributes = {}
                    if len(matched) > 1 and len(matched) < len(all_variations):
                        attr_values = {}
                        for v in matched:
                            for a in v.get("attributes", []):
                                name = a.get("name", "")
                                opt = a.get("option", "")
                                if name and opt:
                                    attr_values.setdefault(name, set()).add(opt)
                        for attr_name, options in attr_values.items():
                            if len(options) == 1:
                                resolved_attributes[attr_name] = list(options)[0]
                        logger.info(f"Step 3.55: Resolved attributes so far: {resolved_attributes}")

                    # Merge in any previously resolved attributes from prior turns
                    if prev_resolved:
                        for k, v in prev_resolved.items():
                            if k not in resolved_attributes:
                                resolved_attributes[k] = v
                        logger.info(f"Step 3.55: Merged with previous resolved_attributes: {resolved_attributes}")

                    # Use session-cached parent_raw — avoids an extra WooCommerce API call
                    _session_parent = sessions.get(session_id, {}).get("variation_cache", {}).get(str(_var_product_id), {}).get("parent_raw")
                    if _session_parent:
                        parent_raw = _session_parent
                        logger.info(f"Step 3.55: Using session-cached parent_raw — skipping parent product API call")
                    else:
                        parent_call = WooAPICall(
                            method="GET",
                            endpoint=f"{WOO_BASE_URL}/products/{_var_product_id}",
                            params={},
                            description=f"Fetch parent product '{_var_product_name}' for variant re-prompt",
                        )
                        parent_resp = woo_client.execute(parent_call)
                        parent_raw = parent_resp.get("data", {}) if parent_resp.get("success") else {}
                    if len(matched) > 1 and len(matched) < len(all_variations):
                        attr_values_all = {}
                        for v in matched:
                            for a in v.get("attributes", []):
                                name = a.get("name", "")
                                opt = a.get("option", "")
                                if name and opt:
                                    attr_values_all.setdefault(name, set()).add(opt)
                        
                        # Exclude already-resolved attributes so we never ask the user
                        # for an attribute they already specified in a prior turn.
                        _already_resolved = {k.lower() for k in resolved_attributes}
                        ambiguous = {
                            k: sorted(v)
                            for k, v in attr_values_all.items()
                            if len(v) > 1 and k.lower() not in _already_resolved
                        }
                        if ambiguous:
                            lines = [f"Great, I found **{_var_product_name}** in your selected options! I just need a bit more info:\n"]
                            for attr_name, options in ambiguous.items():
                                lines.append(f"• **{attr_name}:** {', '.join(options)}")
                            lines.append("\nWhich combination would you like?")
                            prompt_msg = "\n".join(lines)
                        else:
                            variation_labels = [
                                " / ".join(a.get("option", "") for a in v.get("attributes", []) if a.get("option"))
                                for v in matched
                            ]
                            prompt_msg = (
                                f"I found **{len(matched)}** variants of **{_var_product_name}** matching your description:\n\n"
                                + "\n".join(f"• {lbl}" for lbl in variation_labels if lbl)
                                + "\n\nWhich one would you like?"
                            )
                    else:
                        prompt_msg = _build_variant_prompt(parent_raw, _var_product_name)
                        if len(all_variations) > 0:
                            prompt_msg = f"Sorry, I couldn't find that exact variant. " + prompt_msg
                    elapsed = time.time() - start_time
                    return jsonify({
                        "success": True,
                        "bot_message": prompt_msg,
                        "intent": "guided_flow",
                        "products": [],
                        "filters_applied": {},
                        "suggestions": [],
                        "session_id": session_id,
                        "metadata": {
                            "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                            "pending_product_id": _var_product_id,
                            "pending_product_name": _var_product_name,
                            "pending_quantity": _var_quantity,
                            "resolved_attributes": resolved_attributes,
                            "response_time_ms": round(elapsed * 1000),
                        },
                        "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                        "pagination": _default_pagination(page),
                    }), 200

    # ─── Step 3.6: QUICK_ORDER / ORDER_ITEM / PLACE_ORDER — create order from matched product ───
    if intent in (Intent.QUICK_ORDER, Intent.ORDER_ITEM, Intent.PLACE_ORDER) and customer_id and entities.quantity:
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
            }
            all_products_raw.append(_injected)
            _order_product_raw = _injected
            logger.info(f"Step 3.6: Injected minimal product dict into all_products_raw (count={len(all_products_raw)})")
        else:
            logger.warning("Step 3.6: No product found to order (all_products_raw empty, no last_product_ctx)")

        if _order_product_id:
            _order_variation_id = entities.variation_id
            _product_type = (_order_product_raw or {}).get("type", "simple")

            if _product_type == "variable":
                has_attrs = any([entities.color_tone, entities.finish, entities.tile_size, entities.sample_size])

                if not _order_variation_id and not has_attrs:
                    logger.info(f"Step 3.6: Variable product with no variant info | product_id={_order_product_id}")
                    prompt_msg = _build_variant_prompt(_order_product_raw or {}, _order_product_name)
                    # ── Write to session-side variation cache ──
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
                        "filters_applied": {},
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
                        "pagination": _default_pagination(page),
                    }), 200

                elif not _order_variation_id and has_attrs:
                    logger.info(f"Step 3.6: Variable product with attributes, resolving variation | product_id={_order_product_id}")
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
                                prompt_msg = _build_variant_prompt(_order_product_raw or {}, _order_product_name)
                            elapsed = time.time() - start_time
                            return jsonify({
                                "success": True,
                                "bot_message": prompt_msg,
                                "intent": INTENT_LABELS.get(intent, "order"),
                                "products": [format_product(_order_product_raw)] if _order_product_raw else [],
                                "filters_applied": {},
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
                                "pagination": _default_pagination(page),
                            }), 200

            # For simple products or resolved variations from Step 3.6 — go to shipping
            # instead of placing order directly
            logger.info(f"Step 3.6: Product resolved, proceeding to shipping | product_id={_order_product_id} | variation_id={_order_variation_id} | quantity={entities.quantity}")

            # Fetch customer address
            shipping_address = None
            try:
                cust_call = WooAPICall(
                    method="GET",
                    endpoint=f"{WOO_BASE_URL}/customers/{customer_id}",
                    params={},
                    body={},
                    description=f"Fetch customer {customer_id} shipping address (from Step 3.6)",
                )
                cust_resp = woo_client.execute(cust_call)
                if cust_resp.get("success") and isinstance(cust_resp.get("data"), dict):
                    shipping_address = cust_resp["data"].get("shipping", {})
            except Exception as exc:
                logger.warning(f"Step 3.6: Could not fetch customer address | error={exc}")

            has_address = bool(
                shipping_address
                and (shipping_address.get("address_1") or shipping_address.get("city"))
            )

            base_meta = {
                "pending_product_id": _order_product_id,
                "pending_product_name": _order_product_name,
                "pending_quantity": entities.quantity,
                "pending_variation_id": _order_variation_id,
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
                elapsed = time.time() - start_time
                return jsonify({
                    "success": True,
                    "bot_message": (
                        f"Your shipping address on file:\n\n"
                        f"📦 **{addr_display}**\n\n"
                        "Would you like to ship to this address, or use a different one?"
                    ),
                    "intent": "guided_flow",
                    "products": [],
                    "filters_applied": {},
                    "suggestions": ["Yes, use this address", "Change address", "Cancel"],
                    "session_id": session_id,
                    "metadata": {**base_meta, "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value},
                    "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
                    "pagination": _default_pagination(page),
                }), 200
            else:
                elapsed = time.time() - start_time
                return jsonify({
                    "success": True,
                    "bot_message": "No shipping address is on file. Please type your shipping address (street, city, state, zip code):",
                    "intent": "guided_flow",
                    "products": [],
                    "filters_applied": {},
                    "suggestions": [],
                    "session_id": session_id,
                    "metadata": {**base_meta, "flow_state": FlowState.AWAITING_NEW_ADDRESS.value},
                    "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
                    "pagination": _default_pagination(page),
                }), 200
        else:
            logger.warning("Step 3.6: Skipped order creation (no product_id resolved)")

    # ─── Step 3.7: Variation product handling ───
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

            category_mismatch_msg = ""
            if entities.category_name:
                product_cats = [c.lower() for c in parent_formatted.get("categories", [])]
                requested_cat = entities.category_name.lower()
                if product_cats and requested_cat not in product_cats:
                    actual_cats = ", ".join(parent_formatted.get("categories", []))
                    category_mismatch_msg = (
                        f"**{parent_formatted['name']}** is not available in the "
                        f"**{entities.category_name}** category — it's part of "
                        f"**{actual_cats}**."
                    )
                    logger.info(
                        f"Step 3.7: Category mismatch detected | "
                        f"product={parent_formatted['name']} | "
                        f"requested_category={entities.category_name} | "
                        f"actual_categories={actual_cats}"
                    )
                    entities.category_name = actual_cats

            has_attributes = any([
                entities.finish, entities.color_tone, entities.tile_size,
                entities.thickness, entities.visual, entities.origin,
            ])

            if variations_raw and has_attributes:
                filtered_vars = _filter_variations_by_entities(variations_raw, entities)
                variation_products = [format_variation(v, parent_product_raw) for v in filtered_vars]
                products = [parent_formatted] + variation_products
            elif variations_raw:
                variation_products = [format_variation(v, parent_product_raw) for v in variations_raw]
                products = [parent_formatted] + variation_products
            else:
                products = [parent_formatted]

            bot_message = generate_bot_message(intent, entities, products, confidence, order_data)

            if category_mismatch_msg:
                bot_message = f"⚠️ {category_mismatch_msg}\n\n{bot_message}"

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
                "category_mismatch": bool(category_mismatch_msg),
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
                "pagination": _build_pagination(page, api_responses, api_calls_to_execute),
            }), 200

    # ─── Step 3.8: LLM Retry on Empty Search Results ───
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
                corrected_term = llm_retry_result["corrected_term"]
                logger.info(f"Step 3.8: LLM suggested correction | corrected_term={corrected_term}")
                
                corrected_result = classify(corrected_term)
                corrected_api_calls = build_api_calls(corrected_result)
                corrected_responses = woo_client.execute_all(corrected_api_calls)
                
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
                    all_products_raw = corrected_products_raw
                    logger.info(f"Step 3.8: LLM retry successful | found {len(all_products_raw)} products")
                else:
                    logger.info("Step 3.8: LLM retry still returned 0 products")
            
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
                    "pagination": _default_pagination(page),
                }), 200

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
            if "featured_image" in p:
                products.append(format_custom_product(p))
            else:
                products.append(format_product(p))

    products = [p for p in products if p.get("name")]
    logger.info(f"Step 4: Formatted {len(products)} products")

    # ─── Step 5: Generate bot message ───
    # Extract total_items from pagination so the bot message shows the real
    # total (e.g. "26 products") instead of just the page size (e.g. "4 products")
    _pagination_data = _build_pagination(page, api_responses, api_calls_to_execute)
    _total_items_for_msg = _pagination_data.get("total_items")

    bot_message = generate_bot_message(intent, entities, products, confidence, order_data, total_items=_total_items_for_msg)
        
    if intent in ORDER_CREATE_INTENTS and order_data:
        placed_order = order_data[-1]
        if products:
            used_product_name = products[0]["name"]
        elif placed_order.get("line_items"):
            used_product_name = placed_order["line_items"][0].get("name") or "your item"
        else:
            used_product_name = "your item"
        
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
        
        if used_product_name == "your item":
            logger.warning("Step 5: Used fallback 'your item' - no product name available from products[] or line_items[]")
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
        "pagination": _pagination_data,
    }

    # ─── Step 5.5: Detect when quantity is needed for ordering ───
    if intent in ORDER_CREATE_INTENTS and not entities.quantity and products:
        product = products[0]
        if product.get("type") == "variable":
            _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
            _variations_for_cache = [p for p in all_products_raw if p.get("parent_id") == product.get("id")]
            prompt_msg = _build_variant_prompt(_raw_for_prompt, product["name"])
            # ── Write to session-side variation cache on first fetch ──
            if session_id and session_id in sessions:
                _pid = str(product.get("id"))
                sessions[session_id].setdefault("variation_cache", {})[_pid] = {
                    "variations": _variations_for_cache,
                    "parent_raw": _raw_for_prompt,
                }
                logger.info(f"Step 5.5: Cached {len(_variations_for_cache)} variations for product_id={_pid} in session")
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": prompt_msg,
                "intent": INTENT_LABELS.get(intent, "order"),
                "products": products[:1],
                "filters_applied": {},
                "suggestions": [],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                    "pending_product_id": product.get("id"),
                    "pending_product_name": product["name"],
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pagination": _default_pagination(page),
            }), 200
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
                "response_time_ms": round(elapsed * 1000),
            },
            "flow_state": FlowState.AWAITING_QUANTITY.value,
            "pagination": _default_pagination(page),
        }), 200

    # After quantity check, also check for variant requirement
    if intent in ORDER_CREATE_INTENTS and entities.quantity and products and not order_data:
        product = products[0]
        if product.get("type") == "variable":
            _raw_for_prompt = next((p for p in all_products_raw if not p.get("parent_id")), {})
            _variations_for_cache = [p for p in all_products_raw if p.get("parent_id") == product.get("id")]
            prompt_msg = _build_variant_prompt(_raw_for_prompt, product["name"])
            # ── Write to session-side variation cache on first fetch ──
            if session_id and session_id in sessions:
                _pid = str(product.get("id"))
                sessions[session_id].setdefault("variation_cache", {})[_pid] = {
                    "variations": _variations_for_cache,
                    "parent_raw": _raw_for_prompt,
                }
                logger.info(f"Step 5.5: Cached {len(_variations_for_cache)} variations for product_id={_pid} in session")
            elapsed = time.time() - start_time
            return jsonify({
                "success": True,
                "bot_message": prompt_msg,
                "intent": INTENT_LABELS.get(intent, "order"),
                "products": products[:1],
                "filters_applied": {},
                "suggestions": [],
                "session_id": session_id,
                "metadata": {
                    "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                    "pending_product_id": product.get("id"),
                    "pending_product_name": product["name"],
                    "pending_quantity": entities.quantity,
                    "response_time_ms": round(elapsed * 1000),
                },
                "flow_state": FlowState.AWAITING_VARIANT_SELECTION.value,
                "pagination": _default_pagination(page),
            }), 200

    # ─── Step 10.5: After successful response, add "anything else?" flow ───
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