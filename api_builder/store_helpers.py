"""
api_builder/store_helpers.py — Thin accessor functions for StoreLoader data.

Centralises all loader lookups so the rest of api_builder never calls
get_store_loader() or touches loader internals directly.
"""

from typing import Optional
from store_registry import get_store_loader


def loader():
    """Convenience accessor for StoreLoader."""
    return get_store_loader()


def tag_id(slug: str) -> Optional[int]:
    """Get tag ID by slug from live data."""
    l = loader()
    return l.get_tag_id_by_slug(slug) if l else None


def attr_id(slug: str) -> Optional[int]:
    """Get attribute ID by slug from live data."""
    l = loader()
    return l.get_attribute_id(slug) if l else None


def category_slug(category_id: int) -> Optional[str]:
    """Get category slug by ID from live data."""
    l = loader()
    return l.get_category_slug(category_id) if l else None


def attr_slug_for_label(label: str) -> Optional[str]:
    """
    Resolve a WooCommerce attribute taxonomy slug from an attribute label.
    e.g. "finish" → "pa_finish", "tile size" → "pa_tile-size"
    Uses live all_attributes_raw — no hardcoded ATTR_* constants needed.
    """
    l = loader()
    if not l or not l.all_attributes_raw:
        return None
    label_lower = label.lower().strip()
    for attr in l.all_attributes_raw:
        attr_label = (
            attr.get("attribute_label")
            or attr.get("name")
            or attr.get("attribute_name")
            or ""
        ).lower().strip()
        if attr_label == label_lower:
            return attr.get("taxonomy")
    return None


def resolve_attr_filters(attributes: dict) -> dict:
    """
    Convert entity attributes {label: value} → {pa_slug: value}.
    Shared helper used by every intent that needs attribute filters.
    """
    result = {}
    for label, value in attributes.items():
        slug = attr_slug_for_label(label)
        if slug and value:
            result[slug] = value
    return result


def get_attribute_term_slug(taxonomy: str, raw_term: str) -> Optional[str]:
    """Resolve a human term to a WooCommerce term slug."""
    l = loader()
    return l.get_attribute_term_slug(taxonomy, raw_term) if l else None