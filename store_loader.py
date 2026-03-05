"""
Store Loader — Fetches categories, tags, and attributes from WooCommerce.

Auth method: Browser User-Agent + Query-string auth
(Test 3 confirmed this bypasses ModSecurity 406 on wgc.net.in)
"""

import os
import re
import time
import threading
import requests
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv
from config.store_config import TAG_SLUG_QUICK_SHIP, TAG_SLUG_CHIP_CARD
from chat_logger import get_logger

load_dotenv()

logger = get_logger("miraq_chat")

WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
REQUEST_TIMEOUT = 30

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


class StoreLoader:
    """Fetches and caches all WooCommerce taxonomy data."""

    def __init__(self):
        self.base = WOO_BASE_URL
        self.consumer_key = WOO_CONSUMER_KEY
        self.consumer_secret = WOO_CONSUMER_SECRET
        self.timeout = REQUEST_TIMEOUT

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

        self.categories: List[Dict] = []
        self.tags: List[Dict] = []
        self.attributes: List[Dict] = []
        self.attribute_terms: Dict[int, List[Dict]] = {}
        self.products: List[Dict] = []
        self.all_attributes_raw: List[Dict] = []

        self.category_by_slug: Dict[str, Dict] = {}
        self.category_slugs_by_name: Dict[str, List[str]] = {}
        self.category_by_id: Dict[int, Dict] = {}
        self.category_by_name_lower: Dict[str, Dict] = {}
        self.tag_by_slug: Dict[str, Dict] = {}
        self.tag_by_id: Dict[int, Dict] = {}
        self.product_by_name_lower: Dict[str, Dict] = {}
        self.product_name_tokens: List[tuple] = []

        self.category_keywords: Dict[str, int] = {}
        self._store_generic_terms: set = set()
        self._category_synonyms: Dict[str, str] = self._load_category_synonyms()

        self.product_variation_schema: Dict[int, Dict] = {}
        self.variation_detail_cache: Dict[int, List[Dict]] = {}

        self.attribute_by_slug: Dict[str, Dict] = {}
        self.attribute_by_id: Dict[int, Dict] = {}
        self.tag_by_name_lower: Dict[str, Dict] = {}

        self._lock = threading.Lock()
        self._last_loaded: Optional[float] = None
        self._refresh_interval: int = 6 * 3600
        self._retry_interval: int = 2 * 60   # 2 min retry when degraded
        self._refresh_thread: Optional[threading.Thread] = None
        self._degraded: bool = False          # True when critical data failed to load
        self._degraded_reasons: list = []     # Human-readable list of what's missing

    def load_all(self):
        """Fetch all taxonomy data from WooCommerce."""
        logger.info("StoreLoader: Loading store data from WooCommerce...")
        logger.info(f"StoreLoader: Base URL={self.base}")

        if not self.consumer_key or self.consumer_key.startswith("ck_your"):
            logger.error("StoreLoader: API keys not configured! Update .env file.")
            return

        self.categories = self._fetch_all_pages(f"{self.base}/products/categories")
        logger.info(f"StoreLoader: Loaded {len(self.categories)} categories {'✅' if self.categories else '⚠️ EMPTY'}")

        self.tags = self._fetch_all_pages(f"{self.base}/products/tags")
        logger.info(f"StoreLoader: Loaded {len(self.tags)} tags")

        self.attributes = self._fetch_all_pages(f"{self.base}/products/attributes")
        logger.info(f"StoreLoader: Loaded {len(self.attributes)} attributes")

        for attr in self.attributes:
            attr_id = attr["id"]
            terms = self._fetch_all_pages(
                f"{self.base}/products/attributes/{attr_id}/terms"
            )
            self.attribute_terms[attr_id] = terms
            logger.info(f"StoreLoader: Loaded {len(terms)} terms for '{attr['name']}' (id={attr_id})")

        self.products = self._fetch_all_pages(
            f"{self.base}/products",
            extra_params={"status": "publish"},
        )
        logger.info(f"StoreLoader: Loaded {len(self.products)} products {'✅' if self.products else '⚠️ EMPTY'}")

        custom_api_base = os.getenv(
            "CUSTOM_API_BASE_URL",
            self.base.replace("/wp-json/wc/v3", "/wp-json/custom-api/v1"),
        )
        try:
            resp = self.session.get(f"{custom_api_base}/all-attributes", timeout=self.timeout)
            resp.raise_for_status()
            self.all_attributes_raw = resp.json()
            logger.info(f"StoreLoader: Loaded {len(self.all_attributes_raw)} attributes from custom API")
        except Exception as e:
            logger.warning(f"StoreLoader: Custom all-attributes API failed | error={e}")
            self.all_attributes_raw = []

        self._build_lookups()
        self._last_loaded = time.time()
        self._validate_load()

        logger.info(
            f"StoreLoader: Summary | categories={len(self.categories)} | tags={len(self.tags)} | "
            f"attributes={len(self.attributes)} | products={len(self.products)} | "
            f"cat_keywords={len(self.category_keywords)}"
        )
        if self._degraded:
            logger.warning(f"StoreLoader: DEGRADED — {', '.join(self._degraded_reasons)} | retry_in={self._retry_interval // 60}min")
        else:
            logger.info("StoreLoader: Store data loaded successfully ✅")

    def _validate_load(self):
        """
        Check whether critical data loaded successfully.
        Sets self._degraded = True and populates self._degraded_reasons
        if any critical resource is missing.

        Critical resources:
          - categories: without these, no category detection works at all
          - products:   without these, product name matching is broken
          - cat keywords: derived from categories; 0 means category matching is broken

        Tags and attributes are non-critical — missing tags/attributes degrade
        suggestions and filtering but do not break the core query flow.
        """
        reasons = []
        if len(self.categories) == 0:
            reasons.append("0 categories (likely 503/maintenance during fetch)")
        if len(self.products) == 0:
            reasons.append("0 products")
        if len(self.category_keywords) == 0:
            reasons.append("0 category keywords generated")

        self._degraded = len(reasons) > 0
        self._degraded_reasons = reasons

    def start_background_refresh(self):
        """
        Start a background thread that keeps store data fresh.

        Normal mode:  refreshes every 6 hours.
        Degraded mode: retries every 2 minutes until data loads fully,
                       then switches to the normal 6-hour cadence.
        """
        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def _refresh_loop():
            while True:
                if self._degraded:
                    interval = self._retry_interval
                    logger.warning(f"StoreLoader: DEGRADED ({', '.join(self._degraded_reasons)}) — retrying in {interval // 60}min")
                else:
                    interval = self._refresh_interval
                time.sleep(interval)

                label = "🔁 Degraded load retry" if self._degraded else "🔄 Background refresh"
                logger.info(f"StoreLoader: {label} — reloading store data...")
                try:
                    self.load_all()
                    if not self._degraded:
                        logger.info(f"StoreLoader: {label} complete ✅")
                    else:
                        logger.warning(f"StoreLoader: {label} still degraded — {', '.join(self._degraded_reasons)}")
                except Exception as e:
                    logger.error(f"StoreLoader: {label} failed | error={e}", exc_info=True)

        self._refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
        self._refresh_thread.start()
        if self._degraded:
            logger.warning(f"StoreLoader: Starting in DEGRADED mode — auto-retry every {self._retry_interval // 60}min")
        else:
            logger.info(f"StoreLoader: Background refresh scheduled every {self._refresh_interval // 3600}h")

    def _fetch_all_pages(self, url: str, extra_params: Dict = None) -> List[Dict]:
        """Fetch all pages using browser UA + query-string auth."""
        all_items = []
        page = 1
        per_page = 100

        while True:
            params = {
                "per_page": per_page,
                "page": page,
                "consumer_key": self.consumer_key,
                "consumer_secret": self.consumer_secret,
            }
            if extra_params:
                params.update(extra_params)

            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()

                if not data:
                    break

                all_items.extend(data)

                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break
                page += 1

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                body = e.response.text[:300] if e.response is not None else "N/A"
                logger.warning(f"StoreLoader: HTTP {status} at {url} page {page} | body={body}")
                break
            except Exception as e:
                logger.warning(f"StoreLoader: Error fetching {url} | error={e}")
                break

        return all_items

    # ─────────────────────────────────────────────
    # LOOKUP BUILDERS
    # ─────────────────────────────────────────────

    def _load_category_synonyms(self) -> Dict[str, str]:
        import json
        raw = os.getenv("CATEGORY_SYNONYMS", "{}")
        try:
            synonyms = json.loads(raw)
            if isinstance(synonyms, dict):
                return {k.lower(): v.lower() for k, v in synonyms.items()}
        except Exception:
            pass
        return {}

    def _build_store_generic_terms(self) -> set:
        """Derive generic/filler terms from the store's own category names.

        Words appearing in >30% of category names are too broad to be useful
        as standalone keywords — treat them as suffixes for combo generation instead.
        e.g. "tile" appears in Tile, Tile Floor, Tile Wall → auto-detected as generic.
        Works for any store: a furniture store would detect "furniture", etc.
        """
        from collections import Counter
        word_counts = Counter()
        valid_cats = [c for c in self.categories if c.get("slug") != "uncategorized" and c.get("count", 0) > 0]
        total = len(valid_cats) or 1
        for cat in valid_cats:
            for word in re.split(r"[\s\-_/&]+", cat.get("name", "").lower()):
                word = word.strip()
                if word and len(word) > 2:
                    word_counts[word] += 1
        generic = set()
        for word, count in word_counts.items():
            if count >= 2:
                solo_count = sum(
                    1 for c in valid_cats
                    if c.get("name", "").lower().strip() == word
                )
                compound_count = count - solo_count
                if compound_count > solo_count:
                    generic.add(word)
        return generic

    def _build_lookups(self):
        """Build lookup dicts and NLP keyword maps from loaded data."""

        self._store_generic_terms = self._build_store_generic_terms()

        self.attribute_by_slug = {}
        self.attribute_by_id = {}
        for attr in self.attributes:
            entry = {
                "id": attr["id"],
                "name": attr.get("name", ""),
                "slug": attr.get("slug", ""),
            }
            self.attribute_by_slug[attr.get("slug", "")] = entry
            self.attribute_by_id[attr["id"]] = entry

        self.tag_by_name_lower = {}
        for tag in self.tags:
            name_lower = tag.get("name", "").lower()
            slug = tag.get("slug", "")
            entry = {
                "id": tag["id"],
                "name": tag.get("name", ""),
                "slug": slug,
                "count": tag.get("count", 0),
            }
            self.tag_by_name_lower[name_lower] = entry
            slug_words = slug.replace("-", " ")
            if slug_words != name_lower:
                self.tag_by_name_lower.setdefault(slug_words, entry)

        for cat in self.categories:
            cat_id = cat["id"]
            slug = cat.get("slug", "")
            name = cat.get("name", "")
            name_lower = name.lower()
            count = cat.get("count", 0)
            parent = cat.get("parent", 0)

            entry = {
                "id": cat_id,
                "name": name,
                "slug": slug,
                "count": count,
                "parent": parent,
                "description": cat.get("description", ""),
                "image": cat.get("image"),
            }

            self.category_by_slug[slug] = entry
            self.category_by_id[cat_id] = entry
            existing = self.category_by_name_lower.get(name_lower)
            if not existing or count > existing.get("count", 0):
                self.category_by_name_lower[name_lower] = entry
            if name_lower not in self.category_slugs_by_name:
                self.category_slugs_by_name[name_lower] = []
            if slug not in self.category_slugs_by_name[name_lower]:
                self.category_slugs_by_name[name_lower].append(slug)

            if slug != "uncategorized" and count > 0:
                self._generate_category_keywords(entry)

        for tag in self.tags:
            tag_id = tag["id"]
            slug = tag.get("slug", "")
            entry = {
                "id": tag_id,
                "name": tag.get("name", ""),
                "slug": slug,
                "count": tag.get("count", 0),
            }
            self.tag_by_slug[slug] = entry
            self.tag_by_id[tag_id] = entry

        for product in self.products:
            name = product.get("name", "")
            slug = product.get("slug", "")
            if not name:
                continue
            entry = {
                "id": product.get("id"),
                "name": name,
                "slug": slug,
            }
            self.product_by_name_lower[name.lower()] = entry
            stop = {"the", "a", "an", "and", "or", "of", "series", "product", "products"} | self._store_generic_terms
            for token in re.split(r'[\s\-_/]+', name.lower()):
                token = token.strip()
                if token and token not in stop and len(token) > 2:
                    self.product_name_tokens.append((token, entry))

        self.product_variation_schema = {}
        for product in self.products:
            pid = product.get("id")
            if not pid or product.get("type") != "variable":
                continue
            attrs = product.get("attributes", [])
            variation_axes = {}
            for attr in attrs:
                if attr.get("variation"):
                    variation_axes[attr["slug"]] = {
                        "name": attr["name"],
                        "options": attr.get("options", []),
                    }
            self.product_variation_schema[pid] = {
                "variation_axes": variation_axes,
                "default_attributes": {
                    a["name"].lower(): a["option"]
                    for a in product.get("default_attributes", [])
                },
                "variation_ids": product.get("variations", []),
                "variation_count": len(product.get("variations", [])),
            }

    def _generate_category_keywords(self, cat_entry: Dict):
        """
        Auto-generate NLP keywords from category name/slug.

        For your store's real categories like:
          Countertop, New Releases, Wall, Wall/Floor
        This generates keywords:
          "countertop" → id, "wall" → id, "floor" → id,
          "wall/floor" → id, "new releases" → id, etc.

        Also registers singular forms (strip trailing 's') so that e.g.
          "mosaic" → Mosaics, "panel" → Panels, "paver" → Pavers.
        This prevents singular user terms from leaking into _extract_attributes.
        """
        cat_id = cat_entry["id"]
        name = cat_entry["name"].lower().strip()
        slug = cat_entry["slug"]

        cat_count = cat_entry.get("count", 0)

        def _register(kw: str, cid: int):
            """Register keyword, preferring the most specific category (lowest product count).
            Consistent with get_category_for_text / get_all_categories_for_text which also
            rank by lowest count first."""
            if kw not in self.category_keywords:
                self.category_keywords[kw] = cid
            else:
                existing_id = self.category_keywords[kw]
                existing_count = (self.category_by_id.get(existing_id) or {}).get("count", 0)
                if cat_count < existing_count:   # lower count = more specific → wins
                    self.category_keywords[kw] = cid

        # Full name always registered — this covers single-word categories like "Tile"
        # even though "tile" is in _store_generic_terms.
        _register(name, cat_id)

        # Split by spaces, hyphens, slashes, underscores.
        stop_words = {
            "the", "a", "an", "and", "or", "of", "for",
            "in", "on", "to", "is", "all", "our", "new",
        } | self._store_generic_terms
        words = re.split(r'[\s\-_/&]+', name)

        # Only register individual words for truly single-word category names.
        # Compound/slash categories (e.g. "Wall/Floor", "Tile Floor") must NOT
        # register their individual words — those belong to their own standalone
        # single-word categories. "floor" must map to Floor, not Wall/Floor.
        # Single-word is determined by RAW word count before any stop-word stripping,
        # so generic-term categories like "Tile" (1 raw word) still register correctly.
        raw_words = [w for w in words if w.strip()]
        is_single_word_category = len(raw_words) <= 1

        if is_single_word_category:
            for word in raw_words:
                if len(word) > 2:
                    _register(word, cat_id)
                    # Register both plural and singular so "tiles"→Tile, "mosaic"→Mosaics etc.
                    if word.endswith("s") and len(word) > 3:
                        _register(word[:-1], cat_id)
                    else:
                        _register(word + "s", cat_id)

        # Slug as words: "wall-floor" → "wall floor"
        slug_words = slug.replace("-", " ")
        if slug_words != name:
            _register(slug_words, cat_id)

        # Add synonym variations from config (store-specific, empty by default)
        for original, variant in self._category_synonyms.items():
            if original in name:
                alt_name = name.replace(original, variant)
                _register(alt_name, cat_id)

        # Add "[category name] + [generic term]" combos.
        # Only single-word categories get individual word+suffix combos (e.g. "floor tile").
        # Compound categories only get full-name+suffix (e.g. "wall/floor tile").
        for suffix in self._store_generic_terms:
            _register(f"{name} {suffix}", cat_id)
            if is_single_word_category:
                for word in raw_words:
                    if len(word) > 2:
                        _register(f"{word} {suffix}", cat_id)

    # ─────────────────────────────────────────────
    # QUERY METHODS
    # ─────────────────────────────────────────────

    def get_category_id(self, keyword: str) -> Optional[int]:
        """Look up category ID by keyword, name, or slug."""
        keyword = keyword.lower().strip()

        if keyword in self.category_by_name_lower:
            return self.category_by_name_lower[keyword]["id"]
        if keyword in self.category_by_slug:
            return self.category_by_slug[keyword]["id"]
        if keyword in self.category_keywords:
            return self.category_keywords[keyword]

        for name_lower, entry in self.category_by_name_lower.items():
            if keyword in name_lower or name_lower in keyword:
                if entry["count"] > 0:
                    return entry["id"]

        keyword_words = set(keyword.split())
        best_id = None
        best_count = 0
        for name_lower, entry in self.category_by_name_lower.items():
            name_words = set(name_lower.split())
            overlap = keyword_words & name_words
            if overlap and entry["count"] > best_count:
                best_id = entry["id"]
                best_count = entry["count"]
        if best_id:
            return best_id

        return None

    def get_category_for_text(self, text: str) -> Optional[Dict]:
        """
        Scan user text for any category keyword match.
        Returns the most specific matching category (lowest product count), or None.

        For multi-category queries use get_all_categories_for_text() instead.
        """
        results = self.get_all_categories_for_text(text)
        return results[0] if results else None

    def get_all_categories_for_text(self, text: str) -> List[Dict]:
        """
        Scan user text for ALL matching category keywords.
        Returns a list sorted by specificity (lowest count first, longest keyword as
        tiebreaker) so callers can AND-filter across multiple categories.

        e.g. "exterior pavers in gray"
          → [Pavers (count=5), Exterior (count=17)]
          Primary = Pavers (index 0), Extra = [Exterior] (index 1+)

        Deduplicates by category ID so the same category is never returned twice.
        """
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

    def get_product_for_text(self, text: str) -> Optional[Dict]:
        """
        Scan user text for any known product name or token.
        Returns the best (longest name) matching product or None.
        """
        text_lower = text.lower()
        best_match = None
        best_match_len = 0

        for name_lower, entry in self.product_by_name_lower.items():
            if name_lower in text_lower and len(name_lower) > best_match_len:
                best_match = entry
                best_match_len = len(name_lower)

        if best_match:
            return best_match

        for token, entry in self.product_name_tokens:
            if (re.search(rf'\b{re.escape(token)}\b', text_lower)
                    and len(token) > best_match_len):
                best_match = entry
                best_match_len = len(token)

        return best_match

    # ─────────────────────────────────────────────
    # ATTRIBUTE & TAG LOOKUPS
    # ─────────────────────────────────────────────

    def get_category_slug(self, category_id: int) -> Optional[str]:
        """Return category slug for an ID."""
        entry = self.category_by_id.get(category_id)
        return entry["slug"] if entry else None

    def get_all_slugs_for_category(self, category_id: int) -> List[str]:
        """Return ALL slugs for categories sharing the same name as category_id.
        Useful when WooCommerce has duplicate category entries with different slugs.
        e.g. 'Tile Floor' → ['tile-floor', 'tile-floor-mosaics-4', ...]"""
        entry = self.category_by_id.get(category_id)
        if not entry:
            return []
        name_lower = entry["name"].lower()
        return self.category_slugs_by_name.get(name_lower, [entry["slug"]])

    def get_attribute_id(self, slug: str) -> Optional[int]:
        """Return WooCommerce attribute ID for a slug, e.g. 'pa_tile-size' → 5."""
        entry = self.attribute_by_slug.get(slug)
        return entry["id"] if entry else None

    def get_attribute_slug(self, attr_id: int) -> Optional[str]:
        """Return attribute slug for an ID."""
        entry = self.attribute_by_id.get(attr_id)
        return entry["slug"] if entry else None

    def get_attribute_term_ids(self, attr_slug: str, user_value: str) -> List[int]:
        """
        Fuzzy-match user_value against live attribute terms for attr_slug.
        e.g. get_attribute_term_ids("pa_tile-size", "24x48") → [123]
             get_attribute_term_ids("pa_finish", "matte")     → [45]
             get_attribute_term_ids("pa_application", "interior wall") → [67]
        Returns list of matching term IDs (may be multiple partial matches).
        """
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        attr_id = attr["id"]
        terms = self.attribute_terms.get(attr_id, [])
        if not terms:
            return []

        needle = user_value.lower().strip()
        needle = re.sub(r'[\"\'`]', '', needle).strip()

        exact = []
        partial = []
        for term in terms:
            term_name = term.get("name", "").lower()
            term_slug = term.get("slug", "").lower()
            term_clean = re.sub(r'[\"\'`]', '', term_name).strip()

            if term_clean == needle or term_slug == needle:
                exact.append(term["id"])
            elif needle in term_clean or term_clean in needle:
                partial.append(term["id"])
            elif re.sub(r'[^\dx]', '', needle) and re.sub(r'[^\dx]', '', needle) in re.sub(r'[^\dx]', '', term_clean):
                partial.append(term["id"])

        return exact if exact else partial

    def get_attribute_term_slug(self, attr_slug: str, user_value: str) -> str:
        """
        Like get_attribute_term_ids but returns the slug of the first matched term.
        Returns empty string if no match found.
        e.g. get_attribute_term_slug("pa_sample-size", '3"x3"') → "3x3"
             get_attribute_term_slug("pa_finish", "matte")        → "matte"
        """
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return ""
        terms = self.attribute_terms.get(attr["id"], [])
        needle = re.sub(r'[\"\'`]', '', user_value.lower().strip())

        exact_slug = ""
        partial_slug = ""
        for term in terms:
            term_name  = term.get("name", "").lower()
            term_slug  = term.get("slug", "").lower()
            term_clean = re.sub(r'[\"\'`]', '', term_name).strip()

            if term_clean == needle or term_slug == needle:
                exact_slug = term.get("slug", "")
                break
            if not partial_slug and (
                needle in term_clean or term_clean in needle
                or (re.sub(r'[^\dx]', '', needle) and
                    re.sub(r'[^\dx]', '', needle) in re.sub(r'[^\dx]', '', term_clean))
            ):
                partial_slug = term.get("slug", "")

        return exact_slug or partial_slug

    def get_all_attribute_terms(self, attr_slug: str) -> List[Dict]:
        """Return all terms for an attribute slug."""
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        return self.attribute_terms.get(attr["id"], [])

    def get_variation_schema(self, product_id: int) -> Optional[Dict]:
        """Return cached variation schema for a product (built at startup)."""
        return self.product_variation_schema.get(product_id)

    def get_cached_variations(self, product_id: int) -> Optional[List[Dict]]:
        """Return cached variation detail objects if already fetched, else None."""
        return self.variation_detail_cache.get(product_id)

    def cache_variations(self, product_id: int, variations: List[Dict]) -> None:
        """Store fetched variation details in the global cache (shared across sessions)."""
        self.variation_detail_cache[product_id] = variations

    def get_tag_id_by_slug(self, slug: str) -> Optional[int]:
        """Return tag ID by slug. Uses live data."""
        entry = self.tag_by_slug.get(slug)
        return entry["id"] if entry else None

    def get_tag_ids_for_keyword(self, keyword: str) -> List[int]:
        needle = keyword.lower().strip()
        exact = []
        partial = []
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
        """
        Find tags whose slug is similar to the given slug.
        Used for filter suggestions when a tag returns 0 results.

        Matching strategy (priority order):
        1. Prefix match: failed slug is a prefix of the candidate
           e.g. "wilde" -> "wilde-mosaic", "wilde-series"
        2. Word overlap: candidate shares at least one hyphen-word with failed slug
           e.g. "gray-tone" -> "gray-tones", "dark-gray-tones"

        Returns tags sorted by score DESC. Excludes the failed slug and count=0 tags.
        """
        needle_words = set(slug.split("-")) - {""}
        candidates = []

        for tag_slug, tag in self.tag_by_slug.items():
            if tag_slug == slug:
                continue
            if tag.get("count", 0) == 0:
                continue
            tag_words = set(tag_slug.split("-")) - {""}
            score = 0
            if tag_slug.startswith(slug + "-") or tag_slug == slug:
                score = 100 + tag.get("count", 0)
            elif needle_words and needle_words & tag_words:
                overlap = len(needle_words & tag_words) / max(len(needle_words), 1)
                score = int(overlap * 50) + tag.get("count", 0)
            if score > 0:
                candidates.append((score, tag))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in candidates[:limit]]

    def get_related_categories(self, cat_id: int, limit: int = 3) -> List[Dict]:
        """
        Find categories related to the given category.
        Used for filter suggestions when a category + filter combo returns 0 results.

        Strategy:
        1. Compound/slash categories (e.g. Wall/Floor) -> return component single-word
           categories (Floor, Wall) which are more likely to have results with filters.
        2. Siblings (same parent) sorted by product count DESC.

        Excludes original category and count=0 categories.
        """
        cat = self.category_by_id.get(cat_id)
        if not cat:
            return []

        results = []
        seen_ids = {cat_id}

        # Strategy 1: compound category -> component single-word categories
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

        # Strategy 2: siblings (same parent)
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

    def get_sibling_attribute_terms(
        self, attr_slug: str, failed_term: str, limit: int = 3
    ) -> List[str]:
        """
        Return other term names for the same attribute, excluding the failed term.
        Used for filter suggestions when an attribute value returns 0 results.

        e.g. attr_slug="pa_finish", failed_term="Matte" -> ["Polished", "Brushed", "Honed"]
        Returns term names sorted by count DESC, count=0 terms excluded.
        """
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        terms = self.attribute_terms.get(attr["id"], [])
        failed_lower = failed_term.lower().strip()
        candidates = [
            t for t in terms
            if t.get("name", "").lower().strip() != failed_lower
            and t.get("count", 0) > 0
        ]
        candidates.sort(key=lambda x: x.get("count", 0), reverse=True)
        return [t["name"] for t in candidates[:limit]]

    def get_quick_ship_tag_id(self) -> Optional[int]:
        """Convenience: return the Quick Ship tag ID."""
        return self.get_tag_id_by_slug(TAG_SLUG_QUICK_SHIP)

    def get_chip_card_tag_id(self) -> Optional[int]:
        """Convenience: return the Chip Card tag ID."""
        return self.get_tag_id_by_slug(TAG_SLUG_CHIP_CARD)

    def is_ready(self) -> bool:
        """True if store data has been loaded at least once (even if degraded)."""
        return self._last_loaded is not None

    def get_status(self) -> dict:
        """
        Return a structured status dict for the /status health endpoint.
        Exposes load state, degraded flag, and per-resource counts.
        """
        import datetime as _dt
        last_loaded_iso = (
            _dt.datetime.fromtimestamp(self._last_loaded, tz=_dt.timezone.utc).isoformat()
            if self._last_loaded else None
        )
        next_retry_in = None
        if self._degraded and self._last_loaded:
            import time as _time
            elapsed = _time.time() - self._last_loaded
            remaining = max(0, self._retry_interval - elapsed)
            next_retry_in = f"{int(remaining // 60)}m {int(remaining % 60)}s"

        return {
            "ready": self.is_ready(),
            "degraded": self._degraded,
            "degraded_reasons": self._degraded_reasons,
            "last_loaded": last_loaded_iso,
            "next_retry_in": next_retry_in,
            "counts": {
                "categories": len(self.categories),
                "tags": len(self.tags),
                "attributes": len(self.attributes),
                "products": len(self.products),
                "category_keywords": len(self.category_keywords),
                "attribute_terms": sum(len(v) for v in self.attribute_terms.values()),
            },
        }

    def print_categories(self):
        """Print categories in a tree structure."""
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
                child_count = child.get("count", 0)
                lines.append(f"  │   └── {child['name']} (id={child['id']}, count={child_count})")
        logger.info("\n".join(lines))

    def print_keywords(self):
        """Print all auto-generated category keywords."""
        if not self.category_keywords:
            logger.info("StoreLoader: No category keywords generated")
            return

        lines = ["StoreLoader: Category Keywords → Category Mapping:"]
        for kw, cat_id in sorted(self.category_keywords.items()):
            cat_name = self.category_by_id.get(cat_id, {}).get("name", "?")
            lines.append(f"  '{kw}' → {cat_name} (id={cat_id})")
        logger.info("\n".join(lines))