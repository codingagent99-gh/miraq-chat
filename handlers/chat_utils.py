"""
handlers/chat_utils.py — Shared helper functions used across chat handlers.
"""

import re
from datetime import datetime, timezone

from flask import jsonify
from config.settings import DEFAULT_PER_PAGE
from app_config import WOO_BASE_URL
from models import WooAPICall
from woo_client import woo_client
from conversation_flow import FlowState
from chat_logger import get_logger, sanitize_log_string

logger = get_logger("miraq_chat")

_TOKEN_OVERLAP_THRESHOLD = 0.5
_STRIP_QUOTES_RE = re.compile(r'["\'\u201c\u201d\u2018\u2019]')
_TOKENIZE_RE = re.compile(r'[\w/]+')


def default_pagination(page: int = 1) -> dict:
    """Return a default pagination object for responses without product lists."""
    return {
        "page": page,
        "per_page": 0,
        "total_items": 0,
        "total_pages": 1,
        "has_more": False,
    }


def build_pagination(page: int, api_responses: list, api_calls: list) -> dict:
    """Build pagination object from API responses and call params."""
    total_items = None
    total_pages = None
    per_page = DEFAULT_PER_PAGE

    if api_calls:
        per_page = int(api_calls[0].params.get("per_page", DEFAULT_PER_PAGE))

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


def fetch_unit_price(product_id, variation_id=None) -> str:
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
        logger.warning(f"fetch_unit_price failed | error={exc}")
    return "N/A"


def format_order_for_frontend(order: dict) -> dict:
    """Map WooCommerce order dict to the frontend Order interface."""
    line_items = order.get("line_items", [])
    items = [
        {
            "name": item.get("name", "Unknown Item"),
            "quantity": item.get("quantity", 1),
            "price": float(item.get("price", 0) or 0),
            "total": float(item.get("total", 0) or 0),
            "sku": item.get("sku", ""),
        }
        for item in line_items
    ]
    try:
        total = float(order.get("total", 0) or 0)
    except (ValueError, TypeError):
        total = 0.0

    return {
        "id": order.get("id"),
        "order_number": str(order.get("number") or order.get("id", "")),
        "status": order.get("status", "unknown"),
        "currency": order.get("currency_symbol", "$"),
        "total": total,
        "subtotal": order.get("subtotal", "0"),
        "shipping_total": order.get("shipping_total", "0"),
        "date_created": order.get("date_created", ""),
        "date_paid": order.get("date_paid"),
        "payment_method": order.get("payment_method_title", ""),
        "items": items,
        "item_count": len(items),
        "shipping": order.get("shipping", {}),
        "billing": order.get("billing", {}),
    }


def build_variant_prompt(product_raw: dict, product_name: str) -> str:
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
            lines.append(f"🎨 **{name}:** {', '.join(options)}")
    lines.append("\nWhich combination would you like?")
    return "\n".join(lines)


def score_variation_against_text(var: dict, user_text_clean: str, user_tokens: set) -> int:
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


def fetch_shipping_address(customer_id: int, step_label: str) -> dict | None:
    """Fetch a customer's shipping address from WooCommerce. Returns dict or None."""
    try:
        cust_call = WooAPICall(
            method="GET",
            endpoint=f"{WOO_BASE_URL}/customers/{customer_id}",
            params={},
            body={},
            description=f"Fetch customer {customer_id} shipping address ({step_label})",
        )
        cust_resp = woo_client.execute(cust_call)
        if cust_resp.get("success") and isinstance(cust_resp.get("data"), dict):
            return cust_resp["data"].get("shipping", {})
    except Exception as exc:
        logger.warning(f"{step_label}: Could not fetch customer address | error={exc}")
    return None


def shipping_address_response(
    session_id: str,
    page: int,
    start_time: float,
    base_meta: dict,
    shipping_address: dict | None,
) -> object:
    """
    Build the jsonify response for the shipping address confirmation step.
    Returns a Flask response asking the user to confirm or enter an address.
    """
    import time
    has_address = bool(
        shipping_address
        and (shipping_address.get("address_1") or shipping_address.get("city"))
    )

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
        base_meta["flow_state"] = FlowState.AWAITING_SHIPPING_CONFIRM.value
        base_meta["response_time_ms"] = round((time.time() - start_time) * 1000)
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
            "metadata": base_meta,
            "flow_state": FlowState.AWAITING_SHIPPING_CONFIRM.value,
            "pagination": default_pagination(page),
        }), 200
    else:
        base_meta["flow_state"] = FlowState.AWAITING_NEW_ADDRESS.value
        base_meta["response_time_ms"] = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "No shipping address is on file. Please type your shipping address (street, city, state, zip code):",
            "intent": "guided_flow",
            "products": [],
            "suggestions": [],
            "session_id": session_id,
            "metadata": base_meta,
            "flow_state": FlowState.AWAITING_NEW_ADDRESS.value,
            "pagination": default_pagination(page),
        }), 200
