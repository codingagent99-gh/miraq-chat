"""
store_loader/lookup_builder.py — Builds all in-memory lookup indexes
from raw WooCommerce data: category keywords, tag/attribute indexes,
product search index, and longest-match catalog.
"""

import os
import re
import json
import time
from collections import Counter
from typing import Dict, List

from chat_logger import get_logger
from models.catalog import (
    CatalogAttribute,
    CatalogAttributeTerm,
    CatalogCategory,
    CatalogTag,
)
from store_loader.config import ECOMMERCE_BACKEND
logger = get_logger("miraq_chat")


# ══════════════════════════════════════════════════════════════
# GENERIC TERM DETECTION
# ══════════════════════════════════════════════════════════════

def build_store_generic_terms(categories: list) -> set:
    """Detect words that appear in 2+ category names (noise words for extraction)."""
    word_counts = Counter()
    valid = [c for c in categories if c.get("slug") != "uncategorized" and c.get("count", 0) > 0]
    for cat in valid:
        for word in re.split(r"[\s\-_/&]+", cat.get("name", "").lower()):
            word = word.strip()
            if word and len(word) > 2:
                word_counts[word] += 1
    generic = set()
    for word, count in word_counts.items():
        if count >= 2:
            solo = sum(1 for c in valid if c.get("name", "").lower().strip() == word)
            if (count - solo) > solo:
                generic.add(word)
    return generic


# ══════��═══════════════════════════════════════════════════════
# CATEGORY KEYWORD GENERATION
# ══════════════════════════════════════════════════════════════

def generate_category_keywords(
    cat_entry: Dict,
    category_keywords: Dict[str, int],
    category_by_id: Dict[int, Dict],
    category_synonyms: Dict[str, str],
    store_generic_terms: set,
):
    """Generate search keywords for a single category entry."""
    cat_id = cat_entry["id"]
    name = cat_entry["name"].lower().strip()
    slug = cat_entry["slug"]
    cat_count = cat_entry.get("count", 0)

    def _register(kw: str, cid: int):
        if kw not in category_keywords:
            category_keywords[kw] = cid
        else:
            existing_id = category_keywords[kw]
            existing_count = (category_by_id.get(existing_id) or {}).get("count", 0)
            if cat_count < existing_count:
                category_keywords[kw] = cid

    _register(name, cat_id)

    words = re.split(r'[\s\-_/&]+', name)
    raw_words = [w for w in words if w.strip()]
    is_single = len(raw_words) <= 1

    if is_single:
        for word in raw_words:
            if len(word) > 2:
                _register(word, cat_id)
                if word.endswith("s") and len(word) > 3:
                    _register(word[:-1], cat_id)
                else:
                    _register(word + "s", cat_id)

    slug_words = slug.replace("-", " ")
    if slug_words != name:
        _register(slug_words, cat_id)

    for original, variant in category_synonyms.items():
        if original in name:
            _register(name.replace(original, variant), cat_id)

    for suffix in store_generic_terms:
        _register(f"{name} {suffix}", cat_id)
        if is_single:
            for word in raw_words:
                if len(word) > 2:
                    _register(f"{word} {suffix}", cat_id)


# ══════════════════════════════════════════════════════════════
# LONGEST-MATCH CATALOG
# ══════════════════════════════════════════════════════════════

def build_longest_match_catalog(
    product_by_name_lower: dict,
    category_by_name_lower: dict,
    all_attributes_raw: list,
    tag_by_name_lower: dict,
) -> List[tuple]:
    """
    Build a pre-sorted list of all store terms (longest to shortest)
    for O(1)-style access during chat message parsing.
    """
    items = []

    # Products
    for name, data in product_by_name_lower.items():
        items.append((name, 'product', data))

    # Categories (skip 0-count)
    for name, data in category_by_name_lower.items():
        if data.get("count", 0) == 0:
            continue
        items.append((name, 'category', data))

    # Attributes (with combined "term + label" support)
    for attr in all_attributes_raw:
        label_raw = attr.get("attribute_label") or attr.get("name") or attr.get("attribute_name") or ""
        label = label_raw.lower().strip()

        for term in attr.get("terms", []):
            term_name = term.get("name", "").lower().strip()
            payload = {
                'label': label,
                'slug': term.get("slug"),
                'attribute_name': attr.get("attribute_name", ""),
                'name': term.get("name", ""),
            }
            items.append((term_name, 'attribute', payload))
            if label and label not in term_name:
                items.append((f"{term_name} {label}", 'attribute', payload))

    # Tags (skip 0-count)
    for name, data in tag_by_name_lower.items():
        if data.get("count", 0) == 0:
            continue
        items.append((name, 'tag', data))

    items.sort(key=lambda x: len(x[0]), reverse=True)
    return items


