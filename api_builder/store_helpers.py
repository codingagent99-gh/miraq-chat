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


def attr_id(taxonomy_slug: str) -> Optional[int]:
    """Get attribute ID by WooCommerce taxonomy slug (e.g. 'pa_color')."""
    l = loader()
    if not l:
        return None
    # Try neutral key first (remove pa_ prefix)
    key = taxonomy_slug.removeprefix("pa_")
    attr = l.resolve_attribute(key)
    if attr:
        return attr.backend_ref.get("id")
    # Fall back to scanning all attributes for exact taxonomy match
    for a in l.attribute_by_key.values():
        if a.backend_ref.get("taxonomy") == taxonomy_slug:
            return a.backend_ref.get("id")
    return None


def category_slug(category_id: int) -> Optional[str]:
    """Get category slug by ID from live data."""
    l = loader()
    return l.get_category_slug(category_id) if l else None


def attr_slug_for_label(label: str) -> Optional[str]:
    """
    Resolve a WooCommerce attribute taxonomy slug from an attribute label.
    e.g. "finish" → "pa_finish", "tile size" → "pa_tile-size"

    Delegates to StoreLoader.resolve_attribute(), which already handles both
    the hyphenated attribute_name form ("colors-2") and the spaced display
    label form ("Colors 2", "Mosaic Type") — see its docstring for why.
    """
    l = loader()
    if not l:
        return None
    attr = l.resolve_attribute(label)
    return attr.backend_ref.get("taxonomy") if attr else None


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
    """Resolve a human term to a WooCommerce term slug via neutral attribute lookup."""
    l = loader()
    if not l:
        return None
    # Convert taxonomy slug (e.g. 'pa_color') to neutral key
    key = taxonomy.removeprefix("pa_")
    term = l.resolve_attribute_term(key, raw_term)
    return term.backend_ref.get("slug") if term else None