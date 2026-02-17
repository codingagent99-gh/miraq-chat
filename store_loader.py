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

        # Lookup maps
        self.category_by_slug: Dict[str, Dict] = {}
        self.category_by_id: Dict[int, Dict] = {}
        self.category_by_name_lower: Dict[str, Dict] = {}
        self.tag_by_slug: Dict[str, Dict] = {}
        self.tag_by_id: Dict[int, Dict] = {}
        self.product_by_name_lower: Dict[str, Dict] = {}
        self.product_name_tokens: List[tuple] = []  # [(token, product_dict), ...]

        # NLP keyword → category mappings
        self.category_keywords: Dict[str, int] = {}

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

    def _build_lookups(self):
        """Build lookup dicts and NLP keyword maps from loaded data."""

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
            self.category_by_name_lower[name_lower] = entry

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
            stop = {"tile", "tiles", "the", "a", "an", "and", "or", "of", "series"}
            for token in re.split(r'[\s\-_/]+', name.lower()):
                token = token.strip()
                if token and token not in stop and len(token) > 2:
                    self.product_name_tokens.append((token, entry))

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

        # Full name: "Wall/Floor" → "wall/floor"
        self.category_keywords[name] = cat_id

        # Split by spaces, hyphens, slashes, underscores
        stop_words = {
            "the", "a", "an", "and", "or", "of", "for",
            "in", "on", "to", "is", "all", "our", "new",
        }
        words = re.split(r'[\s\-_/&]+', name)
        for word in words:
            word = word.strip().lower()
            if word and word not in stop_words and len(word) > 2:
                if word not in self.category_keywords:
                    self.category_keywords[word] = cat_id

        # Slug as words: "wall-floor" → "wall floor"
        slug_words = slug.replace("-", " ")
        if slug_words != name:
            self.category_keywords[slug_words] = cat_id

        # Common NL variations people might type
        variations = {
            "tiles": "tile", "tile": "tiles",
            "flooring": "floor", "floor": "flooring",
            "walls": "wall", "wall": "walls",
            "countertops": "countertop", "countertop": "countertops",
            "counter top": "countertop", "counter tops": "countertops",
            "backsplash": "backsplashes", "backsplashes": "backsplash",
            "outdoor": "exterior", "exterior": "outdoor",
            "indoor": "interior", "interior": "indoor",
        }

        # Add variations of the full name
        for original, variant in variations.items():
            if original in name:
                alt_name = name.replace(original, variant)
                if alt_name not in self.category_keywords:
                    self.category_keywords[alt_name] = cat_id

        # Also add "X tiles" if not already there
        # e.g., category "Wall" → also match "wall tiles"
        for suffix in ["tiles", "tile"]:
            combo = f"{name} {suffix}"
            if combo not in self.category_keywords:
                self.category_keywords[combo] = cat_id

        # And "[word] tiles" for each word
        for word in words:
            word = word.strip().lower()
            if word and word not in stop_words and len(word) > 2:
                for suffix in ["tiles", "tile"]:
                    combo = f"{word} {suffix}"
                    if combo not in self.category_keywords:
                        self.category_keywords[combo] = cat_id

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

        # Partial match
        for name_lower, entry in self.category_by_name_lower.items():
            if keyword in name_lower or name_lower in keyword:
                if entry["count"] > 0:
                    return entry["id"]
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
        for token, entry in self.product_name_tokens:
            if token in text_lower and len(token) > best_match_len:
                best_match = entry
                best_match_len = len(token)

        return best_match

    # ─────────────────────────────────────────────
    # ATTRIBUTE & TAG LOOKUPS  (replaces store_registry hardcoded maps)
    # ─────────────────────────────────────────────

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

    def get_tag_id_by_slug(self, slug: str) -> Optional[int]:
        """Return tag ID by slug. Uses live data."""
        entry = self.tag_by_slug.get(slug)
        return entry["id"] if entry else None

    def get_tag_ids_for_keyword(self, keyword: str) -> List[int]:
        """
        Find tag IDs matching a keyword against live tag names/slugs.
        e.g. "matte" → matches "Matte finish" tag → [65]
             "gray"  → matches "Gray Tones" tag   → [1152]
             "italy" → matches "Made in Italy" tag → [69]
        """
        needle = keyword.lower().strip()
        results = []
        seen = set()

        for name_lower, entry in self.tag_by_name_lower.items():
            if entry["id"] in seen:
                continue
            if needle in name_lower or name_lower in needle:
                results.append(entry["id"])
                seen.add(entry["id"])

        return results

    def get_quick_ship_tag_id(self) -> Optional[int]:
        """Convenience: return the Quick Ship tag ID."""
        return self.get_tag_id_by_slug("quick-ship")

    def get_chip_card_tag_id(self) -> Optional[int]:
        """Convenience: return the Chip Card tag ID."""
        return self.get_tag_id_by_slug("chip-card")

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