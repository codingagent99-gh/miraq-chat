"""
utils/entity_helpers.py — Shared helpers for merging ExtractedEntities instances
and restoring carryover state from semantic match context.
"""

import re
from typing import Optional
from models import ExtractedEntities
from classifier.utils import normalize_for_tag_compare
from api_builder.store_helpers import attr_slug_for_label

STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "at", "by", "with", "from",
    "is", "are", "am", "i", "i'm", "im", "my", "me", "you", "your", "it", "that", "this", "these", "those",
    "what", "how", "who", "where", "why", "which", "do", "does", "did", "can", "could", "would",
    "show", "tell", "give", "find", "search", "looking", "look", "suggest", "recommend", "want", "need",
    "product", "products", "item", "items", "option", "options", "something", "anything", "some", "any",
    "series", "collection", "line", "brand", "style", "type", "have", "has", "had",
    "please", "thanks", "thank", "hello", "hi", "hey",
    "under", "over", "above", "below", "latest", "newest", "recent", "new", "top", "bottom", "within"
}


def clean_leftovers(text_chunk: str) -> str:
    """Strip stop words and punctuation from leftover text."""
    if not text_chunk:
        return ""
    text_chunk = text_chunk.replace(",", " ")
    words = text_chunk.split()
    kept_words = []
    for w in words:
        clean_w = w.lower().strip('?,.!;:')
        if clean_w and clean_w not in STOP_WORDS:
            kept_words.append(clean_w)
    return " ".join(kept_words)


def append_category_name(entities: ExtractedEntities, new_name: str):
    """Append a category name to entities.category_name with proper formatting."""
    if not new_name:
        return
    if not getattr(entities, 'category_name', None):
        entities.category_name = new_name
    elif new_name not in entities.category_name:
        existing = [n.strip() for n in re.split(r',\s*|\s*&\s*', entities.category_name) if n.strip()]
        existing.append(new_name)
        entities.category_name = (
            ", ".join(existing[:-1]) + " & " + existing[-1]
            if len(existing) > 1 else existing[0]
        )

def merge_attribute(target_attrs: dict, key: str, value: str):
    """Merge an attribute value into a target dict, deduplicating CSV terms.

    Keys are compared via normalized tokens. When two differently-spelled
    keys represent the same attribute, the one that actually resolves via
    attr_slug_for_label is kept as the dict key (hyphenated slug-style
    spellings often don't match the WooCommerce label lookup at all).
    """
    norm_key = normalize_for_tag_compare(key.replace("-", " "))

    existing_key = None
    for k in target_attrs:
        if normalize_for_tag_compare(k.replace("-", " ")) == norm_key:
            existing_key = k
            break

    if existing_key is None:
        target_attrs[key] = value
        return

    if existing_key != key and attr_slug_for_label(key) and not attr_slug_for_label(existing_key):
        target_attrs[key] = target_attrs.pop(existing_key)
        existing_key = key

    existing_vals = target_attrs[existing_key].split(",")
    new_vals = [val for val in value.split(",") if val not in existing_vals]
    if new_vals:
        target_attrs[existing_key] += "," + ",".join(new_vals)         

def merge_tags(target: ExtractedEntities, source_tag_ids: list, source_tag_slugs: list):
    """Merge tag IDs and slugs into target without duplicates."""
    for tid, tslug in zip(source_tag_ids, source_tag_slugs):
        if tid not in target.tag_ids:
            target.tag_ids.append(tid)
            target.tag_slugs.append(tslug)


