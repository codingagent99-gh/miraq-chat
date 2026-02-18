"""
Core Helpers

Utility functions for filters, entity conversion, and user placeholder resolution.
"""

from typing import List

from models import Intent, ExtractedEntities, WooAPICall
from config.settings import USER_PLACEHOLDERS


def build_filters(
    intent: Intent,
    entities: ExtractedEntities,
    api_calls: List[WooAPICall],
) -> dict:
    """Build the filters_applied dict for the response."""
    filters = {
        "search": None,
        "category": None,
        "tag": None,
        "min_price": None,
        "max_price": None,
        "on_sale": None,
        "orderby": "date",
        "order": "desc",
    }

    # Extract from API call params
    if api_calls:
        params = api_calls[0].params
        filters["search"] = params.get("search")
        filters["category"] = params.get("category")
        filters["tag"] = params.get("tag")
        filters["on_sale"] = params.get("on_sale")
        if params.get("orderby"):
            filters["orderby"] = params["orderby"]
        if params.get("order"):
            filters["order"] = params["order"]

    # Override with entity data if more specific
    if entities.category_name and not filters["category"]:
        filters["category"] = entities.category_name
    if entities.on_sale:
        filters["on_sale"] = True

    return filters


def entities_to_dict(entities: ExtractedEntities) -> dict:
    """Convert entities to a clean dict for metadata."""
    d = {}
    if entities.product_name:    d["product_name"] = entities.product_name
    if entities.product_slug:    d["product_slug"] = entities.product_slug
    if entities.category_id:     d["category_id"] = entities.category_id
    if entities.category_name:   d["category_name"] = entities.category_name
    if entities.visual:          d["visual"] = entities.visual
    if entities.finish:          d["finish"] = entities.finish
    if entities.tile_size:       d["tile_size"] = entities.tile_size
    if entities.color_tone:      d["color_tone"] = entities.color_tone
    if entities.thickness:       d["thickness"] = entities.thickness
    if entities.origin:          d["origin"] = entities.origin
    if entities.collection_year: d["collection_year"] = entities.collection_year
    if entities.quick_ship:      d["quick_ship"] = True
    if entities.on_sale:         d["on_sale"] = True
    if entities.tag_slugs:       d["tags"] = entities.tag_slugs
    if entities.order_id:        d["order_id"] = entities.order_id
    if entities.order_count:     d["order_count"] = entities.order_count
    if entities.reorder:         d["reorder"] = entities.reorder
    if entities.order_item_name: d["order_item_name"] = entities.order_item_name
    return d


def resolve_user_placeholders(api_calls: List[WooAPICall], customer_id: int):
    """
    Replace CURRENT_USER_ID placeholders with actual customer ID.
    
    Modifies api_calls in-place, replacing any placeholder strings in params or body
    with the provided customer_id (converted to string for API compatibility).
    
    Args:
        api_calls: List of WooAPICall objects to process
        customer_id: The actual customer ID (integer) to substitute for placeholders.
                     Will be converted to string internally for WooCommerce API compatibility.
    """
    customer_id_str = str(customer_id)
    for call in api_calls:
        if isinstance(call.params, dict):
            for key in call.params:
                if isinstance(call.params[key], str) and call.params[key] in USER_PLACEHOLDERS:
                    call.params[key] = customer_id_str
        if isinstance(call.body, dict):
            for key in call.body:
                if isinstance(call.body[key], str) and call.body[key] in USER_PLACEHOLDERS:
                    call.body[key] = customer_id_str
