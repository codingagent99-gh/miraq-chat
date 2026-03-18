"""
Product formatters for converting WooCommerce API responses to clean format.
"""

import re
from typing import List

from models import ExtractedEntities


def format_category(raw: dict) -> dict:
    """Convert raw WooCommerce category to clean response format."""
    image = raw.get("image")
    image_url = ""
    if isinstance(image, dict):
        image_url = image.get("src", "")

    return {
        "id":          raw.get("id"),
        "name":        raw.get("name", ""),
        "slug":        raw.get("slug", ""),
        "parent":      raw.get("parent", 0),
        "count":       raw.get("count", 0),
        "description": _clean_html(raw.get("description", "")),
        "image":       image_url,
        "type":        "category",
    }


def format_product(raw: dict) -> dict:
    """Convert raw WooCommerce product to clean response format."""
    images = raw.get("images", [])
    image_urls = []
    for img in images:
        if isinstance(img, dict):
            src = img.get("src", "")
            if src:
                image_urls.append(src)
        elif isinstance(img, str) and img:
            image_urls.append(img)

    categories = raw.get("categories", [])
    cat_names = []
    for c in categories:
        if isinstance(c, dict):
            name = c.get("name", "")
            if name:
                cat_names.append(name)
        elif isinstance(c, str) and c:
            cat_names.append(c)

    tags = raw.get("tags", [])
    tag_names = []
    for t in tags:
        if isinstance(t, dict):
            name = t.get("name", "")
            if name:
                tag_names.append(name)
        elif isinstance(t, str) and t:
            tag_names.append(t)

    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None
    
    # ── SMART STOCK CHECK ──
    raw_status = raw.get("stock_status", "instock")
    is_in_stock = (raw_status != "outofstock")
    
    if raw.get("type") == "variable" and not is_in_stock:
        variations = raw.get("variations", [])
        if variations:
            is_in_stock = any(
                v.get("stock_status") == "instock" or v.get("in_stock") is True 
                for v in variations
            )

    # ── HONEST DATA: Pass exactly what the API reports ──
    is_on_sale = raw.get("on_sale", False)

    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "in_stock": is_in_stock, 
        "stock_status": raw_status,
        "slug":          raw.get("slug", ""),
        "sku":           raw.get("sku", ""),
        "permalink":     raw.get("permalink", ""),
        "type":          raw.get("type", "simple"),
        "price":         price,
        "regular_price": regular_price,
        "sale_price":    sale_price,
        "on_sale":       is_on_sale,
        "categories":    cat_names,
        "tags":          tag_names,
        "images":        image_urls,
        "attributes":    _format_attributes(raw.get("attributes", [])),
        "raw_attributes": _format_attributes(raw.get("attributes", []), include_hidden=True),
        "variations":    raw.get("variations", []),
    }

def _format_attributes(attrs: list, include_hidden: bool = False) -> list:
    """Format product attributes for response."""
    result = []
    for attr in attrs:
        if isinstance(attr, dict) and (include_hidden or attr.get("visible", False)):
            result.append({
                "name":    attr.get("name", ""),
                "options": attr.get("options", []),
                "is_variation": attr.get("variation", False)
            })
    return result