# ══════════════════════════════════════════════════════════════
# MASTER LOOKUP BUILDER
# ══════════════════════════════════════════════════════════════

class _LookupStaging:
    """Write-through staging view over a StoreLoader.

    Reads fall through to the real loader; writes are captured locally.
    ``publish()`` copies every captured attribute onto the loader in a single
    ``__dict__.update()``.

    Why this exists: build_all_lookups used to reset ``category_by_name_lower``,
    ``product_by_name_lower`` and friends to ``{}`` on the LIVE loader and then
    repopulate them in place. ``StoreLoader._lock`` is only held against a second
    ``load_all()`` -- no request thread ever takes it -- and gunicorn runs
    gthread with 4 threads per worker. So any request that landed mid-rebuild
    read a half-built or empty index and silently failed to resolve a category
    the store definitely has. That is a wrong answer, not an error, which is why
    it never showed up as one. Staging closes the window.
    """

    __slots__ = ("_loader", "_staged")

    def __init__(self, loader, raw=None):
        object.__setattr__(self, "_loader", loader)
        object.__setattr__(self, "_staged", dict(raw or {}))

    def __getattr__(self, name):
        staged = object.__getattribute__(self, "_staged")
        if name in staged:
            return staged[name]
        return getattr(object.__getattribute__(self, "_loader"), name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, "_staged")[name] = value

    def publish(self):
        # dict.update is a single C-level call, so under the GIL a reader sees
        # either the whole old set or the whole new set.
        loader = object.__getattribute__(self, "_loader")
        loader.__dict__.update(object.__getattribute__(self, "_staged"))


def build_all_lookups(loader, raw=None):
    """Rebuild every lookup index and swap it in atomically.

    ``raw`` is the freshly fetched payload (categories/tags/products/...).
    Passing it here rather than assigning it on the loader first means the raw
    data and the indexes derived from it become visible in the same instant.
    """
    staging = _LookupStaging(loader, raw)
    _build_lookups_into(staging)
    staging.publish()


