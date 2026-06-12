"""
handlers/search_refinement.py — Conversational search refinement (button-based).

Design contract (do not reintroduce guessing):
  - Typing ANY product-search message CONTINUES the current search. Filters
    accumulate across turns.
  - The ONLY way to start over is the "New Search" reset, intercepted in
    chat.py before classification. This module never decides new-vs-continue.

Per-attribute accumulation rule, driven by the catalog + SINGLE_VALUE_ATTRIBUTES:
  - empty slot      -> fill
  - single-value    -> replace (e.g. price, size)
  - multi-value     -> append alongside (default; e.g. colour - a tile can be
                      beige AND white)

State lives in conversation.context_data["active_search"], keyed by the NEUTRAL
attribute key (taxonomy minus "pa_", hyphens->spaces, lowercased) - the SAME key
entities.attributes uses. A mismatch here silently no-ops every refinement, so
merge_into_active_search logs when it sees an unknown key shape.

Public API:
  - merge_into_active_search(entities, active_search) -> updated dict (mutates entities)
  - save_active_search(user_context, entities)
  - clear_active_search(user_context)
  - active_search_is_fresh(active_search) -> bool
  - describe_active_filters(entities) -> str
"""

import time
from typing import Optional

from models import ExtractedEntities
from classifier.consolidation import consolidate_entities
from models.domain import Intent
from config.store_config import SINGLE_VALUE_ATTRIBUTES
from chat_logger import get_logger

logger = get_logger("miraq_chat")

# A search context older than this (seconds) is stale - next query starts fresh.
_ACTIVE_SEARCH_TTL_SECONDS = 15 * 60


def active_search_is_fresh(active_search: Optional[dict]) -> bool:
    """True if an active_search exists and hasn't passed its TTL."""
    if not active_search:
        return False
    ts = active_search.get("_ts")
    if not ts:
        return False
    return (time.time() - ts) <= _ACTIVE_SEARCH_TTL_SECONDS


def _is_single_value(attr_key: str) -> bool:
    """Whether an attribute replaces (single) or appends (multi) on refinement."""
    return attr_key.lower().strip() in SINGLE_VALUE_ATTRIBUTES


def _append_value(existing: str, new: str) -> str:
    """Append new CSV value(s) to existing, de-duplicating, preserving order."""
    seen = [v.strip() for v in existing.split(",") if v.strip()]
    for v in (x.strip() for x in new.split(",") if x.strip()):
        if v not in seen:
            seen.append(v)
    return ",".join(seen)


def merge_into_active_search(entities: ExtractedEntities, active_search: Optional[dict]) -> dict:
    """
    Merge the prior active search's accumulated filters into the current
    `entities`, applying per-attribute append/replace rules, then re-consolidate.

    Returns the updated active_search dict (caller persists it). Mutates
    `entities` in place so the current turn searches the full accumulated set.
    """
    if not active_search_is_fresh(active_search):
        return active_search or {}

    slots = active_search.get("slots", {})

    # -- Attributes: fill / replace / append per cardinality --
    for prior_key, prior_val in slots.get("attributes", {}).items():
        cur_val = entities.attributes.get(prior_key)
        if cur_val is None:
            entities.attributes[prior_key] = prior_val          # carry forward
        elif _is_single_value(prior_key):
            pass                                                 # replace = keep current
        else:
            entities.attributes[prior_key] = _append_value(str(prior_val), str(cur_val))

    # Diagnostic: prior had attribute keys but none matched current key shape on
    # a turn that DID extract attributes -> the silent key-mismatch signature.
    if slots.get("attributes") and entities.attributes:
        prior_keys = set(slots["attributes"].keys())
        cur_keys = set(entities.attributes.keys())
        if prior_keys and not (prior_keys & cur_keys) and not prior_keys <= cur_keys:
            logger.warning(
                "search_refinement: possible attribute-key mismatch - "
                f"prior_keys={sorted(prior_keys)} cur_keys={sorted(cur_keys)}. "
                "Slot keys must match entities.attributes neutral keys."
            )

    # -- Tags (inherently multi -> additive) --
    for t in slots.get("tags", []):
        if t not in entities.tag_slugs:
            entities.tag_slugs.append(t)

    # -- Categories (additive set) --
    prior_cats = slots.get("categories", [])
    if prior_cats:
        if getattr(entities, "target_category_slugs", None) is None:
            entities.target_category_slugs = set()
        entities.target_category_slugs.update(prior_cats)

    # -- Price (single-value: current turn wins; else carry prior) --
    if entities.min_price is None and slots.get("min_price") is not None:
        entities.min_price = slots["min_price"]
    if entities.max_price is None and slots.get("max_price") is not None:
        entities.max_price = slots["max_price"]

    # -- OR pairs carry forward --
    for op in slots.get("attr_tag_or_pairs", []):
        if op not in entities.attr_tag_or_pairs:
            entities.attr_tag_or_pairs.append(op)

    # -- Re-consolidate the COMBINED set (category<->attribute overlaps -> OR
    #    pairs, duplicate OR pairs pruned) as a single-turn query would be. --
    consolidate_entities(Intent.PRODUCT_SEARCH, entities, "")

    logger.info(
        f"search_refinement: merged | attrs={dict(entities.attributes)} | "
        f"tags={entities.tag_slugs} | cats={list(getattr(entities, 'target_category_slugs', set()))} | "
        f"price=({entities.min_price},{entities.max_price})"
    )

    return _snapshot(entities)


def _snapshot(entities: ExtractedEntities) -> dict:
    """Capture the current (consolidated) filter set as a fresh active_search."""
    return {
        "_ts": time.time(),
        "slots": {
            "attributes": dict(entities.attributes),
            "tags": list(entities.tag_slugs),
            "categories": list(getattr(entities, "target_category_slugs", set())),
            "attr_tag_or_pairs": list(getattr(entities, "attr_tag_or_pairs", [])),
            "min_price": entities.min_price,
            "max_price": entities.max_price,
        },
    }


def save_active_search(user_context: dict, entities: ExtractedEntities) -> None:
    """Persist the current consolidated filter set. Call only on successful results."""
    user_context["active_search"] = _snapshot(entities)


def clear_active_search(user_context: dict) -> None:
    """Drop the active search (New Search pressed, or context gone stale)."""
    user_context.pop("active_search", None)


def describe_active_filters(entities: ExtractedEntities) -> str:
    """Readable summary of the active filter set, e.g. 'beige + concrete look, under $500'."""
    parts = []

    cat_name = getattr(entities, "category_name", None)
    if cat_name:
        parts.append(cat_name)

    for slug in entities.tag_slugs:
        parts.append(slug.replace("-", " "))

    for val in entities.attributes.values():
        for v in str(val).split(","):
            v = v.strip().replace("-", " ")
            if v:
                parts.append(v)

    base = " + ".join(p for p in parts if p)

    price = ""
    if entities.min_price is not None and entities.max_price is not None:
        price = f"${entities.min_price:g}\u2013${entities.max_price:g}"
    elif entities.max_price is not None:
        price = f"under ${entities.max_price:g}"
    elif entities.min_price is not None:
        price = f"over ${entities.min_price:g}"

    if base and price:
        return f"{base}, {price}"
    return base or price