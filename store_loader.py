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

load_dotenv()

WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
REQUEST_TIMEOUT = 30

# ──────────────────────────────────────
# This exact header set returned 200 in Test 3
# ModSecurity blocks python-requests default UA
# ──────────────────────────────────────
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

        # Populated after load()
        self.categories: List[Dict] = []
        self.tags: List[Dict] = []
        self.attributes: List[Dict] = []
        self.attribute_terms: Dict[int, List[Dict]] = {}
        self.products: List[Dict] = []
        self.all_attributes_raw: List[Dict] = []

        # Lookup maps
        self.category_by_slug: Dict[str, Dict] = {}
        self.category_slugs_by_name: Dict[str, List[str]] = {}  # name_lower → all slugs
        self.category_by_id: Dict[int, Dict] = {}
        self.category_by_name_lower: Dict[str, Dict] = {}
        self.tag_by_slug: Dict[str, Dict] = {}
        self.tag_by_id: Dict[int, Dict] = {}
        self.product_by_name_lower: Dict[str, Dict] = {}
        self.product_name_tokens: List[tuple] = []  # [(token, product_dict), ...]

        # NLP keyword → category mappings
        self.category_keywords: Dict[str, int] = {}

        # Store-generic terms derived from category names (e.g. {"tile", "tiles"} for a tile store)
        # Used to generate "wall tile", "floor tiles" etc. combos — no hardcoding needed
        self._store_generic_terms: set = set()

        # Synonym map loaded from config — store-specific, empty by default
        # Format: {"flooring": "floor", "walls": "wall", ...}
        self._category_synonyms: Dict[str, str] = self._load_category_synonyms()

        # Product variation schema — built at startup from products list
        # product_id → {variation_axes, default_attributes, variation_ids, variation_count}
        self.product_variation_schema: Dict[int, Dict] = {}

        # Variation detail cache — populated lazily on first request, shared across sessions
        # product_id → [variation_dict, ...]
        self.variation_detail_cache: Dict[int, List[Dict]] = {}

        # ── Derived lookup maps (built after load) ──
        self.attribute_by_slug: Dict[str, Dict] = {}   # slug → {id, name, slug}
        self.attribute_by_id: Dict[int, Dict] = {}     # id   → {id, name, slug}
        self.tag_by_name_lower: Dict[str, Dict] = {}   # name_lower → tag entry

        # Background refresh state
        self._lock = threading.Lock()
        self._last_loaded: Optional[float] = None      # epoch time of last successful load
        self._refresh_interval: int = 6 * 3600         # 6 hours
        self._refresh_thread: Optional[threading.Thread] = None

    def load_all(self):
        """Fetch all taxonomy data from WooCommerce."""
        print("📡 Loading store data from WooCommerce...")
        print(f"   Base URL: {self.base}")
        print(f"   Auth Key: {self.consumer_key[:12]}...")

        if not self.consumer_key or self.consumer_key.startswith("ck_your"):
            print("\n   ❌ API keys not configured! Update .env file.")
            return

        self.categories = self._fetch_all_pages(f"{self.base}/products/categories")
        print(f"   ✅ Loaded {len(self.categories)} categories")

        self.tags = self._fetch_all_pages(f"{self.base}/products/tags")
        print(f"   ✅ Loaded {len(self.tags)} tags")

        self.attributes = self._fetch_all_pages(f"{self.base}/products/attributes")
        print(f"   ✅ Loaded {len(self.attributes)} attributes")

        for attr in self.attributes:
            attr_id = attr["id"]
            terms = self._fetch_all_pages(
                f"{self.base}/products/attributes/{attr_id}/terms"
            )
            self.attribute_terms[attr_id] = terms
            print(f"   ✅ Loaded {len(terms)} terms for '{attr['name']}' (id={attr_id})")

        self.products = self._fetch_all_pages(
            f"{self.base}/products",
            extra_params={"status": "publish"},
        )
        print(f"   ✅ Loaded {len(self.products)} products")

        # Also fetch from custom all-attributes API for fresh data
        custom_api_base = os.getenv(
            "CUSTOM_API_BASE_URL",
            self.base.replace("/wp-json/wc/v3", "/wp-json/custom-api/v1"),
        )
        try:
            resp = self.session.get(f"{custom_api_base}/all-attributes", timeout=self.timeout)
            resp.raise_for_status()
            self.all_attributes_raw = resp.json()
            print(f"   ✅ Loaded {len(self.all_attributes_raw)} attributes from custom API")
        except Exception as e:
            print(f"   ⚠️  Custom all-attributes API failed: {e}")
            self.all_attributes_raw = []

        self._build_lookups()
        self._last_loaded = time.time()

        print(f"\n📊 Store Data Summary:")
        print(f"   Categories:   {len(self.categories)}")
        print(f"   Tags:         {len(self.tags)}")
        print(f"   Attributes:   {len(self.attributes)}")
        print(f"   Products:     {len(self.products)}")
        print(f"   Cat Keywords: {len(self.category_keywords)}")
        print(f"   Ready! ✅\n")

    def start_background_refresh(self):
        """Start a background thread that reloads store data every 6 hours."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        def _refresh_loop():
            while True:
                time.sleep(self._refresh_interval)
                print("🔄 Background refresh: reloading store data...")
                try:
                    self.load_all()
                    print("🔄 Background refresh: complete.")
                except Exception as e:
                    print(f"🔄 Background refresh failed: {e}")
        self._refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
        self._refresh_thread.start()
        print(f"⏰ Background refresh scheduled every {self._refresh_interval // 3600}h")

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
                print(f"   ⚠️  HTTP {status} at {url} page {page}: {body}")
                break
            except Exception as e:
                print(f"   ⚠️  Error fetching {url}: {e}")
                break

        return all_items

    # ─────────────────────────────────────────────
    # LOOKUP BUILDERS
    # ─────────────────────────────────────────────

    def _load_category_synonyms(self) -> Dict[str, str]:
        """Load store-specific synonym map from env/config. Empty by default.

        Set CATEGORY_SYNONYMS in your .env as a JSON string, e.g.:
            CATEGORY_SYNONYMS={"flooring":"floor","walls":"wall","countertops":"countertop"}

        This keeps tile-store-specific knowledge out of the source code.
        """
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
        # A word is "generic" if it appears in multiple category names AND
        # is outnumbered — i.e. it alone doesn't identify a unique category.
        # We use count >= 2 AND the word appears in more categories than any single
        # category with that name has products (prevents real categories from being stripped).
        generic = set()
        for word, count in word_counts.items():
            if count >= 2:
                solo_count = sum(
                    1 for c in valid_cats
                    if c.get("name", "").lower().strip() == word
                )
                compound_count = count - solo_count
                # Generic only if it appears MORE in compounds than as a solo category.
                # "tile": solo=1 (Tile), compound=2 (Tile Floor, Tile Wall) → generic ✅
                # "wall": solo=3 (Wall×3), compound=2 (Tile Wall, Wall/Floor) → NOT generic ✅
                # "floor": solo=3 (Floor×3), compound=2 (Tile Floor, Wall/Floor) → NOT generic ✅
                if compound_count > solo_count:
                    generic.add(word)
        return generic

    def _build_lookups(self):
        """Build lookup dicts and NLP keyword maps from loaded data."""

        # Derive store-generic terms from category names before building keywords
        self._store_generic_terms = self._build_store_generic_terms()

        # ── Attribute lookups ──
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

        # ── Tag name lookups ──
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
            # Also index by slug words e.g. "matte-finish" → "matte finish"
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
            # Prefer higher-count category when names collide (e.g. two "Floor" categories)
            existing = self.category_by_name_lower.get(name_lower)
            if not existing or count > existing.get("count", 0):
                self.category_by_name_lower[name_lower] = entry
            # Collect ALL slugs for duplicate-named categories
            if name_lower not in self.category_slugs_by_name:
                self.category_slugs_by_name[name_lower] = []
            if slug not in self.category_slugs_by_name[name_lower]:
                self.category_slugs_by_name[name_lower].append(slug)

            # Generate keywords for non-empty, non-uncategorized categories
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
            # Also index each meaningful word/token from the product name
            # e.g. "Lager Matte 24x48" → tokens: ["lager", "matte", "24x48"]
            stop = {"the", "a", "an", "and", "or", "of", "series", "product", "products"} | self._store_generic_terms
            for token in re.split(r'[\s\-_/]+', name.lower()):
                token = token.strip()
                if token and token not in stop and len(token) > 2:
                    self.product_name_tokens.append((token, entry))

        # Build variation schema from product attributes
        # This avoids needing an API call just to know what axes a product has
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
        """
        cat_id = cat_entry["id"]
        name = cat_entry["name"].lower().strip()
        slug = cat_entry["slug"]

        cat_count = cat_entry.get("count", 0)

        def _register(kw: str, cid: int):
            """Register keyword, preferring the category with higher product count."""
            if kw not in self.category_keywords:
                self.category_keywords[kw] = cid
            else:
                existing_id = self.category_keywords[kw]
                existing_count = (self.category_by_id.get(existing_id) or {}).get("count", 0)
                if cat_count > existing_count:
                    self.category_keywords[kw] = cid

        # Full name: "Wall/Floor" → "wall/floor"
        _register(name, cat_id)

        # Split by spaces, hyphens, slashes, underscores
        # Universal grammar stop words + store-generic terms (e.g. "tile" for a tile store)
        # _store_generic_terms is derived from category name frequency — no hardcoding needed
        stop_words = {
            "the", "a", "an", "and", "or", "of", "for",
            "in", "on", "to", "is", "all", "our", "new",
        } | self._store_generic_terms
        words = re.split(r'[\s\-_/&]+', name)
        for word in words:
            word = word.strip().lower()
            if word and word not in stop_words and len(word) > 2:
                _register(word, cat_id)

        # Slug as words: "wall-floor" → "wall floor"
        slug_words = slug.replace("-", " ")
        if slug_words != name:
            _register(slug_words, cat_id)

        # Add synonym variations from config (store-specific, empty by default)
        # e.g. {"flooring": "floor", "walls": "wall"} for a tile store
        for original, variant in self._category_synonyms.items():
            if original in name:
                alt_name = name.replace(original, variant)
                _register(alt_name, cat_id)

        # Add "[category name] + [generic term]" combos
        # e.g. store generic terms = {"tile", "tiles"} → "wall tile", "wall tiles"
        # Derived from the store's own category names, not hardcoded
        for suffix in self._store_generic_terms:
            _register(f"{name} {suffix}", cat_id)
            for word in words:
                word = word.strip().lower()
                if word and word not in stop_words and len(word) > 2:
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

        # Partial substring match
        for name_lower, entry in self.category_by_name_lower.items():
            if keyword in name_lower or name_lower in keyword:
                if entry["count"] > 0:
                    return entry["id"]

        # Word-set overlap match — catches word-order flips and partial matches like
        # "floor tiles" vs "Tile Floor", "floor" vs "Tile Floor", "wall" vs "Tile Wall"
        keyword_words = set(keyword.split())

        best_id = None
        best_count = 0
        for name_lower, entry in self.category_by_name_lower.items():
            name_words = set(name_lower.split())
            overlap = keyword_words & name_words
            # Any meaningful word overlap qualifies; prefer higher product count
            if overlap and entry["count"] > best_count:
                best_id = entry["id"]
                best_count = entry["count"]
        if best_id:
            return best_id

        return None

    def get_category_for_text(self, text: str) -> Optional[Dict]:
        """
        Scan user text for any category keyword match.
        Returns best (longest) matching category or None.

        Example with your real categories:
          "Show me wall tiles"     → matches "wall tiles" → Wall category
          "countertop options"     → matches "countertop" → Countertop category
          "what's new"             → matches "new releases" → New Releases category
          "floor and wall tiles"   → matches "wall/floor" → Wall/Floor category
        """
        text_lower = text.lower()
        best_match = None
        best_match_len = 0

        for keyword, cat_id in sorted(
            self.category_keywords.items(),
            key=lambda x: len(x[0]),
            reverse=True,
        ):
            if keyword in text_lower and len(keyword) > best_match_len:
                cat = self.category_by_id.get(cat_id)
                if cat and cat["count"] > 0:
                    best_match = cat
                    best_match_len = len(keyword)

        return best_match

    def get_product_for_text(self, text: str) -> Optional[Dict]:
        """
        Scan user text for any known product name or token.
        Returns the best (longest name) matching product or None.

        Example:
          "show me lager"           → matches "lager" token → Lager product
          "I want affogato mosaic"  → matches "affogato" token → Affogato product
        """
        text_lower = text.lower()
        best_match = None
        best_match_len = 0

        # First try full product name match (most accurate)
        for name_lower, entry in self.product_by_name_lower.items():
            if name_lower in text_lower and len(name_lower) > best_match_len:
                best_match = entry
                best_match_len = len(name_lower)

        if best_match:
            return best_match

        # Fall back to token matching (catches "lager" when product is "Lager Matte 24x48")
        # Use word-boundary matching to prevent e.g. "mosaic" matching "mosaics"
        for token, entry in self.product_name_tokens:
            if (re.search(rf'\b{re.escape(token)}\b', text_lower)
                    and len(token) > best_match_len):
                best_match = entry
                best_match_len = len(token)

        return best_match

    # ─────────────────────────────────────────────
    # ATTRIBUTE & TAG LOOKUPS  (replaces store_registry hardcoded maps)
    # ─────────────────────────────────────────────

    def get_category_slug(self, category_id: int) -> Optional[str]:
        """Return category slug for an ID."""
        entry = self.category_by_id.get(category_id)
        return entry["slug"] if entry else None

    def get_all_slugs_for_category(self, category_id: int) -> List[str]:
        """Return ALL slugs for categories sharing the same name as category_id.
        Useful when WooCommerce has duplicate category entries with different slugs.
        e.g. 'Tile Floor' → ['tile-floor', 'tile-floor-mosaics-4', ...]"""""
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
        # Remove quotes and extra spaces
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
            # Also match numeric parts: "24x48" matches "24"x48""
            elif re.sub(r'[^\dx]', '', needle) and re.sub(r'[^\dx]', '', needle) in re.sub(r'[^\dx]', '', term_clean):
                partial.append(term["id"])

        return exact if exact else partial

    def get_all_attribute_terms(self, attr_slug: str) -> List[Dict]:
        """Return all terms for an attribute slug."""
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        return self.attribute_terms.get(attr["id"], [])

    def get_variation_schema(self, product_id: int) -> Optional[Dict]:
        """Return cached variation schema for a product (built at startup).
        Contains variation_axes, default_attributes, variation_ids, variation_count.
        Returns None if product is not variable or not found."""
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

    def get_quick_ship_tag_id(self) -> Optional[int]:
        """Convenience: return the Quick Ship tag ID."""
        return self.get_tag_id_by_slug(TAG_SLUG_QUICK_SHIP)

    def get_chip_card_tag_id(self) -> Optional[int]:
        """Convenience: return the Chip Card tag ID."""
        return self.get_tag_id_by_slug(TAG_SLUG_CHIP_CARD)

    def is_ready(self) -> bool:
        """True if store data has been loaded at least once."""
        return self._last_loaded is not None

    def print_categories(self):
        """Print categories in a tree structure."""
        if not self.categories:
            print("\n📂 No categories loaded")
            return

        print("\n📂 Store Categories:")
        top_level = [c for c in self.categories if c.get("parent", 0) == 0]
        for cat in sorted(top_level, key=lambda x: x.get("name", "")):
            count = cat.get("count", 0)
            slug = cat.get("slug", "")
            if slug == "uncategorized" and count == 0:
                continue
            print(f"   ├── {cat['name']} (id={cat['id']}, slug={slug}, count={count})")
            children = [c for c in self.categories if c.get("parent") == cat["id"]]
            for child in sorted(children, key=lambda x: x.get("name", "")):
                child_count = child.get("count", 0)
                print(f"   │   └── {child['name']} (id={child['id']}, count={child_count})")

    def print_keywords(self):
        """Print all auto-generated category keywords."""
        if not self.category_keywords:
            print("\n🔑 No category keywords generated")
            return

        print("\n🔑 Category Keywords → Category Mapping:")
        for kw, cat_id in sorted(self.category_keywords.items()):
            cat_name = self.category_by_id.get(cat_id, {}).get("name", "?")
            print(f"   '{kw}' → {cat_name} (id={cat_id})")