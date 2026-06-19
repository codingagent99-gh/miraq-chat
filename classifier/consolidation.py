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

def _resolve_tag_attribute_overlap(entities: ExtractedEntities):
    """
    extract_tag and extract_attributes run independently on separately-masked
    text in classify(), with no cross-checking — so the same input (e.g.
    "quick ship") can set BOTH tag_slugs=['quick-ship'] AND
    attributes={'quick-ship': 'yes'} simultaneously. Merge these into a
    single OR pair so the query becomes (product_tag=X OR pa_X=value)
    instead of silently using only one of the two taxonomies.
    """
    if not (entities.tag_slugs and entities.attributes):
        return

    loader = get_store_loader()

    for tag_slug in list(entities.tag_slugs):
        tag_tokens = normalize_for_tag_compare(tag_slug.replace("-", " "))

        for attr_label, attr_value in list(entities.attributes.items()):
            label_tokens = normalize_for_tag_compare(attr_label.replace("-", " "))
            if label_tokens != tag_tokens:
                continue

            actual_tax = ""
            if loader and loader.all_attributes_raw:
                attr_label_norm = attr_label.lower().strip()
                for a in loader.all_attributes_raw:
                    # attribute_name is the hyphenated form matching entities.attributes
                    # dict keys (e.g. "quick-ship"); attribute_label is the human-readable
                    # form (e.g. "Quick Ship") and won't match for multi-word names.
                    name_raw = (a.get("attribute_name") or "").lower().strip()
                    label_raw = (a.get("attribute_label") or a.get("name") or "").lower().strip()
                    if attr_label_norm in (name_raw, label_raw, label_raw.replace(" ", "-")):
                        actual_tax = a.get("taxonomy", "")
                        break

            if not actual_tax:
                continue

            entities.attr_tag_or_pairs.append({
                "tag_slug": tag_slug,
                "attr_taxonomy": actual_tax,
                "attr_term": attr_value,
            })

            # Remove tag_slug AND its paired tag_id together — tag_ids and
            # tag_slugs are parallel lists (merge_tags zips them by index),
            # so removing only one would misalign them on future merges.
            if tag_slug in entities.tag_slugs:
                idx = entities.tag_slugs.index(tag_slug)
                entities.tag_slugs.pop(idx)
                if idx < len(entities.tag_ids):
                    entities.tag_ids.pop(idx)

            del entities.attributes[attr_label]
            logger.info(
                f"_resolve_tag_attribute_overlap: merged tag='{tag_slug}' + "
                f"attr='{attr_label}:{attr_value}' → OR pair on taxonomy='{actual_tax}'"
            )
            break
        
def consolidate_entities(intent: Intent, entities: ExtractedEntities, text: str):
    _resolve_product_vs_category(intent, entities)
    _resolve_series_tag_conflict(entities, text)
    _deduplicate_or_pairs(entities)
    _resolve_category_attribute_overlap(entities)
    _resolve_tag_attribute_overlap(entities)
    _prune_tag_covered_attrs(entities)
    _prune_redundant_attributes(entities)

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

        overlapping_cat_slugs = [
            cat_slug for cat_slug, c_tokens in cat_tokens_map.items()
            if attr_tokens <= c_tokens or c_tokens <= attr_tokens or attr_slug == cat_slug
        ]

        if not overlapping_cat_slugs:
            continue

        actual_tax = ""
        if loader and loader.all_attributes_raw:
            attr_label_norm = attr_label.lower().strip()
            for a in loader.all_attributes_raw:
                # attribute_name is the hyphenated form matching entities.attributes
                # dict keys (e.g. "quick-ship"); attribute_label is the human-readable
                # form (e.g. "Quick Ship") and won't match for multi-word names.
                name_raw = (a.get("attribute_name") or "").lower().strip()
                label_raw = (a.get("attribute_label") or a.get("name") or "").lower().strip()
                if attr_label_norm in (name_raw, label_raw, label_raw.replace(" ", "-")):
                    actual_tax = a.get("taxonomy", "")
                    break

        if actual_tax:
            entities.attr_tag_or_pairs.append({
                "cat_slugs": overlapping_cat_slugs,
                "attr_taxonomy": actual_tax,
                "attr_term": attr_slug,
            })
            for slug in overlapping_cat_slugs:
                entities.target_category_slugs.discard(slug)
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