def merge_entities(target: ExtractedEntities, source: ExtractedEntities):
    """Full merge of source into target, deduplicating all fields."""
    # Product
    if getattr(source, 'product_id', None) and not getattr(target, 'product_id', None):
        target.product_name = source.product_name
        target.product_slug = source.product_slug
        target.product_id = source.product_id

    # Categories
    if getattr(source, 'target_category_slugs', None):
        target.target_category_slugs.update(source.target_category_slugs)
        append_category_name(target, getattr(source, 'category_name', None) or "")

    # Tags
    merge_tags(target, getattr(source, 'tag_ids', []), getattr(source, 'tag_slugs', []))

    # Attributes
    if getattr(source, 'attributes', None):
        for k, v in source.attributes.items():
            merge_attribute(target.attributes, k, v)

    # Exclusions
    if getattr(source, 'excluded_tags', None):
        if not hasattr(target, 'excluded_tags'):
            target.excluded_tags = []
        target.excluded_tags.extend(source.excluded_tags)

    if getattr(source, 'excluded_categories', None):
        if not hasattr(target, 'excluded_categories'):
            target.excluded_categories = []
        target.excluded_categories.extend(source.excluded_categories)

    if getattr(source, 'excluded_attributes', None):
        if not hasattr(target, 'excluded_attributes'):
            target.excluded_attributes = {}
        for k, v in source.excluded_attributes.items():
            if k not in target.excluded_attributes:
                target.excluded_attributes[k] = v
            else:
                target.excluded_attributes[k].extend(v)

    if getattr(source, 'excluded_search_term', None):
        target.excluded_search_term = source.excluded_search_term

    # Scalar filters
    _scalar_fields = [
        'in_stock', 'on_sale', 'min_price', 'max_price',
        'collection_year', 'date_after', 'date_before', 'order_count'
    ]
    for f in _scalar_fields:
        val = getattr(source, f, None)
        if val is not None:
            setattr(target, f, val)

    # Semantic matches
    if source.semantic_matches:
        target.semantic_matches.extend(source.semantic_matches)


# ─── Carryover Restore ───

_CARRYOVER_SIMPLE = [
    ("carryover_category_name", "category_name"),
    ("carryover_product_id", "product_id"),
    ("carryover_product_name", "product_name"),
    ("carryover_quantity", "quantity"),
    ("carryover_order_id", "order_id"),
    ("carryover_collection_year", "collection_year"),
    ("carryover_in_stock", "in_stock"),
    ("carryover_on_sale", "on_sale"),
    ("carryover_min_price", "min_price"),
    ("carryover_max_price", "max_price"),
    ("carryover_search_term", "search_term"),
]


def restore_carryover(entities: ExtractedEntities, pending: dict):
    """Restore all carryover fields from a pending_semantic_match dict into entities."""
    # Tags
    if pending.get("carryover_tags"):
        entities.tag_slugs.extend(pending["carryover_tags"])

    # Categories
    if pending.get("carryover_categories"):
        entities.target_category_slugs.update(pending["carryover_categories"])

    # Simple scalar fields
    for src_key, dst_attr in _CARRYOVER_SIMPLE:
        val = pending.get(src_key)
        if val is not None:
            setattr(entities, dst_attr, val)

    # Attributes
    if pending.get("carryover_attributes"):
        for k, v in pending["carryover_attributes"].items():
            merge_attribute(entities.attributes, k, v)

    # Excluded tags
    if pending.get("carryover_excluded_tags"):
        if not hasattr(entities, 'excluded_tags'):
            entities.excluded_tags = []
        entities.excluded_tags.extend(pending["carryover_excluded_tags"])

    # Excluded categories
    if pending.get("carryover_excluded_categories"):
        if not hasattr(entities, 'excluded_categories'):
            entities.excluded_categories = []
        entities.excluded_categories.extend(pending["carryover_excluded_categories"])

    # Excluded attributes
    if pending.get("carryover_excluded_attributes"):
        if not hasattr(entities, 'excluded_attributes'):
            entities.excluded_attributes = {}
        for k, v in pending["carryover_excluded_attributes"].items():
            if k not in entities.excluded_attributes:
                entities.excluded_attributes[k] = v
            else:
                entities.excluded_attributes[k].extend(v)

    # OR pairs
    if pending.get("carryover_attr_tag_or_pairs"):
        if not hasattr(entities, 'attr_tag_or_pairs'):
            entities.attr_tag_or_pairs = []
        entities.attr_tag_or_pairs.extend(pending["carryover_attr_tag_or_pairs"])