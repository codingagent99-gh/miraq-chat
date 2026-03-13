"""
Store Loader — Fetches categories, tags, and attributes from WooCommerce.

Auth method: Browser User-Agent + Query-string auth
(Test 3 confirmed this bypasses ModSecurity 406 on wgc.net.in)

Dev mode: Set DEV_CACHE=true in .env to cache all store data to a local
JSON file. On subsequent restarts, data loads from disk in ~50ms instead
of fetching from WooCommerce (~15-30s). Run with DEV_CACHE_BUST=true
(or delete .dev_cache/store_data.json) to force a fresh fetch.
"""

import os
import re
import json
import time
import threading
import requests
from collections import OrderedDict
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

# Dev cache settings
DEV_CACHE_ENABLED = os.getenv("DEV_CACHE", "false").lower() == "true"
DEV_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dev_cache")
DEV_CACHE_FILE = os.path.join(DEV_CACHE_DIR, "store_data.json")
DEV_CACHE_BUST = os.getenv("DEV_CACHE_BUST", "false").lower() == "true"

# Path configuration based on your folder structure
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FILE_MAP = {
    "attributes": "all-attributes-and-terms.json",
    "tags":       "list-of-all-tags.json",
    "categories": "product-category.json",
    "products":   "product-list.json"
}
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


# ═══════════════════════════════════════════
# BOUNDED LRU CACHE FOR VARIATION DETAILS
# ═══════════════════════════════════════════

class BoundedVariationCache:
    """
    LRU cache with max size and TTL for variation data.

    - max_size: max number of product_ids to cache (default 200)
    - ttl: seconds before an entry expires (default 1 hour)

    When full, the least-recently-accessed entry is evicted.
    When accessed, expired entries return None (cache miss → re-fetch from API).
    """

    def __init__(self, max_size: int = 200, ttl: int = 3600):
        self._cache: OrderedDict = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl
        self.all_attributes_raw = []
        self.categories = []
        self.tags = []
        self.products = []
        self.attribute_terms = {}
        self._lock = threading.Lock()
        self._last_loaded = None

    def get(self, product_id: int):
        """Get cached variations. Returns None on miss or expiry."""
        entry = self._cache.get(product_id)
        if entry is None:
            return None

        # Check TTL
        if time.time() - entry["cached_at"] > self.ttl:
            del self._cache[product_id]
            return None

        # Cache hit — move to end (most recently used)
        self._cache.move_to_end(product_id)
        return entry["variations"]

    def __setitem__(self, product_id: int, variations: list):
        """Cache variations for a product_id."""
        if product_id in self._cache:
            self._cache.move_to_end(product_id)
        self._cache[product_id] = {
            "variations": variations,
            "cached_at": time.time(),
        }
        # Evict oldest if over capacity
        while len(self._cache) > self.max_size:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.debug(f"BoundedVariationCache: Evicted product_id={evicted_key} (capacity={self.max_size})")

    def __len__(self):
        return len(self._cache)

    def clear(self):
        """Clear all cached entries."""
        self._cache.clear()

    def pop(self, product_id: int, default=None):
        """Remove and return variations for a product_id, or default."""
        entry = self._cache.pop(product_id, None)
        if entry is None:
            return default
        return entry["variations"]


