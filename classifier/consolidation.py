"""
classifier/consolidation.py — Post-classification entity consolidation.

Handles:
  - Hijack prevention (product vs category vs tag conflicts)
  - OR-pair deduplication
  - Category ↔ attribute overlap detection
  - Redundant attribute pruning
"""

import re

from models import Intent, ExtractedEntities
from store_registry import get_store_loader
from chat_logger import get_logger
from classifier.utils import normalize_for_tag_compare
from classifier.extractors import extract_category

logger = get_logger("miraq_chat")

PRODUCT_SPECIFIC_INTENTS = {
    Intent.PRODUCT_VARIATIONS, Intent.PRODUCT_DETAIL, Intent.PRODUCT_SEARCH,
    Intent.PRODUCT_ATTRIBUTE_INFO, Intent.QUICK_ORDER,
    Intent.PLACE_ORDER, Intent.ORDER_ITEM,
}


def consolidate_entities(intent: Intent, entities: ExtractedEntities, text: str):
    logger.debug(f"[consolidate] START | tags={entities.tag_slugs} | attrs={dict(entities.attributes)} | or_pairs={entities.attr_tag_or_pairs}")
    _resolve_product_vs_category(intent, entities)
    logger.debug(f"[consolidate] after _resolve_product_vs_category | tags={entities.tag_slugs}")
    _resolve_series_tag_conflict(entities, text)
    logger.debug(f"[consolidate] after _resolve_series_tag_conflict | tags={entities.tag_slugs}")
    _deduplicate_or_pairs(entities)
    logger.debug(f"[consolidate] after _deduplicate_or_pairs | tags={entities.tag_slugs}")
    _resolve_category_attribute_overlap(entities)
    logger.debug(f"[consolidate] after _resolve_category_attribute_overlap | tags={entities.tag_slugs}")
    _prune_tag_covered_attrs(entities)
    logger.debug(f"[consolidate] after _prune_tag_covered_attrs | tags={entities.tag_slugs}")
    _prune_redundant_attributes(entities)
    logger.debug(f"[consolidate] after _prune_redundant_attributes | tags={entities.tag_slugs}")


def _resolve_product_vs_category(intent: Intent, entities: ExtractedEntities):
    """When both product_id and category are set, drop the lower-priority one."""
    if not (getattr(entities, 'target_category_slugs', set()) and entities.product_id is not None):
        return
    if intent in PRODUCT_SPECIFIC_INTENTS:
        entities.target_category_slugs.clear()
        entities.category_name = None
    else:
        entities.product_id = None


def _resolve_series_tag_conflict(entities: ExtractedEntities, text: str):
    """Drop product_id when a series/collection tag was matched instead."""
    if not (entities.tag_ids and entities.product_id):
        return
    is_series = any("series" in slug or "collection" in slug for slug in entities.tag_slugs)
    if not is_series:
        return

    logger.info(f"Conflict: Series tag found. Dropping product_id {entities.product_id}")
    if not getattr(entities, 'target_category_slugs', set()):
        extract_category(text, entities)
        if entities.category_name:
            logger.info(f"Restored category '{entities.category_name}' after product drop.")

    entities.product_id = None
    entities.product_name = None
    entities.product_slug = None


def _deduplicate_or_pairs(entities: ExtractedEntities):
    """Remove categories already covered by attr_tag_or_pairs."""
    if not entities.attr_tag_or_pairs:
        return

    # Do NOT remove tags from tag_slugs here — if the user explicitly
    # requested a tag in the current turn, removing it because a prior
    # active search OR pair also references that tag silently drops a
    # valid filter. The AND of (tag standalone) + (tag OR attr) is
    # equivalent to just (tag), which is exactly what the user wants.

    handled_cats = {p.get("attr_term") for p in entities.attr_tag_or_pairs if p.get("attr_taxonomy") == "product_cat"}
    if handled_cats and getattr(entities, 'target_category_slugs', set()).intersection(handled_cats):
        entities.target_category_slugs.clear()
        entities.category_name = None

    loader = get_store_loader()

    handled_tags = {p.get("tag_slug") for p in entities.attr_tag_or_pairs if p.get("tag_slug")}
    if handled_tags:
        entities.tag_slugs = [s for s in entities.tag_slugs if s not in handled_tags]
        entities.tag_ids = [
            tid for tid in entities.tag_ids
            if not (loader and loader.tag_by_id.get(tid, {}).get("slug") in handled_tags)
        ]

    handled_cats = {p.get("attr_term") for p in entities.attr_tag_or_pairs if p.get("attr_taxonomy") == "product_cat"}
    if handled_cats and getattr(entities, 'target_category_slugs', set()).intersection(handled_cats):
        entities.target_category_slugs.clear()
        entities.category_name = None