def _build_lookups_into(loader):
    """
    Build all in-memory lookup dictionaries from raw data.
    Mutates the loader instance in-place.
    """
    loader._store_generic_terms = build_store_generic_terms(loader.categories)

    # Reset
    loader.attribute_by_id = {}
    loader.category_by_id = {}
    loader.category_by_name_lower = {}
    loader.category_slugs_by_name = {}
    loader.tag_by_id = {}
    loader.tag_by_name_lower = {}
    loader.product_by_name_lower = {}
    loader.product_name_tokens = []
    loader.category_keywords = {}

    # Attributes
    if loader.all_attributes_raw:
        for attr in loader.all_attributes_raw:
            if not attr.get("visible", True):
                continue
            taxonomy_slug = attr.get("taxonomy", "")
            attr_id = attr.get("attribute_id")
            entry = {
                "id": attr_id,
                "name": attr.get("attribute_label") or attr.get("name") or attr.get("attribute_name") or "",
                "slug": taxonomy_slug,
            }
            loader.attribute_by_id[attr_id] = entry

    # Categories
    for cat in loader.categories:
        cat_id = cat["id"]
        name_lower = cat.get("name", "").lower()
        entry = {"id": cat_id, "name": cat["name"], "slug": cat.get("slug", ""), "count": cat.get("count", 0)}
        loader.category_by_id[cat_id] = entry
        loader.category_by_name_lower[name_lower] = entry
        if name_lower not in loader.category_slugs_by_name:
            loader.category_slugs_by_name[name_lower] = []
        loader.category_slugs_by_name[name_lower].append(entry["slug"])

        if entry["slug"] != "uncategorized" and entry["count"] > 0:
            generate_category_keywords(
                entry, loader.category_keywords, loader.category_by_id,
                loader._category_synonyms, loader._store_generic_terms,
            )

    # Tags
    for tag in loader.tags:
        name_lower = tag.get("name", "").lower()
        entry = {"id": tag["id"], "name": tag["name"], "slug": tag["slug"], "count": tag.get("count", 0)}
        loader.tag_by_id[tag["id"]] = entry
        loader.tag_by_name_lower[name_lower] = entry

    # Neutral catalog indexes (Phase 4a; additive, dual-populated with legacy Woo indexes)
    loader.attribute_by_key = {}
    loader.category_by_key = {}
    loader.tag_by_key = {}

    for attr in loader.all_attributes_raw or []:
        taxonomy = attr.get("taxonomy", "")
        key = attr.get("attribute_name", "").lower()
        if not key:
            continue

        label = (
            attr.get("attribute_label")
            or attr.get("name")
            or attr.get("attribute_name")
            or key.title()
        )
        terms = tuple(
            CatalogAttributeTerm(
                key=term.get("slug", ""),
                name=term.get("name", ""),
                count=term.get("count", 0),
                backend_ref={"slug": term.get("slug", ""), "id": term.get("id")},
            )
            for term in attr.get("terms", [])
            if term.get("slug")
        )
        loader.attribute_by_key[key] = CatalogAttribute(
            key=key,
            label=label,
            terms=terms,
            backend_ref={
                "taxonomy": taxonomy,
                "id": attr.get("attribute_id"),
                "attribute_name": attr.get("attribute_name", ""),
            },
        )

    for cat in loader.categories:
        key = cat.get("slug", "")
        if not key:
            continue

        parent_key = None
        parent_id = cat.get("parent", 0)
        if parent_id:
            parent_entry = loader.category_by_id.get(parent_id)
            if parent_entry and parent_entry.get("slug"):
                parent_key = parent_entry["slug"]

        loader.category_by_key[key] = CatalogCategory(
            key=key,
            name=cat.get("name", ""),
            parent_key=parent_key,
            count=cat.get("count", 0),
            backend_ref={
                "id": cat.get("id"),
                "slug": key,
                "parent_id": cat.get("parent", 0),
            },
        )

    for tag in loader.tags:
        key = tag.get("slug", "")
        if not key:
            continue
        loader.tag_by_key[key] = CatalogTag(
            key=key,
            name=tag.get("name", ""),
            count=tag.get("count", 0),
            backend_ref={"id": tag.get("id"), "slug": key},
        )

    # Products
    for product in loader.products:
        status = product.get("status")
        # "active" = Shopify published, "publish" = WooCommerce published
        # None = safe to include (e.g. local cache data without status field)
        if status is not None and status not in ("active", "publish"):
            continue

        name = (product.get("name") or "").strip()
        if not name:
            continue
        entry = {
            "id":         product.get("_shopify_gid") or product.get("id"),
            "numeric_id": product.get("id"),
            "name":       name,
            "slug":       product.get("slug", ""),
        }
        loader.product_by_name_lower[name.lower()] = entry
        
    logger.debug(f"lookup_builder: product_by_name_lower keys = {list(loader.product_by_name_lower.keys())}")

    # Longest-match catalog
    loader.longest_match_catalog = build_longest_match_catalog(
        loader.product_by_name_lower,
        loader.category_by_name_lower,
        loader.all_attributes_raw,
        loader.tag_by_name_lower,
    )

    # Build the Phase 1 regex index here rather than letting the first
    # request trigger it. The cache in catalog_parser is unlocked on purpose,
    # so a lazy build costs not one request but every request that arrives
    # while it runs -- on the Sep 2026 cold start that was the first SIX,
    # at ~1.6s each. Building it as part of the catalog means no request ever
    # pays, including after a webhook-triggered reload.
    try:
        from parsers.catalog_parser import _get_phase1_index
        _get_phase1_index(loader, loader.longest_match_catalog)
        logger.info("lookup_builder: Phase 1 index prewarmed")
    except Exception as exc:
        # Never fail a catalog load over a warm-up: the first request will
        # just build it lazily, exactly as before.
        logger.warning(f"lookup_builder: Phase 1 index prewarm skipped — {exc}")
    
    # Fuzzy typo-correction vocabulary (utils/typo_correction.py)
    build_fuzzy_vocab(loader)
        
        
# ══════════════════════════════════════════════════════════════
# FUZZY TYPO-CORRECTION VOCABULARY
# ══════════════════════════════════════════════════════════════

