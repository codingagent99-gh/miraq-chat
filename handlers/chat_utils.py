"""
handlers/chat_utils.py — Shared helper functions used across chat handlers.
"""

import re

from flask import jsonify
from app_config import DEFAULT_PER_PAGE, get_currency_symbol
from models import WooAPICall
from woo_client import woo_client
from conversation_flow import FlowState
from chat_logger import get_logger
from ecommerce import endpoints
from store_registry import get_store_loader

logger = get_logger("miraq_chat")

_TOKEN_OVERLAP_THRESHOLD = 0.5
_STRIP_QUOTES_RE = re.compile(r'["\'\u201c\u201d\u2018\u2019]')
_TOKENIZE_RE = re.compile(r'[\w/]+')


def _attribute_key_candidates(attr_name: str) -> list[str]:
    key = str(attr_name or "").strip().lower().replace("_", "-")
    if key.startswith("attribute_"):
        key = key[len("attribute_"):]
    if key.startswith("pa_"):
        key = key[3:]
    candidates = [key]
    if " " in key:
        candidates.append(key.replace(" ", "-"))
    if "-" in key:
        candidates.append(key.replace("-", " "))
    return [candidate for candidate in dict.fromkeys(candidates) if candidate]


def _get_store_loader_safe():
    try:
        return get_store_loader()
    except Exception:
        return None


def _resolve_catalog_attribute(attr_name: str, store_loader=None):
    loader = store_loader if store_loader is not None else _get_store_loader_safe()
    if not loader or not hasattr(loader, "resolve_attribute"):
        return None
    for candidate in _attribute_key_candidates(attr_name):
        attr = loader.resolve_attribute(candidate)
        if attr:
            return attr
    return None


def _fallback_attribute_display_name(attr_name: str) -> str:
    raw = str(attr_name or "").strip()
    if raw.startswith("pa_"):
        raw = raw[3:]
    return raw.replace("-", " ").replace("_", " ").title()


def _attribute_display_name(attr_name: str, store_loader=None) -> str:
    attr = _resolve_catalog_attribute(attr_name, store_loader)
    if attr and getattr(attr, "label", None):
        return attr.label
    return _fallback_attribute_display_name(attr_name)


def _resolve_attribute_term_name(attr_name: str, raw_value, store_loader=None) -> str:
    value = str(raw_value or "")
    if not value:
        return ""
    loader = store_loader if store_loader is not None else _get_store_loader_safe()
    attr = _resolve_catalog_attribute(attr_name, loader)
    if loader and attr and hasattr(loader, "resolve_attribute_term"):
        term = loader.resolve_attribute_term(attr.key, value)
        if term and getattr(term, "name", None):
            return term.name
    return value


def _normalize_attribute_lookup_name(attr_name: str) -> str:
    raw = str(attr_name or "").strip().lower().replace("_", " ")
    if raw.startswith("pa_"):
        raw = raw[3:]
    return re.sub(r"\s+", " ", raw.replace("-", " ")).strip()


def _resolve_display_to_slug_key(attr_name: str, display_to_slug: dict = None, store_loader=None) -> str:
    attr = _resolve_catalog_attribute(attr_name, store_loader)
    if attr:
        taxonomy = getattr(attr, "backend_ref", {}).get("taxonomy")
        if taxonomy:
            return taxonomy
        if attr.key:
            return attr.key

    raw = str(attr_name or "").strip()
    if not raw:
        return ""
    if not display_to_slug:
        return raw.lower()
    if raw in display_to_slug:
        return raw
    raw_lower = raw.lower()
    if raw_lower in display_to_slug:
        return raw_lower

    normalized = _normalize_attribute_lookup_name(raw)
    for key in display_to_slug.keys():
        if _normalize_attribute_lookup_name(key) == normalized:
            return key
    return raw_lower