def format_custom_product(raw: dict) -> dict:
    """Convert raw custom API product to clean response format."""
    image_urls = raw.get("images", [])
    cat_names  = raw.get("categories", [])

    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price") or raw.get("regular", ""))
    sale_price_raw = raw.get("sale_price", "") or raw.get("sale", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None
    
    # ── HONEST DATA ──
    is_in_stock = raw.get("stock_status") == "instock"
    is_on_sale = bool(sale_price_raw and sale_price_raw != "")

    # Attributes come as {slug: {...}} — convert to [{name, options}]
    attributes_dict = raw.get("attributes", {})
    attributes = []
    for slug, attr_data in attributes_dict.items():
        options = []
        if isinstance(attr_data, list):
            options = attr_data  # Custom API format: ["Matte", "Polished"]
        elif isinstance(attr_data, dict):
            options = attr_data.get("options", [])
            
        name = slug.replace("pa_", "").replace("-", " ").title()
        attributes.append({"name": name, "options": options})

    return {
        "id":            raw.get("id"),
        "name":          raw.get("name", ""),
        "slug":          raw.get("slug", ""),
        "sku":           raw.get("sku", ""),
        "permalink":     raw.get("permalink", ""),
        "type":          "simple",
        "price":         price,
        "regular_price": regular_price,
        "sale_price":    sale_price,
        "on_sale":       is_on_sale,
        "in_stock":      is_in_stock,
        "categories":    cat_names,
        "images":        image_urls,
        "attributes":    attributes,
        "variations":    raw.get("variations", []),
    }

def format_variation(raw: dict, parent: dict = None) -> dict:
    """Convert a raw WooCommerce variation to clean response format."""
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None

    # ── HONEST DATA ──
    is_in_stock = raw.get("stock_status") == "instock"
    is_on_sale = raw.get("on_sale", False)

    attrs_raw = raw.get("attributes", [])
    # Custom API returns attributes as a flat dict {pa_finish: "matte"};
    # standard WC API returns a list [{name, option}]. Normalise to list.
    if isinstance(attrs_raw, dict):
        attrs = []
        for k, v in attrs_raw.items():
            if v:
                # Convert "pa_colors" -> "Colors"
                clean_name = k.replace("pa_", "").replace("-", " ").title()
                # Convert "ansel-warm-white" -> "Ansel Warm White"
                clean_option = v.replace("-", " ").title()
                attrs.append({"name": clean_name, "option": clean_option})
    else:
        attrs = attrs_raw
        
    attr_label = " / ".join(
        a.get("option", "") for a in attrs if a.get("option")
    )
    parent_name = parent.get("name", "") if parent else ""
    name = f"{parent_name} — {attr_label}" if attr_label else parent_name

    images = raw.get("image", {})
    image_url = images.get("src", "") if isinstance(images, dict) else ""

    return {
        "id":              raw.get("id"),
        "parent_id":       raw.get("parent_id") or (parent.get("id") if parent else None),
        "name":            name,
        "sku":             raw.get("sku", ""),
        "permalink":       parent.get("permalink", "") if parent else "",
        "type":            "variation",
        "price":           price,
        "regular_price":   regular_price,
        "sale_price":      sale_price,
        "on_sale":         is_on_sale,
        "in_stock":        is_in_stock,
        "images":          [image_url] if image_url else (parent.get("images", []) if parent else []),
        "attributes":      attrs,
        "variation_label": attr_label,
    }

def _filter_variations_by_entities(
    variations: List[dict], entities: ExtractedEntities
) -> List[dict]:
    """
    Filter variation list by the attributes the user specified.
    Handles both Native WC arrays and Custom API flat dicts.
    """
    filters: List[tuple] = []
    FINISH_SYNONYMS = {"matt": "matte", "glossy": "polished", "gloss": "polished"}

    for attr_label, attr_value in entities.attributes.items():
        val_lower = attr_value.lower().replace("-", " ")
        filters.append((attr_label, val_lower))
        if attr_label == "finish":
            normalized = FINISH_SYNONYMS.get(val_lower, val_lower)
            if normalized != val_lower:
                filters.append((attr_label, normalized))
        if attr_label == "colors":
            filters.append(("colors 2", val_lower))

    if not filters:
        return variations

    matched = []
    for var in variations:
        raw_attrs = var.get("attributes")
        if not raw_attrs:
            continue
            
        # Normalize attributes into a clean flat dictionary regardless of API source
        var_attrs = {}
        if isinstance(raw_attrs, dict):
            for k, v in raw_attrs.items():
                clean_k = k.replace("attribute_", "").replace("pa_", "").replace("-", " ").strip().lower()
                clean_v = str(v).replace("-", " ").strip().lower()
                var_attrs[clean_k] = clean_v
        elif isinstance(raw_attrs, list):
            for a in raw_attrs:
                clean_k = a.get("name", "").replace("-", " ").strip().lower()
                clean_v = a.get("option", "").replace("-", " ").strip().lower()
                var_attrs[clean_k] = clean_v

        # Check if it matches the user's requested filters
        if all(
            any(f_val in var_attrs.get(f_name, "") for f_name in var_attrs if f_name == attr_name or f_name.startswith(attr_name))
            or any(f_val in opt for opt in var_attrs.values())
            for attr_name, f_val in filters
        ):
            matched.append(var)

    return matched if matched else variations

def _entities_to_dict(entities: ExtractedEntities) -> dict:
    """Convert entities to a dict for logging/metadata."""
    d = {}
    if getattr(entities, 'product_name', None):    d["product_name"] = entities.product_name
    if getattr(entities, 'product_id', None):      d["product_id"] = entities.product_id
    if getattr(entities, 'category_name', None):   d["category_name"] = entities.category_name
    
    # NEW: Safely handle the category slugs set by converting it to a list for JSON
    if getattr(entities, 'target_category_slugs', None): 
        d["target_category_slugs"] = list(entities.target_category_slugs)
        
    if getattr(entities, 'attributes', None):      d["attributes"] = entities.attributes
    if getattr(entities, 'search_term', None):     d["search_term"] = entities.search_term
    if getattr(entities, 'order_id', None):        d["order_id"] = entities.order_id
    if getattr(entities, 'order_item_name', None): d["order_item_name"] = entities.order_item_name
    if getattr(entities, 'order_count', None):     d["order_count"] = entities.order_count
    if getattr(entities, 'quantity', None):        d["quantity"] = entities.quantity
    if getattr(entities, 'variation_id', None):    d["variation_id"] = entities.variation_id
    if getattr(entities, 'tag_ids', None):         d["tag_ids"] = entities.tag_ids
    if getattr(entities, 'collection_year', None): d["collection_year"] = entities.collection_year
    if getattr(entities, 'on_sale', None):         d["on_sale"] = entities.on_sale
    return d

def _safe_float(val) -> float:
    """Safely convert to float."""
    try:
        return float(val) if val not in ("", None) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _clean_html(html: str) -> str:
    """Strip HTML tags from description."""
    if not html:
        return ""
    clean = re.sub(r'<[^>]+>', '', html)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean