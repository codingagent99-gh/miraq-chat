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