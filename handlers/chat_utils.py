"""
handlers/chat_utils.py — Shared helper functions used across chat handlers.
"""

import re

from flask import jsonify
from app_config import DEFAULT_PER_PAGE, get_currency_symbol
from woo_client import woo_client
from conversation_flow import FlowState
from chat_logger import get_logger
from ecommerce import endpoints

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
            call = endpoints.fetch_variant(product_id, variation_id, description=f"Fetch variation {variation_id} price")
            resp = woo_client.execute(call)
            if resp.get("success") and isinstance(resp.get("data"), dict):
                d = resp["data"]
                return d.get("price") or "N/A"
        elif product_id:
            call = endpoints.fetch_product(product_id, description=f"Fetch product {product_id} price")
            resp = woo_client.execute(call)
            if resp.get("success") and isinstance(resp.get("data"), dict):
                d = resp["data"]
                return d.get("price") or "N/A"
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
        "date_created": order.get("created_at", ""),
        "date_paid": order.get("paid_at"),
        "payment_method": order.get("payment_method_label", ""),
        "items": items,
        "item_count": len(items),
        "shipping": order.get("shipping_address", {}),
        "billing": order.get("billing_address", {}),
    }

def build_out_of_stock_response(product_name: str, product_raw: dict, intent, session_id: str, page: int, start_time: float):
    """Generates a standardized 'Out of Stock' Flask response to kill an ordering flow."""
    import time
    from flask import jsonify
    from conversation_flow import FlowState
    from formatters import format_product
    
    elapsed = time.time() - start_time
    
    suggestions = ["Browse all categories"]
    if product_raw and product_raw.get("categories"):
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


def _compute_variant_options(
    parent_raw: dict,
    resolved_attributes: dict = None,
    variations_list: list = None,
    display_to_slug: dict = None,  # {taxonomy: {display_name_lower: slug}}
) -> dict:
    """
    Core computation shared by build_variant_prompt and the API response builder.

    Returns a dict of unresolved variation axes → sorted list of available options.
    Example: {"Colors": ["CORAL Argento", ...], "Finish": ["Anti-Slip", ...]}

    Rules (mirrors WooCommerce frontend behaviour):
    - Axes WITH explicit parent options → use parent options only (source of truth).
    - Axes WITHOUT parent options (wildcard "Any") → scan variation records instead,
      filtered to those consistent with already-resolved attributes.

    display_to_slug: pre-built {taxonomy: {display_name_lower: slug}} from
    all_attributes_raw.  When supplied, _matches_resolved compares resolved
    display names against variation option slugs correctly rather than doing a
    lossy string comparison.
    """
    import logging
    log = logging.getLogger("miraq_chat")

    resolved = resolved_attributes or {}
    resolved_keys_lower = {k.lower() for k in resolved.keys()}
    missing_attrs: dict = {}

    # ── STEP 1: Parent options (authoritative for non-wildcard axes) ──
    parent_defined_axes: set = set()
    attributes = parent_raw.get("attributes", [])
    if isinstance(attributes, list):
        for attr in attributes:
            if not (isinstance(attr, dict) and attr.get("variation") is True):
                continue
            name = attr.get("name", "")
            nice_name = (
                name.replace("pa_", "").replace("-", " ").title()
                if name.startswith("pa_") else name.title()
            )
            if not nice_name or nice_name.lower() in resolved_keys_lower:
                continue
            opts = [str(o).strip() for o in attr.get("options", []) if str(o).strip()]
            if opts:
                parent_defined_axes.add(nice_name.lower())
                missing_attrs[nice_name] = set(opts)

    # ── STEP 2: Variation scan (wildcard axes only) ──
    variations = variations_list if variations_list is not None else parent_raw.get("variations", [])
    log.info(f"build_variant_prompt: Scanning {len(variations)} variations for product")

    def _matches_resolved(var: dict) -> bool:
        """
        Returns True when the variation is consistent with all already-resolved
        attributes.

        WC REST API stores variation attribute values as slugs (e.g. "chip-card")
        while resolved_attributes holds display names (e.g. "Chip Card") as shown
        to the user.  When display_to_slug is available we convert the display name
        to its canonical slug before comparing — no string-normalisation guessing.
        Falls back to a slugified comparison when the taxonomy isn't in the lookup.
        """
        if not resolved:
            return True

        v_attrs = var.get("options") or var.get("attributes", {})
        # Build {nice_name_lower: option_slug} from variation
        var_map: dict = {}
        if isinstance(v_attrs, list):
            for a in v_attrs:
                if isinstance(a, dict):
                    var_map[a.get("name", "").lower()] = a.get("value", "")
        elif isinstance(v_attrs, dict):
            for k, v in v_attrs.items():
                nice = k.replace("pa_", "").replace("-", " ").title().lower()
                var_map[nice] = str(v)

        for res_key, res_val in resolved.items():
            actual_slug = var_map.get(res_key.lower())
            if actual_slug is None:
                continue  # axis not present on this variation (wildcard) — skip

            if display_to_slug:
                taxonomy = f"pa_{res_key.lower().replace(' ', '-')}"
                term_map = display_to_slug.get(taxonomy, {})
                expected_slug = term_map.get(res_val.lower(), "")
                if expected_slug:
                    if expected_slug != actual_slug:
                        return False
                    continue
            # Fallback: slugify both sides (strips hyphens, quotes, spaces)
            if re.sub(r'[^a-z0-9]+', '', res_val.lower()) != re.sub(r'[^a-z0-9]+', '', actual_slug.lower()):
                return False

        return True

    filtered = [v for v in variations if isinstance(v, dict) and _matches_resolved(v)]
    log.info(
        f"build_variant_prompt: {len(filtered)}/{len(variations)} variations "
        f"match resolved={list(resolved.keys())}"
    )

    for v in filtered:
        v_attrs = v.get("options") or v.get("attributes", {})

        if isinstance(v_attrs, dict):
            for k, val in v_attrs.items():
                if not val:
                    continue
                nice_name = k.replace("pa_", "").replace("-", " ").title()
                if nice_name.lower() in resolved_keys_lower or nice_name.lower() in parent_defined_axes:
                    continue
                missing_attrs.setdefault(nice_name, set()).add(val)

        elif isinstance(v_attrs, list):
            for a in v_attrs:
                name = a.get("name", "")
                val = a.get("value", "")
                if not (name and val):
                    continue
                nice_name = (
                    name.replace("pa_", "").replace("-", " ").title()
                    if name.startswith("pa_") else name.title()
                )
                if nice_name.lower() in resolved_keys_lower or nice_name.lower() in parent_defined_axes:
                    continue
                missing_attrs.setdefault(nice_name, set()).add(val)

    log.info(f"build_variant_prompt: Extracted attributes = {missing_attrs}")

    result: dict = {}
    for axis, vals in missing_attrs.items():
        cleaned = sorted({str(v).replace("-", " ").title() for v in vals})
        if cleaned:
            result[axis] = cleaned

    return result