class StoreLoader:
    """Fetches and caches all WooCommerce taxonomy data."""

    # ISO 4217 currency code → symbol mapping used by _currency_code_to_symbol().
    _CURRENCY_MAP: Dict[str, str] = {
        "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
        "JPY": "¥", "CNY": "¥", "AUD": "A$", "CAD": "C$",
        "CHF": "CHF", "SEK": "kr", "NOK": "kr", "DKK": "kr",
        "NZD": "NZ$", "SGD": "S$", "HKD": "HK$", "KRW": "₩",
        "TRY": "₺", "BRL": "R$", "ZAR": "R", "MXN": "MX$",
        "MYR": "RM", "THB": "฿", "PHP": "₱", "IDR": "Rp",
        "AED": "د.إ", "SAR": "﷼", "PLN": "zł", "CZK": "Kč",
        "HUF": "Ft", "RUB": "₽", "ILS": "₪", "CLP": "CL$",
        "COP": "COL$", "PEN": "S/.", "ARS": "AR$", "TWD": "NT$",
        "VND": "₫", "PKR": "₨", "BDT": "৳", "LKR": "Rs",
        "NGN": "₦", "KES": "KSh", "EGP": "E£", "UAH": "₴",
        "RON": "lei", "BGN": "лв", "HRK": "kn", "ISK": "kr",
    }

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
        self.variation_detail_cache = BoundedVariationCache(max_size=200, ttl=3600)

        self.attribute_by_slug: Dict[str, Dict] = {}
        self.attribute_by_id: Dict[int, Dict] = {}
        self.tag_by_name_lower: Dict[str, Dict] = {}
        self.currency_symbol: str = "$"

        self._lock = threading.Lock()
        self._last_loaded: Optional[float] = None
        self._refresh_interval: int = 6 * 3600
        self._retry_interval: int = 2 * 60   # 2 min retry when degraded
        self._refresh_thread: Optional[threading.Thread] = None
        self._degraded: bool = False          # True when critical data failed to load
        self._degraded_reasons: list = []     # Human-readable list of what's missing
        self._expected_product_count: Optional[int] = None
        self._loaded_from_cache: bool = False  # True when data came from dev cache

    # ───────────────────��─────────────────────────
    # DEV CACHE — save/load store data to disk
    # ─────────────────────────────────────────────

    def _save_dev_cache(self, data: dict):
        """Save fetched store data to .dev_cache/store_data.json for fast restarts."""
        try:
            os.makedirs(DEV_CACHE_DIR, exist_ok=True)
            data["_cache_meta"] = {
                "saved_at": time.time(),
                "saved_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                "base_url": self.base,
            }
            with open(DEV_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            size_kb = os.path.getsize(DEV_CACHE_FILE) / 1024
            logger.info(f"StoreLoader: 💾 Dev cache saved → {DEV_CACHE_FILE} ({size_kb:.0f} KB)")
        except Exception as e:
            logger.warning(f"StoreLoader: Could not save dev cache | error={e}")

    def _load_dev_cache(self) -> Optional[dict]:
        """Load store data from .dev_cache/store_data.json if it exists.
        Returns the data dict, or None if cache doesn't exist / is invalid.
        """
        if DEV_CACHE_BUST:
            logger.info("StoreLoader: 🔄 DEV_CACHE_BUST=true — forcing fresh fetch")
            return None

        if not os.path.exists(DEV_CACHE_FILE):
            return None

        try:
            with open(DEV_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Validate it has the expected keys
            required = {"categories", "tags", "attributes", "attribute_terms", "products"}
            if not required.issubset(data.keys()):
                logger.warning("StoreLoader: Dev cache file is missing required keys — ignoring")
                return None

            # Check if the cache was for the same base URL
            meta = data.get("_cache_meta", {})
            cached_url = meta.get("base_url", "")
            if cached_url and cached_url != self.base:
                logger.warning(
                    f"StoreLoader: Dev cache is for {cached_url} but current base is {self.base} — ignoring"
                )
                return None

            saved_at = meta.get("saved_at_iso", "unknown")
            size_kb = os.path.getsize(DEV_CACHE_FILE) / 1024
            logger.info(f"StoreLoader: 📦 Dev cache found ({size_kb:.0f} KB, saved {saved_at})")
            return data

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"StoreLoader: Dev cache file is corrupt — ignoring | error={e}")
            return None

    def _dump_lookups_for_debugging(self):
        """Dump the processed lookup dictionaries to a file in dev mode for inspection."""
        if not DEV_CACHE_ENABLED:
            return

        dump_path = os.path.join(".dev_cache", "lookups_debug.json")
        try:
            dump_data = {
                "store_generic_terms": list(self._store_generic_terms) if self._store_generic_terms else [],
                "attribute_by_slug": self.attribute_by_slug,
                "attribute_by_id": self.attribute_by_id,
                "attribute_terms": self.attribute_terms,
                "category_by_id": self.category_by_id,
                "category_by_name_lower": self.category_by_name_lower,
                "category_keywords": self.category_keywords,
                "tag_by_id": self.tag_by_id,
                "tag_by_slug": self.tag_by_slug,
                "tag_by_name_lower": self.tag_by_name_lower,
            }
            
            # Ensure the .dev_cache directory exists
            os.makedirs(".dev_cache", exist_ok=True)
            
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, indent=2)
            logger.info(f"StoreLoader: Dumped lookup dictionaries to {dump_path} for debugging")
        except Exception as e:
            logger.error(f"StoreLoader: Failed to dump lookups: {e}")
            
    def load_all(self):
        """Loads data from local JSON files to bypass the offline WP API."""
        logger.info(f"StoreLoader: 📁 Loading local data from {DATA_DIR}")
        
        try:
            with self._lock:
                # Load the four files
                self.categories = self._read_json(FILE_MAP["categories"]) or []
                self.tags = self._read_json(FILE_MAP["tags"]) or []
                self.products = self._read_json(FILE_MAP["products"]) or []
                self.all_attributes_raw = self._read_json(FILE_MAP["attributes"]) or []

                # Map attributes and terms for dynamic resolution
                self.attribute_terms = {
                    int(attr["attribute_id"]): attr.get("terms", [])
                    for attr in self.all_attributes_raw 
                    if attr.get("attribute_id")
                }

                # Populate the internal lookup dictionaries for classification
                self._build_lookups()
                self._last_loaded = time.time()
                
            logger.info(f"StoreLoader: ✅ Local load complete. Products: {len(self.products)}")
        except Exception as e:
            logger.error(f"StoreLoader: ❌ Failed to load local files: {e}")

    def _read_json(self, filename: str) -> Optional[List]:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            logger.warning(f"StoreLoader: File not found: {filename}")
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_json_file(self, filename: str) -> Optional[List]:
        """Helper to read individual JSON files."""
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            logger.warning(f"StoreLoader: Missing file {filename}")
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _validate_load(self):
        """
        Check whether critical data loaded successfully.
        """
        reasons = []
        if len(self.categories) == 0:
            reasons.append("0 categories (likely 503/maintenance during fetch)")
        if len(self.products) == 0:
            reasons.append("0 products")
        elif self._expected_product_count and self._expected_product_count > 0:
            ratio = len(self.products) / self._expected_product_count
            if ratio < 0.8:
                reasons.append(
                    f"partial products: {len(self.products)}/{self._expected_product_count} "
                    f"({ratio:.0%} loaded)"
                )
        if len(self.category_keywords) == 0:
            reasons.append("0 category keywords generated")

        self._degraded = len(reasons) > 0
        self._degraded_reasons = reasons

    def _fetch_currency_symbol(self) -> Optional[str]:
        """Fetch the store's currency symbol from WooCommerce settings API."""
        try:
            url = f"{self.base}/settings/general/woocommerce_currency"
            params = {
                "consumer_key": self.consumer_key,
                "consumer_secret": self.consumer_secret,
            }
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            currency_code = data.get("value", "")
            if currency_code:
                return self._currency_code_to_symbol(currency_code)
        except Exception as e:
            logger.warning(f"StoreLoader: Could not fetch currency setting | error={e}")
        return None

    @staticmethod
    def _currency_code_to_symbol(code: str) -> str:
        """Map ISO 4217 currency code to its symbol."""
        return StoreLoader._CURRENCY_MAP.get(code.upper(), code)

    def start_background_refresh(self):
        """
        Start a background thread that keeps store data fresh.

        Normal mode:  refreshes every 6 hours.
        Degraded mode: retries every 2 minutes until data loads fully,
                       then switches to the normal 6-hour cadence.

        In dev mode: background refresh is disabled — you're restarting
        the server manually anyway. Use DEV_CACHE_BUST=true or delete
        .dev_cache/ to force fresh data.
        """
        if DEV_CACHE_ENABLED:
            logger.info("StoreLoader: 🛑 Background refresh DISABLED in dev mode (DEV_CACHE=true)")
            return

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

    def _fetch_all_pages(self, url: str, extra_params: Dict = None, max_retries: int = 3) -> List[Dict]:
        """Fetch all pages using browser UA + query-string auth with retry."""
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

            data = None
            resp = None
            for attempt in range(max_retries):
                try:
                    resp = self.session.get(url, params=params, timeout=self.timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (requests.exceptions.HTTPError,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt
                        status_code = getattr(getattr(e, 'response', None), 'status_code', '?')
                        logger.warning(
                            f"StoreLoader: Retry {attempt + 1}/{max_retries} for {url} "
                            f"page {page} in {wait}s | HTTP {status_code} | {e}"
                        )
                        time.sleep(wait)
                    else:
                        status_code = getattr(getattr(e, 'response', None), 'status_code', '?')
                        body = getattr(getattr(e, 'response', None), 'text', 'N/A')
                        body = body[:300] if body else 'N/A'
                        logger.error(
                            f"StoreLoader: All {max_retries} retries failed for {url} "
                            f"page {page} | HTTP {status_code} | body={body}"
                        )
                        return all_items
                except Exception as e:
                    logger.warning(f"StoreLoader: Unexpected error fetching {url} page {page} | error={e}")
                    return all_items

            if data is None or not data:
                break

            all_items.extend(data)

            total_pages = int(resp.headers.get("X-WP-TotalPages", 1)) if resp else 1
            if page >= total_pages:
                break
            page += 1

        return all_items

    def _fetch_all_pages_with_total(self, url: str, extra_params: Dict = None, max_retries: int = 3) -> Tuple[List[Dict], Optional[int]]:
        """Like _fetch_all_pages but also returns the X-WP-Total header value."""
        all_items = []
        page = 1
        per_page = 100
        expected_total: Optional[int] = None

        while True:
            params = {
                "per_page": per_page,
                "page": page,
                "consumer_key": self.consumer_key,
                "consumer_secret": self.consumer_secret,
            }
            if extra_params:
                params.update(extra_params)

            data = None
            resp = None
            for attempt in range(max_retries):
                try:
                    resp = self.session.get(url, params=params, timeout=self.timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (requests.exceptions.HTTPError,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt
                        logger.warning(
                            f"StoreLoader: Retry {attempt + 1}/{max_retries} for {url} "
                            f"page {page} in {wait}s | {e}"
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"StoreLoader: All {max_retries} retries failed for {url} page {page}"
                        )
                        return all_items, expected_total
                except Exception as e:
                    logger.warning(f"StoreLoader: Unexpected error fetching {url} page {page} | error={e}")
                    return all_items, expected_total

            if data is None or not data:
                break

            all_items.extend(data)

            if resp:
                if expected_total is None:
                    try:
                        expected_total = int(resp.headers.get("X-WP-Total", 0))
                    except (ValueError, TypeError):
                        pass
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break
            else:
                break
            page += 1

        return all_items, expected_total

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
        """Derive generic/filler terms from the store's own category names."""
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
        self._store_generic_terms = self._build_store_generic_terms()

        # 1. ALWAYS Reset/Initialize lookups
        self.attribute_by_slug = {}
        self.attribute_by_id = {}
        self.category_by_slug = {}
        self.category_by_id = {}
        self.category_by_name_lower = {}
        self.tag_by_slug = {}
        self.tag_by_id = {}
        self.tag_by_name_lower = {}
        self.product_by_name_lower = {}

        # 2. Process Attributes (Custom or Standard)
        if self.all_attributes_raw:
            for attr in self.all_attributes_raw:
                if not attr.get("visible", True): continue
                taxonomy_slug = attr.get("taxonomy", "")
                attr_id = attr.get("attribute_id")
                entry = {"id": attr_id, "name": attr.get("attribute_label", ""), "slug": taxonomy_slug}
                self.attribute_by_slug[taxonomy_slug] = entry
                self.attribute_by_id[attr_id] = entry
                self.attribute_terms[attr_id] = attr.get("terms", [])
        else:
            # Standard WC logic for attributes if raw is missing
            for attr in self.attributes:
                entry = {"id": attr["id"], "name": attr.get("name", ""), "slug": attr.get("slug", "")}
                self.attribute_by_slug[attr.get("slug", "")] = entry
                self.attribute_by_id[attr["id"]] = entry

        # 3. MOVE THIS OUTSIDE THE ELSE: Process Categories
        for cat in self.categories:
            cat_id = cat["id"]
            name_lower = cat.get("name", "").lower()
            entry = {"id": cat_id, "name": cat["name"], "slug": cat.get("slug", ""), "count": cat.get("count", 0)}
            self.category_by_id[cat_id] = entry
            self.category_by_slug[entry["slug"]] = entry
            self.category_by_name_lower[name_lower] = entry
            if entry["slug"] != "uncategorized" and entry["count"] > 0:
                self._generate_category_keywords(entry)

        # 4. Process Tags
        for tag in self.tags:
            name_lower = tag.get("name", "").lower()
            entry = {"id": tag["id"], "name": tag["name"], "slug": tag["slug"], "count": tag.get("count", 0)}
            self.tag_by_id[tag["id"]] = entry
            self.tag_by_slug[tag["slug"]] = entry
            self.tag_by_name_lower[name_lower] = entry

        # 5. Process Products (Ensure Ansel is indexed)
        for product in self.products:
            name = product.get("name", "").strip()
            if not name: continue
            self.product_by_name_lower[name.lower()] = {"id": product.get("id"), "name": name, "slug": product.get("slug", "")}
        def _generate_category_keywords(self, cat_entry: Dict):
            """Auto-generate NLP keywords from category name/slug."""
            cat_id = cat_entry["id"]
            name = cat_entry["name"].lower().strip()
            slug = cat_entry["slug"]
            cat_count = cat_entry.get("count", 0)

            def _register(kw: str, cid: int):
                if kw not in self.category_keywords:
                    self.category_keywords[kw] = cid
                else:
                    existing_id = self.category_keywords[kw]
                    existing_count = (self.category_by_id.get(existing_id) or {}).get("count", 0)
                    if cat_count < existing_count:
                        self.category_keywords[kw] = cid

            _register(name, cat_id)

            stop_words = {
                "the", "a", "an", "and", "or", "of", "for",
                "in", "on", "to", "is", "all", "our", "new",
            } | self._store_generic_terms
            words = re.split(r'[\s\-_/&]+', name)
            raw_words = [w for w in words if w.strip()]
            is_single_word_category = len(raw_words) <= 1

            if is_single_word_category:
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

            for original, variant in self._category_synonyms.items():
                if original in name:
                    alt_name = name.replace(original, variant)
                    _register(alt_name, cat_id)

            for suffix in self._store_generic_terms:
                _register(f"{name} {suffix}", cat_id)
                if is_single_word_category:
                    for word in raw_words:
                        if len(word) > 2:
                            _register(f"{word} {suffix}", cat_id)

    # ─────────────────��───────────────────────────
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

    def get_product_for_text(self, text: str) -> Optional[Dict]:
        text_lower = text.lower()
        candidates = []

        # 1. Exact matches
        for name_lower, entry in self.product_by_name_lower.items():
            if re.search(rf'\b{re.escape(name_lower)}\b', text_lower):
                candidates.append(entry)

        if not candidates:
            # 2. Token matches
            for token, entry in self.product_name_tokens:
                if re.search(rf'\b{re.escape(token)}\b', text_lower):
                    candidates.append(entry)

        # ── THE ULTIMATE DIAGNOSTIC ──
        if not candidates:
            logger.debug(f"StoreLoader: No matches for '{text_lower}'")
            # If this prints, it will list EVERY product it actually loaded.
            # If "ansel" is missing from this list, it means the product-list.json 
            # in your data folder doesn't actually contain it!
            logger.debug(f"StoreLoader: Currently loaded product keys: {list(self.product_by_name_lower.keys())}")
            return None

        # 3. Stop words
        stop_words = self._store_generic_terms.copy()
        stop_words.update({"sample", "samples", "tile", "tiles", "mosaic", "mosaics", "product", "item", "size", "sizes"})
        
        for attr in self.attribute_by_slug.values():
            attr_name = attr.get("name", "").lower().strip()
            stop_words.add(attr_name)
            stop_words.update(attr_name.split())

        specific_matches = [c for c in candidates if c["name"].lower().strip() not in stop_words]
        generic_matches = [c for c in candidates if c["name"].lower().strip() in stop_words]

        if specific_matches:
            return max(specific_matches, key=lambda x: len(x["name"]))

        if generic_matches:
            return max(generic_matches, key=lambda x: len(x["name"]))

        return None
            
    # ─────────────────────────────────────────────
    # ATTRIBUTE & TAG LOOKUPS
    # ─────────────────────────────────────────────

    def get_category_slug(self, category_id: int) -> Optional[str]:
        entry = self.category_by_id.get(category_id)
        return entry["slug"] if entry else None

    def get_all_slugs_for_category(self, category_id: int) -> List[str]:
        entry = self.category_by_id.get(category_id)
        if not entry:
            return []
        name_lower = entry["name"].lower()
        return self.category_slugs_by_name.get(name_lower, [entry["slug"]])

    def get_attribute_id(self, slug: str) -> Optional[int]:
        entry = self.attribute_by_slug.get(slug)
        return entry["id"] if entry else None

    def get_attribute_slug(self, attr_id: int) -> Optional[str]:
        entry = self.attribute_by_id.get(attr_id)
        return entry["slug"] if entry else None

    def get_attribute_term_ids(self, attr_slug: str, user_value: str) -> List[int]:
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
            else:
                needle_clean = re.sub(r'[^\dxX]', '', needle).lower()
                term_clean_raw = re.sub(r'[^\dxX]', '', term_name).lower()
                
                # STRICT MATCH FOR DIMENSIONS: Prevents "3x3" from matching "13x36"
                if needle_clean and term_clean_raw:
                    if re.search(rf'(?<!\d){re.escape(needle_clean)}(?!\d)', term_clean_raw):
                        partial.append(term["id"])
                # STANDARD MATCH FOR TEXT: e.g., "matte" matches "matte white"
                else:
                    if needle in term_clean or term_clean in needle:
                        partial.append(term["id"])

        return exact if exact else partial

    def get_attribute_term_slug(self, attr_slug: str, user_value: str) -> str:
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
                
            if not partial_slug:
                needle_clean = re.sub(r'[^\dxX]', '', needle).lower()
                term_clean_raw = re.sub(r'[^\dxX]', '', term_name).lower()

                if needle_clean and term_clean_raw:
                    # STRICT MATCH FOR DIMENSIONS
                    if re.search(rf'(?<!\d){re.escape(needle_clean)}(?!\d)', term_clean_raw):
                        partial_slug = term.get("slug", "")
                else:
                    # STANDARD MATCH FOR TEXT
                    if needle in term_clean or term_clean in needle:
                        partial_slug = term.get("slug", "")

        return exact_slug or partial_slug

    def get_all_attribute_terms(self, attr_slug: str) -> List[Dict]:
        attr = self.attribute_by_slug.get(attr_slug)
        if not attr:
            return []
        return self.attribute_terms.get(attr["id"], [])

    def get_variation_schema(self, product_id: int) -> Optional[Dict]:
        return self.product_variation_schema.get(product_id)

    def get_cached_variations(self, product_id: int) -> Optional[List[Dict]]:
        return self.variation_detail_cache.get(product_id)

    def cache_variations(self, product_id: int, variations: List[Dict]) -> None:
        self.variation_detail_cache[product_id] = variations

    def get_tag_id_by_slug(self, slug: str) -> Optional[int]:
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

    def get_sibling_attribute_terms(
        self, attr_slug: str, failed_term: str, limit: int = 3
    ) -> List[str]:
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
        return self.get_tag_id_by_slug(TAG_SLUG_QUICK_SHIP)

    def get_chip_card_tag_id(self) -> Optional[int]:
        return self.get_tag_id_by_slug(TAG_SLUG_CHIP_CARD)

    def is_ready(self) -> bool:
        return self._last_loaded is not None

    def get_status(self) -> dict:
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
            "loaded_from_cache": self._loaded_from_cache,
            "dev_cache_enabled": DEV_CACHE_ENABLED,
            "counts": {
                "categories": len(self.categories),
                "tags": len(self.tags),
                "attributes": len(self.attributes),
                "products": len(self.products),
                "expected_products": self._expected_product_count,
                "category_keywords": len(self.category_keywords),
                "attribute_terms": sum(len(v) for v in self.attribute_terms.values()),
                "variation_cache_size": len(self.variation_detail_cache),
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
                child_count = child.get("count", 0)
                lines.append(f"  │   └── {child['name']} (id={child['id']}, count={child_count})")
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