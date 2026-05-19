"""
store_loader/queries.py — All read-only query methods for StoreLoader.

Separated from the loader class so they can be tested independently
and don't clutter the loading/building logic.
"""

import re
import datetime as _dt
import time as _time
from typing import List, Dict, Optional

from config.store_config import TAG_SLUG_QUICK_SHIP, TAG_SLUG_CHIP_CARD
from chat_logger import get_logger
from models.catalog import (
    CatalogAttribute,
    CatalogAttributeTerm,
    CatalogCategory,
    CatalogTag,
)
from store_loader.config import DEV_CACHE_ENABLED

logger = get_logger("miraq_chat")


class StoreQueryMixin:
    """
    Mixin providing all query methods for StoreLoader.
    Expects the host class to have all lookup dictionaries populated.
    """

    # ─── Category queries ───

    def get_category_slug(self, category_id: int) -> Optional[str]:
        entry = self.category_by_id.get(category_id)
        return entry["slug"] if entry else None

    def get_all_slugs_for_category(self, category_id: int) -> List[str]:
        entry = self.category_by_id.get(category_id)
        if not entry:
            return []
        name_lower = entry["name"].lower()
        return self.category_slugs_by_name.get(name_lower, [entry["slug"]])

    def get_category_for_text(self, text: str) -> Optional[Dict]:
        results = self.get_all_categories_for_text(text)
        return results[0] if results else None

    def get_all_categories_for_text(self, text: str) -> List[Dict]:
        text_lower = text.lower()
        best_per_cat: Dict[int, tuple] = {}

        for keyword, cat_id in self.category_keywords.items():
            if keyword in text_lower:
                cat = self.category_by_id.get(cat_id)
                if cat and cat["count"] > 0:
                    kw_len = len(keyword)
                    existing = best_per_cat.get(cat_id)
                    if existing is None or kw_len > existing[0]:
                        best_per_cat[cat_id] = (kw_len, cat["count"], cat)

        if not best_per_cat:
            return []
        candidates = list(best_per_cat.values())
        candidates.sort(key=lambda x: (x[1], -x[0]))
        return [c[2] for c in candidates]

    def get_related_categories(self, cat_id: int, limit: int = 3) -> List[Dict]:
        cat = self.category_by_id.get(cat_id)
        if not cat:
            return []

        results = []
        seen_ids = {cat_id}

        name = cat["name"].lower()
        raw_words = [w for w in re.split(r"[\s\-_/&]+", name) if w.strip()]
        if len(raw_words) > 1:
            for word in raw_words:
                matched_id = self.category_keywords.get(word)
                if matched_id and matched_id not in seen_ids:
                    matched_cat = self.category_by_id.get(matched_id)
                    if matched_cat and matched_cat.get("count", 0) > 0:
                        results.append(matched_cat)
                        seen_ids.add(matched_id)

        parent_id = cat.get("parent", 0)
        siblings = [
            c for c in self.categories
            if c.get("parent", 0) == parent_id
            and c["id"] not in seen_ids
            and c.get("slug") != "uncategorized"
            and c.get("count", 0) > 0
        ]
        siblings.sort(key=lambda x: x.get("count", 0), reverse=True)
        for sibling in siblings:
            if len(results) >= limit:
                break
            results.append(sibling)
            seen_ids.add(sibling["id"])

        return results[:limit]

    # ─── Product queries ───

    def get_product_for_text(self, text: str) -> Optional[Dict]:
        text_lower = text.lower()
        candidates = []
        
        logger.debug(f"get_product_for_text: size={len(self.product_by_name_lower)} sample={list(self.product_by_name_lower.keys())[:5]}")  # ← ADD THIS

        for name_lower, entry in self.product_by_name_lower.items():
            if re.search(rf'\b{re.escape(name_lower)}\b', text_lower):
                candidates.append(entry)
                
        logger.debug(f"get_product_for_text: candidates={candidates}")

        stop_words = self._store_generic_terms.copy()
        stop_words.update({"sample", "samples", "product", "item", "size", "sizes"})
        for attr in self.attribute_by_key.values():
            attr_name = attr.label.lower().strip()
            stop_words.add(attr_name)
            stop_words.update(attr_name.split())

        specific = [c for c in candidates if c["name"].lower().strip() not in stop_words]
        generic = [c for c in candidates if c["name"].lower().strip() in stop_words]

        if specific:
            return max(specific, key=lambda x: len(x["name"]))
        if generic:
            return max(generic, key=lambda x: len(x["name"]))
        return None

    # ─── Attribute queries ───

    def get_attribute_slug(self, attr_id: int) -> Optional[str]:
        entry = self.attribute_by_id.get(attr_id)
        return entry["slug"] if entry else None

    def get_sibling_attribute_terms_neutral(self, attr_key: str, failed_term: str, limit: int = 3) -> List[str]:
        """Return sibling display names for a neutral attribute key, excluding failed_term."""
        attr = self.resolve_attribute(attr_key)
        if not attr:
            return []
        failed_lower = failed_term.lower().strip()
        candidates = [t for t in attr.terms if t.name.lower().strip() != failed_lower]
        candidates.sort(key=lambda x: x.count, reverse=True)
        return [t.name for t in candidates[:limit]]

    # ─── Neutral catalog queries (Phase 4a — preferred for new code) ───

    def resolve_attribute(self, key: str) -> Optional[CatalogAttribute]:
        """Look up an attribute by its neutral key (e.g. 'color')."""
        return self.attribute_by_key.get(key.lower().strip())

    def resolve_attribute_term(self, attr_key: str, term_key_or_name: str) -> Optional[CatalogAttributeTerm]:
        """Look up an attribute term by attr_key + (term key OR display name, case-insensitive)."""
        attr = self.resolve_attribute(attr_key)
        if not attr:
            return None
        needle = term_key_or_name.lower().strip()
        for term in attr.terms:
            if term.key.lower() == needle or term.name.lower() == needle:
                return term
        return None

    def resolve_category(self, key: str) -> Optional[CatalogCategory]:
        """Look up a category by its neutral key (slug)."""
        return self.category_by_key.get(key.lower().strip())

    def resolve_tag(self, key: str) -> Optional[CatalogTag]:
        """Look up a tag by its neutral key (slug)."""
        return self.tag_by_key.get(key.lower().strip())

    # ─── Tag queries ───

    def get_tag_id_by_slug(self, slug: str) -> Optional[int]:
        tag_obj = self.resolve_tag(slug)
        return tag_obj.backend_ref.get("id") if tag_obj else None

    def get_tag_ids_for_keyword(self, keyword: str) -> List[int]:
        needle = keyword.lower().strip()
        exact, partial = [], []
        seen = set()

        for name_lower, entry in self.tag_by_name_lower.items():
            if entry["id"] in seen:
                continue
            if needle == name_lower or needle == entry["slug"].replace("-", " "):
                exact.append(entry["id"])
                seen.add(entry["id"])
            elif needle in name_lower or name_lower in needle:
                partial.append(entry["id"])
                seen.add(entry["id"])

        return exact if exact else partial

    def get_similar_tags(self, slug: str, limit: int = 3) -> List[Dict]:
        needle_words = set(slug.split("-")) - {""}
        candidates = []

        for tag_slug, tag_obj in self.tag_by_key.items():
            if tag_slug == slug or tag_obj.count == 0:
                continue
            tag_words = set(tag_slug.split("-")) - {""}
            score = 0
            if tag_slug.startswith(slug + "-") or tag_slug == slug:
                score = 100 + tag_obj.count
            elif needle_words and needle_words & tag_words:
                overlap = len(needle_words & tag_words) / max(len(needle_words), 1)
                score = int(overlap * 50) + tag_obj.count
            if score > 0:
                tag_dict = {"id": tag_obj.backend_ref.get("id"), "name": tag_obj.name, "slug": tag_obj.key, "count": tag_obj.count}
                candidates.append((score, tag_dict))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in candidates[:limit]]

    def get_quick_ship_tag_id(self) -> Optional[int]:
        return self.get_tag_id_by_slug(TAG_SLUG_QUICK_SHIP)

    def get_chip_card_tag_id(self) -> Optional[int]:
        return self.get_tag_id_by_slug(TAG_SLUG_CHIP_CARD)

    # ─── Variation cache ───

    def get_variation_schema(self, product_id: int) -> Optional[Dict]:
        return self.product_variation_schema.get(product_id)

    def get_cached_variations(self, product_id: int) -> Optional[List[Dict]]:
        return self.variation_detail_cache.get(product_id)

    def cache_variations(self, product_id: int, variations: List[Dict]) -> None:
        self.variation_detail_cache[product_id] = variations

    # ─── Status & diagnostics ───

    def is_ready(self) -> bool:
        return self._last_loaded is not None

    def get_status(self) -> dict:
        last_loaded_iso = (
            _dt.datetime.fromtimestamp(self._last_loaded, tz=_dt.timezone.utc).isoformat()
            if self._last_loaded else None
        )
        next_retry_in = None
        if self._degraded and self._last_loaded:
            elapsed = _time.time() - self._last_loaded
            remaining = max(0, self._retry_interval - elapsed)
            next_retry_in = f"{int(remaining // 60)}m {int(remaining % 60)}s"

        return {
            "ready": self.is_ready(),
            "degraded": self._degraded,
            "degraded_reasons": self._degraded_reasons,
            "last_loaded": last_loaded_iso,
            "next_retry_in": next_retry_in,
            "loaded_from_cache": self._loaded_from_cache,
            "dev_cache_enabled": DEV_CACHE_ENABLED,
            "counts": {
                "categories": len(self.categories),
                "tags": len(self.tags),
                "attributes": len(self.attributes),
                "products": len(self.products),
                "expected_products": self._expected_product_count,
                "category_keywords": len(self.category_keywords),
                "attribute_terms": sum(len(a.terms) for a in self.attribute_by_key.values()),
                "variation_cache_size": len(self.variation_detail_cache),
                "semantic_vectors": len(self.semantic_keys) if self.semantic_keys else 0,
            },
        }

    def print_categories(self):
        if not self.categories:
            logger.info("StoreLoader: No categories loaded")
            return
        lines = ["StoreLoader: Store Categories:"]
        top_level = [c for c in self.categories if c.get("parent", 0) == 0]
        for cat in sorted(top_level, key=lambda x: x.get("name", "")):
            count = cat.get("count", 0)
            slug = cat.get("slug", "")
            if slug == "uncategorized" and count == 0:
                continue
            lines.append(f"  ├── {cat['name']} (id={cat['id']}, slug={slug}, count={count})")
            children = [c for c in self.categories if c.get("parent") == cat["id"]]
            for child in sorted(children, key=lambda x: x.get("name", "")):
                lines.append(f"  │   └── {child['name']} (id={child['id']}, count={child.get('count', 0)})")
        logger.info("\n".join(lines))

    def print_keywords(self):
        if not self.category_keywords:
            logger.info("StoreLoader: No category keywords generated")
            return
        lines = ["StoreLoader: Category Keywords → Category Mapping:"]
        for kw, cat_id in sorted(self.category_keywords.items()):
            cat_name = self.category_by_id.get(cat_id, {}).get("name", "?")
            lines.append(f"  '{kw}' → {cat_name} (id={cat_id})")
        logger.info("\n".join(lines))