def _resolve_category_attribute_overlap(entities: ExtractedEntities):
    """Convert overlapping category + attribute into an OR pair."""
    if not (getattr(entities, 'target_category_slugs', set()) and entities.attributes):
        return

    loader = get_store_loader()

    cat_tokens_map = {}
    for cat_slug in list(entities.target_category_slugs):
        cat_obj = loader.resolve_category(cat_slug) if loader else None
        cat_name = cat_obj.name.lower() if cat_obj else cat_slug.replace("-", " ")
        cat_tokens_map[cat_slug] = normalize_for_tag_compare(cat_name)

    for attr_label, attr_slug in list(entities.attributes.items()):
        attr_tokens = normalize_for_tag_compare(attr_slug.replace("-", " "))

        overlapping_cat_slug = None
        for cat_slug, c_tokens in cat_tokens_map.items():
            if attr_tokens <= c_tokens or c_tokens <= attr_tokens or attr_slug == cat_slug:
                overlapping_cat_slug = cat_slug
                break

        if not overlapping_cat_slug:
            continue

        actual_tax = ""
        if loader and loader.all_attributes_raw:
            for a in loader.all_attributes_raw:
                label_raw = a.get("attribute_label") or a.get("name") or a.get("attribute_name") or ""
                if label_raw.lower().strip() == attr_label:
                    actual_tax = a.get("taxonomy", "")
                    break

        if actual_tax:
            entities.attr_tag_or_pairs.append({
                "cat_slugs": [overlapping_cat_slug],
                "attr_taxonomy": actual_tax,
                "attr_term": attr_slug,
            })
            if overlapping_cat_slug in entities.target_category_slugs:
                entities.target_category_slugs.remove(overlapping_cat_slug)
            if not entities.target_category_slugs:
                entities.category_name = None
            del entities.attributes[attr_label]


def _prune_tag_covered_attrs(entities: ExtractedEntities):
    """Remove attributes and OR pairs whose terms are subsets of exact tag matches."""
    if not entities.tag_slugs:
        return

    loader = get_store_loader()
    exact_tag_tokens = []
    if loader:
        for tslug in entities.tag_slugs:
            tag_obj = loader.resolve_tag(tslug)
            if tag_obj:
                exact_tag_tokens.append(normalize_for_tag_compare(tag_obj.name))

    if not exact_tag_tokens:
        return

    if entities.attr_tag_or_pairs:
        valid = []
        for pair in entities.attr_tag_or_pairs:
            attr_tokens = normalize_for_tag_compare(pair.get("attr_term", "").replace("-", " "))
            if not any(attr_tokens <= et for et in exact_tag_tokens):
                valid.append(pair)
        entities.attr_tag_or_pairs = valid

    if entities.attributes:
        for label, slug in list(entities.attributes.items()):
            attr_tokens = normalize_for_tag_compare(slug.replace("-", " "))
            if any(attr_tokens <= et for et in exact_tag_tokens):
                del entities.attributes[label]


def _prune_redundant_attributes(entities: ExtractedEntities):
    """Remove attributes that are subsets of other attributes."""
    if not entities.attributes or len(entities.attributes) < 2:
        return

    items = list(entities.attributes.items())
    to_delete = set()

    for i, (label1, slug1) in enumerate(items):
        tokens1 = normalize_for_tag_compare(slug1.replace("-", " "))
        for j, (label2, slug2) in enumerate(items):
            if i == j:
                continue
            tokens2 = normalize_for_tag_compare(slug2.replace("-", " "))
            if tokens2 < tokens1:
                to_delete.add(label2)
            elif tokens2 == tokens1:
                if re.search(r'\d', label2) and not re.search(r'\d', label1):
                    to_delete.add(label2)

    for label in to_delete:
        if label in entities.attributes:
            del entities.attributes[label]