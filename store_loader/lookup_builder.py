"""
store_loader/lookup_builder.py — Builds all in-memory lookup indexes
from raw WooCommerce data: category keywords, tag/attribute indexes,
product search index, longest-match catalog, and semantic vectors.
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
from config.store_config import ATTRIBUTE_VALUE_PHRASES
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

def build_all_lookups(loader):
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


# ══════════════════════════════════════════════════════════════
# SEMANTIC VECTOR BUILDER
# ═══════════════════════════════��══════════════════════════════

import os
import time
import torch
from chat_logger import get_logger
from store_loader.config import DEV_CACHE_ENABLED, UPDATE_DEV_CACHE_ENABLED, VECTOR_CACHE_FILE

logger = get_logger("miraq_chat")

def build_semantic_vectors(loader):
    """Translates WooCommerce Tags and Attributes into Semantic Coordinates, with disk caching."""
    logger.info("Building Semantic Vectors for Store Tags & Attributes...")
    start_time = time.time()
    
    # 1. Read from cache ONLY if DEV_CACHE is true
    if DEV_CACHE_ENABLED and os.path.exists(VECTOR_CACHE_FILE):
        try:
            cached_data = torch.load(VECTOR_CACHE_FILE, weights_only=False)
            loader.semantic_tensors = cached_data["tensors"]
            loader.semantic_keys = cached_data["keys"]
            loader.semantic_dictionary = cached_data["dictionary"]
            logger.info(f"⚡ Loaded {len(loader.semantic_keys)} cached semantic vectors in {round(time.time() - start_time, 2)}s")
            return
        except Exception as e:
            logger.warning(f"Failed to load cached vectors, rebuilding from scratch: {e}")
    
    # 2. Build Corpus from scratch (if cache miss or live mode)
    corpus_texts = []
    loader.semantic_keys = []
    loader.semantic_dictionary = {}
    
    # Tags
    for name_lower, tag in loader.tag_by_name_lower.items():
        if tag.get("count", 0) > 0:
            clean_name = name_lower.replace("-", " ")
            corpus_texts.append(clean_name)
            loader.semantic_keys.append(tag["slug"])
            loader.semantic_dictionary[tag["slug"]] = {
                "suggested_name": tag["name"],
                "type": "tag",
                "slug": tag["slug"]
            }

    # Attributes
    for attr in loader.all_attributes_raw:
        taxonomy = attr.get("attribute_name", "") or attr.get("taxonomy", "")
        for term in attr.get("terms", []):
            term_slug = term.get("slug", "")
            term_name = term.get("name", "")
            # Some attribute term values are too generic on their own to be a
            # meaningful semantic anchor (e.g. pa_quick-ship's "Yes"/"No").
            # ATTRIBUTE_VALUE_PHRASES already maps {attr_key: {term_value:
            # natural_phrase}} for exactly this case in the deterministic
            # extractor — reuse it here so the vector index has an actual
            # findable phrase ("quick ship") instead of the bare word "yes",
            # letting fuzzy/typo'd input reach it too.
            phrase_override = ATTRIBUTE_VALUE_PHRASES.get(taxonomy, {}).get(term_name.lower())
            clean_name = phrase_override.lower() if phrase_override else term_name.replace("-", " ").lower()

            corpus_texts.append(clean_name)
            loader.semantic_keys.append(term_slug)
            loader.semantic_dictionary[term_slug] = {
                "suggested_name": term_name,
                "type": "attribute",
                "taxonomy": taxonomy,
                "slug": term_slug
            }
    logger.info(f"[DEBUG quick-ship] corpus entry for 'yes'/pa_quick-ship: {[t for t, k in zip(corpus_texts, loader.semantic_keys) if k == 'yes']}")        
    # Categories
    for name_lower, cat in loader.category_by_name_lower.items():
        if cat.get("count", 0) > 0 and cat.get("slug") != "uncategorized":
            clean_name = name_lower.replace("-", " ")
            corpus_texts.append(clean_name)
            loader.semantic_keys.append(cat["slug"])
            loader.semantic_dictionary[cat["slug"]] = {
                "suggested_name": cat["name"],
                "type": "category",
                "slug": cat["slug"]
            }

    # 3. Generate and Save the Tensors
    if corpus_texts and loader.vector_model:
        loader.semantic_tensors = loader.vector_model.encode(corpus_texts, convert_to_tensor=True)
        
        # Save to disk ONLY if UPDATE_DEV_CACHE is true
        if UPDATE_DEV_CACHE_ENABLED:
            try:
                os.makedirs(os.path.dirname(VECTOR_CACHE_FILE), exist_ok=True)
                torch.save({
                    "tensors": loader.semantic_tensors,
                    "keys": loader.semantic_keys,
                    "dictionary": loader.semantic_dictionary
                }, VECTOR_CACHE_FILE)
                logger.info("💾 Saved newly generated semantic vectors to local cache.")
            except Exception as e:
                logger.error(f"Failed to save semantic vector cache: {e}")

    logger.info(f"Generated {len(corpus_texts)} vectors in {round(time.time() - start_time, 2)}s")
    
