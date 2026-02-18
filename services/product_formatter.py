"""
Product Formatter

Converts raw WooCommerce product data to clean response format.
"""

import re
from typing import List

from models import ExtractedEntities


def format_product(raw: dict) -> dict:
    """Convert raw WooCommerce product to clean response format."""
    images = raw.get("images", [])
    image_urls = [img.get("src", "") for img in images if img.get("src")]

    categories = raw.get("categories", [])
    cat_names = [c.get("name", "") for c in categories]

    tags = raw.get("tags", [])
    tag_names = [t.get("name", "") for t in tags]

    # Parse prices safely
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None

    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": raw.get("permalink", ""),
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": raw.get("on_sale", False),
        "in_stock": raw.get("stock_status") == "instock",
        "stock_status": raw.get("stock_status", ""),
        "total_sales": raw.get("total_sales", 0),
        "description": _clean_html(raw.get("description", "")),
        "short_description": _clean_html(raw.get("short_description", "")),
        "categories": cat_names,
        "tags": tag_names,
        "images": image_urls,
        "average_rating": raw.get("average_rating", "0.00"),
        "rating_count": raw.get("rating_count", 0),
        "weight": raw.get("weight", ""),
        "dimensions": raw.get("dimensions", {"length": "", "width": "", "height": ""}),
        "attributes": _format_attributes(raw.get("attributes", [])),
        "variations": raw.get("variations", []),
        "type": raw.get("type", "simple"),
    }


def _format_attributes(attrs: list) -> list:
    """Format product attributes for response."""
    result = []
    for attr in attrs:
        if attr.get("visible", False):
            result.append({
                "name": attr.get("name", ""),
                "options": attr.get("options", []),
            })
    return result


def format_custom_product(raw: dict) -> dict:
    """Convert raw custom API product to clean response format."""
    # Images are already a list of URLs (not objects like standard WC)
    image_urls = raw.get("images", [])
    
    # Categories are already a list of strings (not objects)
    cat_names = raw.get("categories", [])
    
    # Parse prices safely
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None
    
    # Derive on_sale from sale_price being non-empty
    on_sale = bool(sale_price_raw and sale_price_raw != "")
    
    # Attributes come as a dict {slug: {}} rather than a list
    # Convert to list format for consistency
    attributes_dict = raw.get("attributes", {})
    attributes = []
    for slug, attr_data in attributes_dict.items():
        if isinstance(attr_data, dict):
            # Extract options if available, otherwise empty list
            options = attr_data.get("options", []) if attr_data else []
            # Convert slug to readable name (e.g., pa_finish -> Finish)
            name = slug.replace("pa_", "").replace("-", " ").title()
            attributes.append({
                "name": name,
                "options": options,
            })
    
    return {
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": raw.get("permalink", ""),
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": on_sale,
        "in_stock": raw.get("stock_status") == "instock",
        "stock_status": raw.get("stock_status", ""),
        "total_sales": 0,  # Not provided by custom API
        "description": _clean_html(raw.get("description", "")),
        "short_description": _clean_html(raw.get("short_description", "")),
        "categories": cat_names,
        "tags": [],  # Not provided by custom API
        "images": image_urls,
        "average_rating": "0.00",  # Not provided by custom API
        "rating_count": 0,  # Not provided by custom API
        "weight": "",  # Not provided by custom API
        "dimensions": {"length": "", "width": "", "height": ""},  # Not provided by custom API
        "attributes": attributes,
        "variations": [],  # Not provided by custom API
        "type": "simple",  # Not provided by custom API
    }


def format_variation(raw: dict, parent: dict = None) -> dict:
    """Convert a raw WooCommerce variation to clean response format."""
    price = _safe_float(raw.get("price", ""))
    regular_price = _safe_float(raw.get("regular_price", ""))
    sale_price_raw = raw.get("sale_price", "")
    sale_price = _safe_float(sale_price_raw) if sale_price_raw else None

    # Build attribute label from variation attributes e.g. "Matte / 24x48 / Grey"
    attrs = raw.get("attributes", [])
    attr_label = " / ".join(
        a.get("option", "") for a in attrs if a.get("option")
    )
    parent_name = parent.get("name", "") if parent else ""
    name = f"{parent_name} — {attr_label}" if attr_label else parent_name

    images = raw.get("image", {})
    image_url = images.get("src", "") if isinstance(images, dict) else ""

    return {
        "id": raw.get("id"),
        "parent_id": raw.get("parent_id") or (parent.get("id") if parent else None),
        "name": name,
        "slug": raw.get("slug", ""),
        "sku": raw.get("sku", ""),
        "permalink": parent.get("permalink", "") if parent else "",
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": raw.get("on_sale", False),
        "in_stock": raw.get("stock_status") == "instock",
        "stock_status": raw.get("stock_status", ""),
        "images": [image_url] if image_url else (parent.get("images", []) if parent else []),
        "attributes": attrs,
        "type": "variation",
        "variation_label": attr_label,
    }


def filter_variations_by_entities(
    variations: List[dict], entities: ExtractedEntities
) -> List[dict]:
    """
    Filter variation list by the attributes the user specified.
    Each variation has attributes like:
      [{"name": "Finish", "option": "Matte"}, {"name": "Tile Size", "option": '24"x48"'}]
    """
    # Build a set of (attr_name_lower, option_lower) pairs the user asked for
    filters: List[tuple] = []

    if entities.finish:
        filters.append(("finish", entities.finish.lower()))
        # Common synonyms handled by normalising both sides to lowercase
        FINISH_SYNONYMS = {"matt": "matte", "glossy": "polished", "gloss": "polished"}
        normalized = FINISH_SYNONYMS.get(entities.finish.lower(), entities.finish.lower())
        if normalized != entities.finish.lower():
            filters.append(("finish", normalized))

    if entities.color_tone:
        filters.append(("colors", entities.color_tone.lower()))
        filters.append(("colors 2", entities.color_tone.lower()))

    if entities.tile_size:
        filters.append(("tile size", entities.tile_size.lower()))

    if entities.thickness:
        filters.append(("thickness", entities.thickness.lower()))

    if entities.origin:
        filters.append(("origin", entities.origin.lower()))

    if entities.visual:
        filters.append(("visual", entities.visual.lower()))

    if not filters:
        return variations

    matched = []
    for var in variations:
        var_attrs = {
            a.get("name", "").lower(): a.get("option", "").lower()
            for a in var.get("attributes", [])
        }
        # Variation matches if ALL specified filters are satisfied
        if all(
            any(f_val in var_attrs.get(f_name, "") for f_name in var_attrs if f_name == attr_name or f_name.startswith(attr_name))
            or any(f_val in opt for opt in var_attrs.values())
            for attr_name, f_val in filters
        ):
            matched.append(var)

    return matched if matched else variations  # if nothing matched, return all (don't blank out)


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
