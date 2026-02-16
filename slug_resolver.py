"""
Resolves WooCommerce slugs (category, tag) to numeric IDs at runtime.
"""

import requests
from typing import Optional

BASE_URL = "https://yourstore.com/wp-json/wc/v3"
AUTH = ("ck_your_consumer_key", "cs_your_consumer_secret")


def resolve_category_id(slug: str) -> Optional[int]:
    """Resolve a category slug to its WooCommerce category ID."""
    resp = requests.get(
        f"{BASE_URL}/products/categories",
        params={"slug": slug},
        auth=AUTH,
    )
    data = resp.json()
    return data[0]["id"] if data else None


def resolve_tag_id(slug: str) -> Optional[int]:
    """Resolve a tag slug to its WooCommerce tag ID."""
    resp = requests.get(
        f"{BASE_URL}/products/tags",
        params={"slug": slug},
        auth=AUTH,
    )
    data = resp.json()
    return data[0]["id"] if data else None


def resolve_attribute_term_id(attribute_id: int, slug: str) -> Optional[int]:
    """Resolve an attribute term slug to its ID."""
    resp = requests.get(
        f"{BASE_URL}/products/attributes/{attribute_id}/terms",
        params={"slug": slug},
        auth=AUTH,
    )
    data = resp.json()
    return data[0]["id"] if data else None


def finalize_params(params: dict) -> dict:
    """Replace _category_slug, _tag_slug with resolved numeric IDs."""
    final = {}
    for key, value in params.items():
        if key == "_category_slug":
            cat_id = resolve_category_id(value)
            if cat_id:
                final["category"] = cat_id
        elif key == "_tag_slug":
            tag_id = resolve_tag_id(value)
            if tag_id:
                final["tag"] = tag_id
        else:
            final[key] = value
    return final