def build_variant_prompt(
    parent_raw: dict,
    product_name: str,
    resolved_attributes: dict = None,
    variations_list: list = None,
    display_to_slug: dict = None,  # passed through to _compute_variant_options
) -> str:
    """Builds a friendly markdown prompt listing the available variation options."""
    options = _compute_variant_options(
        parent_raw, resolved_attributes, variations_list, display_to_slug
    )

    if not options:
        return (
            f"I'd love to order **{product_name}** for you! "
            "Which variant would you like? Please specify your options."
        )

    msg = (
        f"I'd love to order **{product_name}** for you! "
        "To make sure I get the right one, please choose from the following options:\n\n"
    )
    for axis, opts in options.items():
        msg += f"• **{axis}:** {', '.join(opts)}\n"

    return msg

def score_variation_against_text(variation: dict, user_text_clean: str, user_tokens: set) -> int:
    """
    Scores how well a variation matches the user's input text.
    Handles both standard WooCommerce attribute lists and flat custom API dicts.
    """
    score = 0
    attrs = variation.get("options") or variation.get("attributes", [])
    
    # ── FORMAT 1: Custom API (Flat Dictionary) ──
    if isinstance(attrs, dict):
        for key, val in attrs.items():
            if not val: continue
            opt_clean = str(val).replace("-", " ").lower()
            if opt_clean in user_text_clean:
                score += 10
            else:
                opt_tokens = set(opt_clean.split())
                overlap = opt_tokens & user_tokens
                score += len(overlap)

    # ── FORMAT 2: Standard WooCommerce (List of Dicts) ──
    elif isinstance(attrs, list):
        for attr in attrs:
            if not isinstance(attr, dict): continue
            opt = attr.get("value", "")
            if not opt: continue
            opt_clean = str(opt).replace("-", " ").lower()
            if opt_clean in user_text_clean:
                score += 10
            else:
                opt_tokens = set(opt_clean.split())
                overlap = opt_tokens & user_tokens
                score += len(overlap)

    return score
