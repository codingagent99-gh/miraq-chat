"""
config/store_config.py — Store-specific config that cannot be derived from WooCommerce data.

All keyword lists (FINISH_CANDIDATES, VISUAL_KEYWORDS, COLOR_KEYWORDS, etc.)
and ATTR_* slug constants have been removed — these are now built dynamically
from the live /custom-api/v1/all-attributes response at startup.

What remains here is config that genuinely requires human definition:
  1. ORIGIN_KEYWORDS   — demonym synonyms ("italian" → "italy") that WooCommerce
                         terms will never contain.
  2. PRODUCT_TAG_SLUGS — special tags your code treats with distinct logic.
  3. FALLBACK_SEARCH_TERM / PRODUCT_TYPE_TERMS — app-level fallback strings.
"""

import json
import os


# ═══════════════════════════════════════════════════════════════
# ORIGIN KEYWORDS
# Maps user keyword/demonym → normalized origin value for tag lookup.
# The ONLY keyword map that can't be derived from live attribute terms —
# WooCommerce stores "Italy" but users say "Italian".
# Override via env var ORIGIN_KEYWORDS_JSON (JSON object).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_ORIGIN_KEYWORDS = {
    "italy":      "italy",
    "italian":    "italy",
    "turkey":     "turkey",
    "turkish":    "turkey",
    "spain":      "spain",
    "spanish":    "spain",
    "china":      "china",
    "chinese":    "china",
    "india":      "india",
    "indian":     "india",
    "portugal":   "portugal",
    "portuguese": "portugal",
}

