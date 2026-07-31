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
  - describe_active_filters_labeled(entities) -> str
  - detect_slot_conflicts(entities, active_search) -> list[dict]
"""

import time
from typing import Optional
from config.store_config import SINGLE_VALUE_ATTRIBUTES, ATTRIBUTE_DISPLAY_OVERRIDES
from models import ExtractedEntities
from classifier.consolidation import consolidate_entities
from models.domain import Intent
from chat_logger import get_logger
import re
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


def detect_slot_conflicts(entities: ExtractedEntities, active_search: Optional[dict]) -> list[dict]:
    """
    Returns multi-value attribute slots where active_search already holds a value
    AND the current turn extracted a *different* value for the same key.

    Checks two locations:
      1. entities.attributes (keyed attr values)
      2. entities.attr_tag_or_pairs (OR-pair values, e.g. WGC color stored as
         {tag_slug, attr_taxonomy, attr_term}) — grouped by attr_taxonomy/attr_key.

    Single-value attributes (always-replace) are excluded — they have no ambiguity.
    Incoming values already present in the existing set are excluded (no new info).
    Returns [] when active_search is stale or absent.
    Each entry: {"key": str, "existing": str, "incoming": str}
      OR-pair entries also carry: {"type": "or_pair", "attr_taxonomy": str}
    """
    if not active_search_is_fresh(active_search):
        return []

    slots     = active_search.get("slots", {})
    conflicts = []

    # ── 1. Plain attributes ────────────────────────────────────────────────
    for key, incoming_val in entities.attributes.items():
        if _is_single_value(key):
            continue                              # silent replace, no prompt needed

        existing_val = slots.get("attributes", {}).get(key)
        if not existing_val:
            continue                              # empty slot: fill directly, no conflict

        incoming_str = str(incoming_val).strip()
        existing_str = str(existing_val).strip()
        if not incoming_str:
            continue

        # Only flag when the incoming set is not already a subset of existing
        existing_values = {v.strip() for v in existing_str.split(",") if v.strip()}
        incoming_values = {v.strip() for v in incoming_str.split(",") if v.strip()}

        if not incoming_values.issubset(existing_values):
            conflicts.append({
                "key":      key,
                "existing": existing_str,
                "incoming": incoming_str,
            })

    # ── 2. OR pairs — grouped by attr_taxonomy/attr_key ───────────────────
    # e.g. active has {attr_taxonomy: "pa_colors-2", attr_term: "beige"} and
    # current turn brings {attr_taxonomy: "pa_colors-2", attr_term: "white"}.
    # attr_key is the preferred field; attr_taxonomy is the deprecated alias.
    active_or_pairs   = slots.get("attr_tag_or_pairs", [])
    incoming_or_pairs = getattr(entities, "attr_tag_or_pairs", [])

    if active_or_pairs and incoming_or_pairs:
        # Group active pairs by taxonomy key → set of terms already present
        active_by_tax: dict = {}
        for op in active_or_pairs:
            tax  = op.get("attr_key") or op.get("attr_taxonomy")
            term = op.get("attr_term", "")
            if tax:
                active_by_tax.setdefault(tax, set()).add(term)

        seen_conflict_taxes: set = set()
        for op in incoming_or_pairs:
            tax = op.get("attr_key") or op.get("attr_taxonomy")
            if not tax or tax not in active_by_tax or tax in seen_conflict_taxes:
                continue
            incoming_term  = op.get("attr_term", "")
            existing_terms = active_by_tax[tax]
            if incoming_term and incoming_term not in existing_terms:
                # Build a user-friendly display key:
                # "pa_colors-2" → strip "pa_" → "colors-2" → strip trailing digit suffix → "colors"
                raw = tax[3:] if tax.startswith("pa_") else tax
                parts = raw.split("-")
                while parts and parts[-1].isdigit():
                    parts.pop()
                display_key = " ".join(parts).strip() or tax

                conflicts.append({
                    "key":          display_key,
                    "existing":     ", ".join(sorted(existing_terms)),
                    "incoming":     incoming_term,
                    "type":         "or_pair",
                    "attr_taxonomy": tax,
                })
                seen_conflict_taxes.add(tax)

    return conflicts


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
    prior_groups = [set(g) for g in slots.get("category_groups", [])]
    current_group_sets = [set(g) for g in entities.category_groups]
    for prior_group in prior_groups:
        if prior_group not in current_group_sets:
            entities.add_category_group(prior_group)
            current_group_sets.append(prior_group)

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
            "category_groups": [list(g) for g in getattr(entities, "category_groups", [])],
            "attr_tag_or_pairs": list(getattr(entities, "attr_tag_or_pairs", [])),
            "min_price": entities.min_price,
            "max_price": entities.max_price,
        },
    }


def save_active_search(user_context: dict, entities: ExtractedEntities) -> None:
    """Persist the current consolidated filter set. Call only on successful results."""
    user_context["active_search"] = _snapshot(entities)


def clear_active_search(user_context: dict) -> None:
    """Drop the active search (New Search pressed, or context gone stale).

    "New Search" is documented as the GUARANTEED reset (see routes/chat.py),
    so it must also drop the two per-conversation decline lists. Both are
    append-only and were previously never cleared by anything, which meant a
    single dismissal silently suppressed that suggestion for the entire life
    of the conversation — including across New Search.
    """
    user_context.pop("active_search", None)
    user_context.pop("rejected_semantic_terms", None)
    user_context.pop("typo_suppressed_tokens", None)


def describe_active_filters(entities: ExtractedEntities) -> str:
    """Readable summary of the active filter set, e.g. 'beige + concrete look, under $500'."""
    def _norm(s):
        return re.sub(r'[^a-z0-9]', '', s.lower())

    parts = []
    seen_norm = set()

    def _add(v: str):
        v = v.strip()
        if not v:
            return
        key = _norm(v)
        if key in seen_norm:
            return
        seen_norm.add(key)
        parts.append(v)

    cat_name = getattr(entities, 'category_name', None)
    if not cat_name and getattr(entities, 'target_category_slugs', None):
        cat_name = ", ".join(s.replace("-", " ").title() for s in sorted(entities.target_category_slugs))
    if cat_name:
        _add(cat_name)

    for slug in entities.tag_slugs:
        _add(slug.replace("-", " "))

    for label, val in entities.attributes.items():
        override = ATTRIBUTE_DISPLAY_OVERRIDES.get(label.lower().strip())
        if override:
            _add(override)
            continue
        for v in str(val).split(","):
            _add(v.replace("-", " "))

    for op in getattr(entities, "attr_tag_or_pairs", []):
        term     = op.get("attr_term", "")
        taxonomy = (op.get("attr_taxonomy") or op.get("attr_key") or "").lower().strip()
        override = ATTRIBUTE_DISPLAY_OVERRIDES.get(taxonomy)
        if override:
            _add(override)
        elif term:
            _add(term.replace("-", " "))

    base = " + ".join(parts)

    price = ""
    if entities.min_price is not None and entities.max_price is not None:
        price = f"${entities.min_price:g}–${entities.max_price:g}"
    elif entities.max_price is not None:
        price = f"under ${entities.max_price:g}"
    elif entities.min_price is not None:
        price = f"over ${entities.min_price:g}"

    if base and price:
        return f"{base}, {price}"
    return base or price

def describe_active_filters_labeled(entities: ExtractedEntities) -> str:
    """
    Returns a labeled, markdown-formatted breakdown of active filters for
    zero-result messages — each filter dimension named separately so the
    shopper knows exactly what is and isn't matching.
    e.g. "**Category:** Mosaics · **Color:** beige, white · **Sample Size:** mosaic"
    """
    parts = []

    # Category
    cat_name = getattr(entities, "category_name", None)
    if cat_name:
        parts.append(("Category", cat_name))

    # Plain tag slugs
    if entities.tag_slugs:
        parts.append(("Tag", ", ".join(s.replace("-", " ") for s in entities.tag_slugs)))

    # Plain attributes — key is already a display name (e.g. "sample size")
    for key, val in entities.attributes.items():
        override = ATTRIBUTE_DISPLAY_OVERRIDES.get(key.lower().strip())
        if override:
            parts.append((override, ""))
            continue
        label  = key.replace("-", " ").title()
        values = ", ".join(
            v.strip().replace("-", " ") for v in str(val).split(",") if v.strip()
        )
        if values:
            parts.append((label, values))

    # OR pairs — group by taxonomy, deduplicate terms within each group
    or_by_tax: dict = {}
    or_tax_order: list = []
    for op in getattr(entities, "attr_tag_or_pairs", []):
        tax  = (op.get("attr_key") or op.get("attr_taxonomy", "")).lower().strip()
        term = op.get("attr_term", "").replace("-", " ").strip()
        if not tax or not term:
            continue
        override = ATTRIBUTE_DISPLAY_OVERRIDES.get(tax)
        if override:
            if override not in or_by_tax:
                or_by_tax[override] = []
                or_tax_order.append(override)
            continue
        raw = tax[3:] if tax.startswith("pa_") else tax
        tax_parts = raw.split("-")
        while tax_parts and tax_parts[-1].isdigit():
            tax_parts.pop()
        display_label = " ".join(tax_parts).strip().title() or tax
        if display_label not in or_by_tax:
            or_by_tax[display_label] = []
            or_tax_order.append(display_label)
        if term not in or_by_tax[display_label]:
            or_by_tax[display_label].append(term)

    for label in or_tax_order:
        parts.append((label, ", ".join(or_by_tax[label])))

    # Price
    if entities.min_price is not None and entities.max_price is not None:
        parts.append(("Price", f"${entities.min_price:g}\u2013${entities.max_price:g}"))
    elif entities.max_price is not None:
        parts.append(("Price", f"under ${entities.max_price:g}"))
    elif entities.min_price is not None:
        parts.append(("Price", f"over ${entities.min_price:g}"))

    return " · ".join(f"**{label}:** {values}" for label, values in parts)