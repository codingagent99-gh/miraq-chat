"""
handlers/chat_utils.py — Shared helper functions used across chat handlers.
"""

import re

from flask import jsonify
from app_config import WOO_BASE_URL, DEFAULT_PER_PAGE, get_currency_symbol
from models import WooAPICall
from woo_client import woo_client
from conversation_flow import FlowState
from chat_logger import get_logger

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
        "currency": order.get("currency_symbol") or get_currency_symbol(),
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

def build_out_of_stock_response(product_name: str, product_raw: dict, intent, session_id: str, page: int, start_time: float):
    """Generates a standardized 'Out of Stock' Flask response to kill an ordering flow."""
    import time
    from flask import jsonify
    from conversation_flow import FlowState
    from formatters import format_product
    
    elapsed = time.time() - start_time
    
    # Dynamically build a foolproof suggestion based on the product's category
    suggestions = ["Browse all categories"]
    if product_raw and product_raw.get("categories"):
        # WooCommerce categories can be dicts or strings depending on the endpoint format
        first_cat = product_raw["categories"][0]
        cat_name = first_cat.get("name", "") if isinstance(first_cat, dict) else first_cat
        if cat_name:
            suggestions.insert(0, f"Show other {cat_name} options")
            
    return jsonify({
        "success": True,
        "bot_message": f"I'm so sorry, but **{product_name}** is currently out of stock! 😔\n\nPlease feel free to explore our other options.",
        "intent": intent.value,
        "products": [format_product(product_raw)] if product_raw else [],
        "suggestions": suggestions,
        "session_id": session_id,
        "metadata": {
            "flow_state": FlowState.IDLE.value,
            "response_time_ms": round(elapsed * 1000),
        },
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200

def build_variant_prompt(parent_raw: dict, product_name: str, resolved_attributes: dict = None, variations_list: list = None) -> str:
    """Builds a friendly prompt listing the available variation options."""
    import logging
    log = logging.getLogger("miraq_chat")
    
    resolved = resolved_attributes or {}
    resolved_keys_lower = {k.lower() for k in resolved.keys()}
    
    missing_attrs = {}
    
    # ── METHOD 1: Deduce options directly from the variations array ──
    variations = variations_list if variations_list is not None else parent_raw.get("variations", [])
    log.info(f"build_variant_prompt: Scanning {len(variations)} variations for {product_name}")
    
    for v in variations:
        if not isinstance(v, dict):
            continue
        v_attrs = v.get("attributes", {})
        
        # Custom API format
        if isinstance(v_attrs, dict):
            for k, val in v_attrs.items():
                if not val: continue
                nice_name = k.replace("pa_", "").replace("-", " ").title()
                if nice_name.lower() not in resolved_keys_lower:
                    missing_attrs.setdefault(nice_name, set()).add(val)
                    
        # Standard WC fallback
        elif isinstance(v_attrs, list):
            for a in v_attrs:
                name = a.get("name", "")
                val = a.get("option", "")
                if name and val and name.lower() not in resolved_keys_lower:
                    missing_attrs.setdefault(name, set()).add(val)

    # ── METHOD 2: Supplement with parent-level "Any" variation axes ──
    # ALWAYS run this to fill in axes that WooCommerce left blank ("") on variations!
    attributes = parent_raw.get("attributes", [])
    if isinstance(attributes, list):
        for attr in attributes:
            if isinstance(attr, dict) and attr.get("variation") is True:
                name = attr.get("name", "")
                nice_name = name.replace("pa_", "").replace("-", " ").title() if name.startswith("pa_") else name.title()
                
                if nice_name and nice_name.lower() not in resolved_keys_lower:
                    opts = attr.get("options", [])
                    if opts:
                        # Merge parent options to fill the gaps
                        if nice_name not in missing_attrs:
                            missing_attrs[nice_name] = set()
                        for o in opts:
                            if str(o).strip():
                                missing_attrs[nice_name].add(str(o).strip())

    log.info(f"build_variant_prompt: Extracted attributes = {missing_attrs}")

    # Clean up the values into nice, readable strings
    for k, v in missing_attrs.items():
        if isinstance(v, set):
            cleaned_vals = [val.replace("-", " ").title() for val in v]
            missing_attrs[k] = sorted(list(set(cleaned_vals)))

    if not missing_attrs:
        return f"I'd love to order **{product_name}** for you! Which variant would you like? Please specify your options."

    msg = f"I'd love to order **{product_name}** for you! To make sure I get the right one, please choose from the following options:\n\n"
    for name, options in missing_attrs.items():
        if options:
            display_opts = options[:8]
            opts_str = ", ".join(display_opts)
            if len(options) > 8:
                opts_str += f", and {len(options) - 8} more..."
            msg += f"• **{name}:** {opts_str}\n"

    return msg

def score_variation_against_text(variation: dict, user_text_clean: str, user_tokens: set) -> int:
    """
    Scores how well a variation matches the user's input text.
    Handles both standard WooCommerce attribute lists and flat custom API dicts.
    """
    score = 0
    attrs = variation.get("attributes", [])
    
    # ── FORMAT 1: Custom API (Flat Dictionary) ──
    # Example: {"pa_colors": "waterfall-havana", "pa_finish": "matte"}
    if isinstance(attrs, dict):
        for key, val in attrs.items():
            if not val: continue
            
            # Clean up the value (e.g. "waterfall-havana" -> "waterfall havana")
            opt_clean = str(val).replace("-", " ").lower()
            
            # Exact phrase match gets high score
            if opt_clean in user_text_clean:
                score += 10
            else:
                # Partial token overlap gets partial score
                opt_tokens = set(opt_clean.split())
                overlap = opt_tokens & user_tokens
                score += len(overlap)

    # ── FORMAT 2: Standard WooCommerce (List of Dicts) ──
    # Example: [{"name": "Color", "option": "Havana"}]
    elif isinstance(attrs, list):
        for attr in attrs:
            if not isinstance(attr, dict): continue
            
            opt = attr.get("option", "")
            if not opt: continue
            
            opt_clean = str(opt).replace("-", " ").lower()
            
            if opt_clean in user_text_clean:
                score += 10
            else:
                opt_tokens = set(opt_clean.split())
                overlap = opt_tokens & user_tokens
                score += len(overlap)

    return score