def _load_origin_keywords() -> dict:
    raw = os.getenv("ORIGIN_KEYWORDS_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return dict(_DEFAULT_ORIGIN_KEYWORDS)

ORIGIN_KEYWORDS: dict = _load_origin_keywords()


# ═══════════════════════════════════════════════════════════════
# PRODUCT TAG SLUGS
# Special tags treated with distinct business logic.
# Override via env var PRODUCT_TAG_SLUGS_JSON.
# ═══════════════════════════════════════════════════════════════

_DEFAULT_PRODUCT_TAG_SLUGS = {
    "quick_ship": "quick-ship",
    "chip_card":  "chip-card",
}

def _load_product_tag_slugs() -> dict:
    raw = os.getenv("PRODUCT_TAG_SLUGS_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return dict(_DEFAULT_PRODUCT_TAG_SLUGS)

PRODUCT_TAG_SLUGS: dict = _load_product_tag_slugs()

TAG_SLUG_QUICK_SHIP = PRODUCT_TAG_SLUGS.get("quick_ship", "quick-ship")
TAG_SLUG_CHIP_CARD  = PRODUCT_TAG_SLUGS.get("chip_card",  "chip-card")


# ═══════════════════════════════════════════════════════════════
# FALLBACK SEARCH TERM
# ═══════════════════════════════════════════════════════════════

FALLBACK_SEARCH_TERM: str = os.getenv("FALLBACK_SEARCH_TERM", "products")


# ═══════════════════════════════════════════════════════════════
# PRODUCT TYPE TERMS  (classifier fallback for PRODUCT_LIST intent)
# Override via env var PRODUCT_TYPE_TERMS_JSON (JSON array).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_PRODUCT_TYPE_TERMS = ["items", "item", "products", "product"]

def _load_product_type_terms() -> list:
    raw = os.getenv("PRODUCT_TYPE_TERMS_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return list(_DEFAULT_PRODUCT_TYPE_TERMS)

PRODUCT_TYPE_TERMS: list = _load_product_type_terms()

# ═══════════════════════════════════════════════════════════════
# GENERIC NOISE WORDS (Stop-Word Hijacking Prevention)
# Words stripped from extraction text to prevent category/tag hijacking.
# Override via env var GENERIC_NOISE_WORDS_JSON (JSON array).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_GENERIC_NOISE_WORDS = ["item", "items", "product", "products"]

def _load_generic_noise_words() -> list:
    raw = os.getenv("GENERIC_NOISE_WORDS_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return list(_DEFAULT_GENERIC_NOISE_WORDS)

GENERIC_NOISE_WORDS: list = _load_generic_noise_words()


# ═══════════════════════════════════════════════════════════════
# SINGLE-VALUE ATTRIBUTES  (conversational search refinement)
# When refining an active search, a new value for an attribute that is already
# set either REPLACES the old value (single-value) or is ADDED alongside it
# (multi-value, the default).
#
# WooCommerce cannot tell us this — every pa_* attribute is structurally
# multi-value — so it is a human/semantic decision. Default is APPEND (safe:
# never produces a zero-result from an unwanted replace). List here only the
# attributes that should REPLACE instead.
#
# IMPORTANT: keys must be the NEUTRAL attribute key, matching how
# entities.attributes is keyed — i.e. taxonomy with "pa_" stripped and
# hyphens turned to spaces (e.g. pa_quick-ship -> "quick ship"), lowercased.
# `price` is always single-value and is handled in code, not listed here.
# Override via env var SINGLE_VALUE_ATTRIBUTES_JSON (JSON array).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_SINGLE_VALUE_ATTRIBUTES = ["size", "thickness", "weight", "width", "length", "depth"]

def _load_single_value_attributes() -> set:
    raw = os.getenv("SINGLE_VALUE_ATTRIBUTES_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return {str(x).lower().strip() for x in parsed}
        except Exception:
            pass
    return {x.lower().strip() for x in _DEFAULT_SINGLE_VALUE_ATTRIBUTES}

SINGLE_VALUE_ATTRIBUTES: set = _load_single_value_attributes()

# ═══════════════════════════════════════════════════════════════
# SEMANTIC AUTO-APPLY THRESHOLD
# When there is exactly one semantic match candidate and its vector score
# meets or exceeds this value, the match is applied silently — no prompt.
# Below the threshold (or multiple candidates), clarification is shown.
# Lower = more auto-applies (faster UX, occasional silent wrong match).
# Override via env var SEMANTIC_AUTO_APPLY_THRESHOLD (float, e.g. "0.55").
# ═══════════════════════════════════════════════════════════════

SEMANTIC_AUTO_APPLY_THRESHOLD: float = float(
    os.getenv("SEMANTIC_AUTO_APPLY_THRESHOLD", "0.55")
)

# ═══════════════════════════════════════════════════════════════
# ATTRIBUTE DISPLAY OVERRIDES  (filter summary display)
# Some attribute taxonomies have a value that's meaningless on its own when
# shown to a user (e.g. pa_quick-ship's term is just "yes"/"no", not a
# descriptive value like "green" or "matte"). This maps such taxonomies
# directly to the exact display text that should be shown in filter
# summaries (describe_active_filters), instead of the raw attr_term value.
# Add a new entry here any time another boolean/flag-style attribute needs
# its own display text — no code changes needed elsewhere.
# Override via env var ATTRIBUTE_DISPLAY_OVERRIDES_JSON (JSON object).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_ATTRIBUTE_DISPLAY_OVERRIDES = {
    "quick-ship":    "Quick Ship",
    "pa_quick-ship": "Quick Ship",
}

def _load_attribute_display_overrides() -> dict:
    raw = os.getenv("ATTRIBUTE_DISPLAY_OVERRIDES_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k).lower().strip(): str(v) for k, v in parsed.items()}
        except Exception:
            pass
    return dict(_DEFAULT_ATTRIBUTE_DISPLAY_OVERRIDES)

ATTRIBUTE_DISPLAY_OVERRIDES: dict = _load_attribute_display_overrides()

# ═══════════════════════════════════════════════════════════════
# ATTRIBUTE VALUE PHRASES  (tag/attribute overlap merging)
# Maps a specific attribute:value pair directly to the phrase it should be
# treated as equivalent to — e.g. attribute "quick-ship" with value "yes"
# means the same thing as if the user had typed "quick ship". The phrase
# does NOT need to resemble the value's own text in any way (it isn't a
# synonym lookup or a fuzzy match) — this is a direct, hand-declared
# equivalence, used by classifier/consolidation.py
# (_resolve_tag_attribute_overlap) to decide whether a tag and an
# attribute:value pair refer to the same underlying concept.
#
# IMPORTANT: once an attribute key appears here, ALL of its values are
# governed by this mapping for label-matching purposes — any value with
# no entry simply does not match via label (fails closed), rather than
# falling back to comparing the raw label text. This is what stops e.g.
# quick-ship:"no" from merging just because the label "quick-ship"
# happens to equal the tag's name.
#
# Shape: {attribute_key: {value: phrase}}. Keys/values matched
# case-insensitively. Override via env var ATTRIBUTE_VALUE_PHRASES_JSON
# (JSON object of the same shape).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_ATTRIBUTE_VALUE_PHRASES = {
    "quick-ship": {"yes": "quick ship"},
}

def _load_attribute_value_phrases() -> dict:
    raw = os.getenv("ATTRIBUTE_VALUE_PHRASES_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {
                    str(k).lower().strip(): {str(vk).lower().strip(): str(vv) for vk, vv in v.items()}
                    for k, v in parsed.items()
                }
        except Exception:
            pass
    return {
        k.lower().strip(): {vk.lower().strip(): vv for vk, vv in v.items()}
        for k, v in _DEFAULT_ATTRIBUTE_VALUE_PHRASES.items()
    }

ATTRIBUTE_VALUE_PHRASES: dict = _load_attribute_value_phrases()

# ═══════════════════════════════════════════════════════════════
# ATTRIBUTE DISAMBIGUATION GROUPS  (same-value collision check)
# Most attribute taxonomies that share a value are alternate/parallel
# representations of the SAME concept (e.g. Color and Colors 2 — both just
# "what color is this") — that's already handled correctly by the OR-merge
# in api_builder/filter_builder.py (_build_attribute_conditions groups
# same-valued attributes across taxonomies into one OR condition), with no
# clarification needed.
#
# A few pairs are genuinely DIFFERENT concepts that happen to coincide on
# a shared value (e.g. "12x12" is a valid Sample Size AND a valid Tile
# Size, but those are two different physical facts about the product) —
# for those, silently OR-ing is wrong; the user should be asked which one
# they meant. List ONLY those known-ambiguous pairs here.
#
# Default is "assume harmless, no clarification" for anything NOT listed —
# this only needs entries for pairs you've actually confirmed are
# ambiguous, not a prediction of every alias pair that might exist.
# Override via env var ATTRIBUTE_DISAMBIGUATION_GROUPS_JSON (JSON array of arrays).
# ═══════════════════════════════════════════════════════════════

_DEFAULT_ATTRIBUTE_DISAMBIGUATION_GROUPS = [
    ["sample size", "tile size"],
]

def _load_attribute_disambiguation_groups() -> list:
    raw = os.getenv("ATTRIBUTE_DISAMBIGUATION_GROUPS_JSON", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [[str(x).lower().strip() for x in group] for group in parsed]
        except Exception:
            pass
    return [[k.lower().strip() for k in group] for group in _DEFAULT_ATTRIBUTE_DISAMBIGUATION_GROUPS]

ATTRIBUTE_DISAMBIGUATION_GROUPS: list = _load_attribute_disambiguation_groups()