def build_fuzzy_vocab(loader):
    """
    Build the search space for pre-classification typo correction:

        loader.fuzzy_vocab_types     — {term: type} for catalog terms only
                                        (category | tag | attribute | product_word)
        loader.fuzzy_protected_words — frozenset of words NEVER corrected
                                        (stop/noise/synonym words + all catalog terms)
        loader.fuzzy_vocab_terms     — list over catalog terms ∪ protected words;
                                        the combined space matters: a misspelled
                                        glue word must be able to win against a
                                        catalog term ("shwo"→"show", not →"shower")

    Multi-word catalog names are indexed as their individual words
    (product names especially) — token-level correction fixes each word,
    then Phase 1's longest-match reassembles the phrase.
    """
    from utils.entity_helpers import STOP_WORDS
    from config.store_config import GENERIC_NOISE_WORDS, GENERIC_WORD_SYNONYMS
    from utils.typo_correction import CONTROL_PHRASE_WORDS

    vocab_types: dict = {}

    def _add(term: str, vtype: str):
        for word in re.split(r"[\s\-_/&]+", term.lower().strip()):
            word = word.strip()
            # <4 chars is below the correction threshold; digits are skipped
            # by the corrector anyway (dimensions, counts).
            if len(word) >= 4 and word.isalpha() and word not in vocab_types:
                vocab_types[word] = vtype

    for name, data in loader.category_by_name_lower.items():
        if data.get("count", 0) > 0 and data.get("slug") != "uncategorized":
            _add(name, "category")
    for name, data in loader.tag_by_name_lower.items():
        if data.get("count", 0) > 0:
            _add(name, "tag")
    for attr in loader.all_attributes_raw or []:
        label = attr.get("attribute_label") or attr.get("name") or ""
        if label:
            _add(label, "attribute")
        for term in attr.get("terms", []):
            _add(term.get("name", ""), "attribute")
    for name in loader.product_by_name_lower:
        _add(name, "product_word")

    protected = set(vocab_types)
    protected.update(w.lower() for w in STOP_WORDS)
    protected.update(w.lower() for w in GENERIC_NOISE_WORDS)
    protected.update(w.lower() for w in GENERIC_WORD_SYNONYMS)
    protected.update(w.lower() for w in GENERIC_WORD_SYNONYMS.values())
    # Conversational control vocabulary ("cancel", "browse", "checkout", ...)
    # — see CONTROL_PHRASE_WORDS in utils/typo_correction.py for why.
    protected.update(CONTROL_PHRASE_WORDS)

    # Every word the intent classifier's regexes key off ("bulk", "qty",
    # "specs", ...). Derived from the evaluators themselves rather than
    # hand-listed, so adding an evaluator can't silently reintroduce the
    # "bulk order -> did you mean bark/back/dusk?" class of bug.
    # See classifier/keywords.py.
    try:
        from classifier.keywords import get_classifier_keywords
        protected.update(get_classifier_keywords())
    except Exception as exc:          # never block vocab build on this
        logger.error(f"build_fuzzy_vocab: classifier keyword union failed: {exc}")

    # Rep display names from the project_rep directory. A person's name is
    # out-of-vocabulary against a tile catalog and therefore a prime candidate
    # for being "corrected" into a product word — the same failure that turned
    # "all time" into a "did you mean tile or tide?" prompt.
    #
    # This only stops the token being REWRITTEN. It does not add it to
    # vocab_types, so a name that is also a catalog term (this store has an
    # "Adams" product) still matches the product exactly as before; the
    # rep-vs-product decision is made later, by find_reps_in_text's collision
    # rule, not here.
    try:
        from utils.checkout_fields import rep_name_tokens
        _reps = rep_name_tokens()
        if _reps:
            protected.update(_reps)
            logger.debug(f"build_fuzzy_vocab: protected {len(_reps)} rep name token(s)")
    except Exception as exc:          # never block vocab build on this
        logger.error(f"build_fuzzy_vocab: rep name union failed: {exc}")

    loader.fuzzy_vocab_types = vocab_types
    loader.fuzzy_protected_words = frozenset(protected)
    loader.fuzzy_vocab_terms = list(protected)  # superset: catalog ∪ glue words

    logger.info(
        f"lookup_builder: fuzzy vocab built | catalog_terms={len(vocab_types)} | "
        f"total_search_space={len(loader.fuzzy_vocab_terms)}"
    )