def _variation_matches_resolved_neutral(
    var: dict,
    resolved_attributes: dict,
    display_to_slug: dict = None,
    store_loader=None,
) -> bool:
    if not resolved_attributes:
        return True

    loader = store_loader if store_loader is not None else _get_store_loader_safe()
    v_attrs = var.get("attributes", {})
    var_map: dict = {}
    var_taxonomies: dict = {}

    def _remember_option(attr_name, option_value):
        option = str(option_value or "")
        if not option:
            return

        taxonomy = _resolve_display_to_slug_key(attr_name, display_to_slug, loader)
        attr = _resolve_catalog_attribute(attr_name, loader)
        aliases = {str(attr_name or "").lower().strip()}
        if attr:
            aliases.update({
                str(attr.key or "").lower().strip(),
                str(attr.label or "").lower().strip(),
            })
        else:
            aliases.add(_fallback_attribute_display_name(attr_name).lower())

        for alias in aliases:
            if alias:
                var_map[alias] = option
                if taxonomy:
                    var_taxonomies[alias] = taxonomy

    if isinstance(v_attrs, list):
        for attr in v_attrs:
            if isinstance(attr, dict):
                _remember_option(attr.get("name", ""), attr.get("option", ""))
    elif isinstance(v_attrs, dict):
        for key, value in v_attrs.items():
            _remember_option(key, value)

    for res_key, res_val in resolved_attributes.items():
        attr = _resolve_catalog_attribute(res_key, loader)
        aliases = []
        if attr:
            aliases.extend([
                str(attr.label or "").lower().strip(),
                str(attr.key or "").lower().strip(),
            ])
        aliases.extend([
            str(res_key or "").lower().strip(),
            _fallback_attribute_display_name(res_key).lower(),
        ])

        actual_slug = None
        taxonomy = ""
        for candidate_alias in dict.fromkeys(alias for alias in aliases if alias):
            if candidate_alias in var_map:
                actual_slug = var_map[candidate_alias]
                taxonomy = var_taxonomies.get(candidate_alias, "")
                break

        if actual_slug is None:
            continue

        if display_to_slug:
            taxonomy = taxonomy or _resolve_display_to_slug_key(res_key, display_to_slug, loader)
            term_map = display_to_slug.get(taxonomy, {})
            expected_slug = term_map.get(str(res_val).lower(), "")
            if expected_slug:
                if expected_slug != actual_slug:
                    if re.sub(r'[^a-z0-9]+', '', expected_slug.lower()) != re.sub(r'[^a-z0-9]+', '', actual_slug.lower()):
                        return False
                continue

        if re.sub(r'[^a-z0-9]+', '', str(res_val).lower()) != re.sub(r'[^a-z0-9]+', '', str(actual_slug).lower()):
            return False

    return True


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
    """Build pagination object from API responses and call params.

    Handles two response shapes:

    WooCommerce (woo_client):
        resp = {"success": True, "total": 28, "total_pages": 7, "data": {...}}
        call.params contains "per_page"

    Shopify executor (ShopifyQueryExecutor.execute_from_body):
        resp = {"success": True, "data": {"total": 28, "pages": 7, "per_page": 4, ...}}
        call.params is {} — per_page lives in call.body
    """
    total_items = None
    total_pages = None
    per_page = DEFAULT_PER_PAGE

    # ── per_page: prefer call.params (WooCommerce), fall back to call.body (Shopify) ──
    if api_calls:
        call = api_calls[0]
        per_page = int(
            call.params.get("per_page")
            or (call.body or {}).get("per_page")
            or DEFAULT_PER_PAGE
        )

    for resp in api_responses:
        if not resp.get("success"):
            continue

        # ── WooCommerce: total/total_pages sit at the top level of resp ──
        raw_total       = resp.get("total")
        raw_total_pages = resp.get("total_pages")

        # ── Shopify executor: they sit inside resp["data"] ──
        # The executor uses "pages" (not "total_pages") for the page count.
        if raw_total is None or raw_total_pages is None:
            data = resp.get("data") or {}
            if isinstance(data, dict):
                if raw_total is None:
                    raw_total = data.get("total")
                if raw_total_pages is None:
                    # "pages" is the Shopify executor key; also accept "total_pages"
                    # in case a future executor aligns with the WooCommerce name.
                    raw_total_pages = data.get("pages") or data.get("total_pages")

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

        break  # use only the first successful response

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
            call = endpoints.fetch_variant(
                product_id=product_id,
                variant_id=variation_id,
                description=f"Fetch variation {variation_id} price",
            )
            resp = woo_client.execute(call)
            if resp.get("success") and isinstance(resp.get("data"), dict):
                variant = endpoints.parse_variant(resp["data"])
                return variant["price"] or "N/A"
        elif product_id:
            call = endpoints.fetch_product(
                product_id=product_id,
                description=f"Fetch product {product_id} price",
            )
            resp = woo_client.execute(call)
            if resp.get("success") and isinstance(resp.get("data"), dict):
                product = endpoints.parse_product(resp["data"])
                return product["price"] or "N/A"
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
    store_loader = _get_store_loader_safe()

    # ── STEP 1: Parent options (authoritative for non-wildcard axes) ──
    parent_defined_axes: set = set()
    attributes = parent_raw.get("attributes", [])
    if isinstance(attributes, list):
        for attr in attributes:
            if not (isinstance(attr, dict) and attr.get("variation") is True):
                continue
            name = attr.get("name", "")
            nice_name = _attribute_display_name(name, store_loader)
            if not nice_name or nice_name.lower() in resolved_keys_lower:
                continue
            opts = [
                _resolve_attribute_term_name(name, o, store_loader).strip()
                for o in attr.get("options", [])
                if str(o).strip()
            ]
            if opts:
                parent_defined_axes.add(nice_name.lower())
                missing_attrs[nice_name] = set(opts)

    # ── STEP 2: Variation scan (wildcard axes only) ──
    variations = variations_list if variations_list is not None else parent_raw.get("variations", [])
    log.info(f"build_variant_prompt: Scanning {len(variations)} variations for product")

    def _matches_resolved(var: dict) -> bool:
        return _variation_matches_resolved_neutral(var, resolved, display_to_slug, store_loader)

    filtered = [v for v in variations if isinstance(v, dict) and _matches_resolved(v)]
    log.info(
        f"build_variant_prompt: {len(filtered)}/{len(variations)} variations "
        f"match resolved={list(resolved.keys())}"
    )

    for v in filtered:
        v_attrs = v.get("attributes", {})

        if isinstance(v_attrs, dict):
            for k, val in v_attrs.items():
                if not val:
                    continue
                nice_name = _attribute_display_name(k, store_loader)
                if nice_name.lower() in resolved_keys_lower or nice_name.lower() in parent_defined_axes:
                    continue
                display_value = _resolve_attribute_term_name(k, val, store_loader)
                missing_attrs.setdefault(nice_name, set()).add(display_value)

        elif isinstance(v_attrs, list):
            for a in v_attrs:
                name = a.get("name", "")
                val = a.get("option", "")
                if not (name and val):
                    continue
                nice_name = _attribute_display_name(name, store_loader)
                if nice_name.lower() in resolved_keys_lower or nice_name.lower() in parent_defined_axes:
                    continue
                display_value = _resolve_attribute_term_name(name, val, store_loader)
                missing_attrs.setdefault(nice_name, set()).add(display_value)

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
    display_to_slug: dict = None,
    resolved_attr_values: list = None,   # ← add this, flat list fallback
) -> str:
    """Builds a friendly markdown prompt listing the available variation options."""

    # resolved_attr_values is a flat hint list ['white', 'gray'] — only use it
    # if no structured resolved_attributes were passed from the entity extractor.
    effective_resolved = resolved_attributes
    if not effective_resolved and resolved_attr_values:
        # Best-effort: treat all values as belonging to Colors axis
        effective_resolved = {"Colors": resolved_attr_values}

    options = _compute_variant_options(
        parent_raw, effective_resolved, variations_list, display_to_slug
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
    attrs = variation.get("attributes", [])
    
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