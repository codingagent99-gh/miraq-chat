"""
Canonical backend-neutral catalog types for in-memory store data.

These types are the canonical in-memory representation of catalog data.
Consumers (classifier, query mixin, suggestion builder, formatters, response
generator) should read only `key`, `label`, `name`, `count`, and the nested
terms. The `backend_ref` blob is opaque to consumers — only
`ecommerce/woo_endpoints.py` (and future `shopify_endpoints.py`) is allowed to
read its contents to construct outgoing API calls.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CatalogAttributeTerm:
    """A backend-neutral attribute value (e.g. "Red" under "Color")."""

    key: str
    name: str
    count: int = 0
    # Intentionally mutable opaque backend blob.
    # Woo: {"slug": "red", "id": 42}
    # Shopify (future): {"value": "Red"}
    backend_ref: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogAttribute:
    """A backend-neutral attribute (e.g. "Color", "Size")."""

    key: str
    label: str
    terms: tuple[CatalogAttributeTerm, ...] = ()
    # Intentionally mutable opaque backend blob.
    # Woo: {"taxonomy": "pa_color", "id": 17, "attribute_name": "color"}
    # Shopify (future): {"option_name": "Color"}
    backend_ref: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogCategory:
    """A backend-neutral product category."""

    key: str
    name: str
    parent_key: Optional[str] = None
    count: int = 0
    # Intentionally mutable opaque backend blob.
    # Woo: {"id": 7, "slug": "wall-tiles", "parent_id": 0}
    # Shopify (future): {"collection_gid": "gid://..."}
    backend_ref: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogTag:
    """A backend-neutral product tag."""

    key: str
    name: str
    count: int = 0
    # Intentionally mutable opaque backend blob.
    backend_ref: dict = field(default_factory=dict)
