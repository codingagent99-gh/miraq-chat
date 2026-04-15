"""
Store Loader — Fetches categories, tags, and attributes from WooCommerce.

Dev mode: Set DEV_CACHE=true in .env to load all store data from local
JSON files in the `data/` folder. When false, fetches live from WooCommerce
and the new Custom API for attributes.
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
import torch
from sentence_transformers import SentenceTransformer, util

load_dotenv()

logger = get_logger("miraq_chat")

WOO_BASE_URL = os.getenv("WOO_BASE_URL", "https://wgc.net.in/hn/wp-json/wc/v3")
CUSTOM_API_BASE_URL = os.getenv("CUSTOM_API_BASE_URL", WOO_BASE_URL.replace("/wc/v3", "/custom-api/v1"))
WOO_CONSUMER_KEY = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
REQUEST_TIMEOUT = 30

# Dev cache settings
DEV_CACHE_ENABLED = os.getenv("DEV_CACHE", "false").lower() == "true"
DEV_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dev_cache")
UPDATE_DEV_CACHE_ENABLED = os.getenv("UPDATE_DEV_CACHE", "false").lower() == "true"
# Path configuration based on your folder structure
DATA_FOLDER = os.getenv("DATA_FOLDER", "data")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_FOLDER)
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
    "Accept-Encoding": "gzip, deflate",
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
        self.custom_api_base = CUSTOM_API_BASE_URL
        self.consumer_key = WOO_CONSUMER_KEY
        self.consumer_secret = WOO_CONSUMER_SECRET
        self.timeout = REQUEST_TIMEOUT

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        
        # ─── 1. INITIALIZE SEMANTIC VECTOR MODEL ───
        logger.info("Loading Semantic Vector Model (all-MiniLM-L6-v2)...")
        try:
            self.vector_model = SentenceTransformer('all-MiniLM-L6-v2')
        except Exception as e:
            logger.error(f"Failed to load vector model. Fallback will not be available: {e}")
            self.vector_model = None
            
        self.semantic_dictionary = {}
        self.semantic_tensors = None
        self.semantic_keys = []

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
        self.longest_match_catalog: List[tuple] = []

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
        self._retry_interval: int = 2 * 60   
        self._refresh_thread: Optional[threading.Thread] = None
        self._degraded: bool = False          
        self._degraded_reasons: list = []     
        self._expected_product_count: Optional[int] = None
        self._loaded_from_cache: bool = False  
        self.conflicts: List[Dict] = []

    # ─────────────────────────────────────────────
    # LOADING & FETCHING LOGIC
    # ─────────────────────────────────────────────
    def _save_to_local_files(self):
        """Saves live API data back to local JSON cache files (when UPDATE_DEV_CACHE=true)."""
        os.makedirs(DATA_DIR, exist_ok=True)
        files_to_save = {
            FILE_MAP["categories"]: self.categories,
            FILE_MAP["tags"]:       self.tags,
            FILE_MAP["attributes"]: self.all_attributes_raw,
            FILE_MAP["products"]:   self.products,
        }
        for filename, data in files_to_save.items():
            path = os.path.join(DATA_DIR, filename)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                logger.info(f"StoreLoader: 💾 Saved {len(data)} items → {filename}")
            except Exception as e:
                logger.error(f"StoreLoader: Failed to save {filename}: {e}")

    def _run_scanner_async(self):
        """Runs the heavy NLP scanner in the background so it doesn't freeze the server."""
        try:
            from conflict_scanner import run_conflict_simulation
            self.conflicts = run_conflict_simulation(self)
        except Exception as e:
            logger.error(f"StoreLoader: Conflict scanner failed: {e}", exc_info=True)

    def load_all(self):
        """Loads store data (from local files in dev mode, or live API in prod)."""
        try:
            with self._lock:
                if DEV_CACHE_ENABLED:
                    self._load_from_local_files()
                    self._loaded_from_cache = True
                else:
                    self._load_from_live_api()
                    self._loaded_from_cache = False
                    
                    if UPDATE_DEV_CACHE_ENABLED:
                        self._save_to_local_files()
                        logger.info("StoreLoader: ✅ Dev cache files updated from live API")

                self._build_lookups()
                self._validate_load()
                self._last_loaded = time.time()
                
                # ─── NEW: Build Semantic Vectors Instantly ───
                if self.vector_model:
                    self.build_semantic_vectors()
                
                if DEV_CACHE_ENABLED:
                    self._dump_lookups_for_debugging()
                    
                self._log_load_summary()

            # Run the scanner OUTSIDE the lock in a detached thread!
            import threading
            threading.Thread(target=self._run_scanner_async, daemon=True).start()

        except Exception as e:
            self._degraded = True
            self._degraded_reasons = [str(e)]
            logger.error(f"StoreLoader: ❌ Failed to load store data: {e}", exc_info=True)

    def build_semantic_vectors(self):
        """Translates WooCommerce Tags and Attributes into Semantic Coordinates."""
        logger.info("Building Semantic Vectors for Store Tags & Attributes...")
        start_time = time.time()
        
        corpus_texts = []
        self.semantic_keys = []
        self.semantic_dictionary = {}
        
        # 1. Add Tags
        for name_lower, tag in self.tag_by_name_lower.items():
            if tag.get("count", 0) > 0:
                clean_name = name_lower.replace("-", " ")
                corpus_texts.append(clean_name)
                self.semantic_keys.append(tag["slug"])
                self.semantic_dictionary[tag["slug"]] = {
                    "suggested_name": tag["name"],
                    "type": "tag",
                    "slug": tag["slug"]
                }

        # 2. Add Attributes
        for attr in self.all_attributes_raw:
            taxonomy = attr.get("attribute_name", "") or attr.get("taxonomy", "")
            for term in attr.get("terms", []):
                clean_name = term.get("name", "").replace("-", " ").lower()
                corpus_texts.append(clean_name)
                self.semantic_keys.append(term["slug"])
                self.semantic_dictionary[term["slug"]] = {
                    "suggested_name": term.get("name"),
                    "type": "attribute",
                    "taxonomy": taxonomy,
                    "slug": term["slug"]
                }
                
        # 3. Add Categories
        for name_lower, cat in self.category_by_name_lower.items():
            if cat.get("count", 0) > 0 and cat.get("slug") != "uncategorized":
                clean_name = name_lower.replace("-", " ")
                corpus_texts.append(clean_name)
                self.semantic_keys.append(cat["slug"])
                self.semantic_dictionary[cat["slug"]] = {
                    "suggested_name": cat["name"],
                    "type": "category",
                    "slug": cat["slug"]
                }

        # 4. Generate the Math Coordinates (Tensors)
        if corpus_texts and self.vector_model:
            self.semantic_tensors = self.vector_model.encode(corpus_texts, convert_to_tensor=True)
            
        logger.info(f"Generated {len(corpus_texts)} vectors in {round(time.time() - start_time, 2)}s")

    def sync_from_webhook(self):
        """The background function triggered by WordPress Action Webhooks to refresh the brain."""
        logger.info("Webhook triggered! Refreshing WooCommerce data in background...")
        try:
            # load_all() naturally calls build_semantic_vectors() under the lock!
            self.load_all()
        except Exception as e:
            logger.error(f"Webhook sync failed: {e}", exc_info=True)

    def _log_load_summary(self):
        """Prints a clean summary of what was loaded into memory."""
        mode = "Local Dev Cache" if DEV_CACHE_ENABLED else "Live WooCommerce API"
        status = "⚠️ DEGRADED" if self._degraded else "✅ HEALTHY"
        
        term_count = sum(len(terms) for terms in self.attribute_terms.values())
        vector_count = len(self.semantic_keys) if self.semantic_keys else 0
        
        summary = [
            f"StoreLoader: Initialization Complete [{status}]",
            f"  ├─ Mode:       {mode}",
            f"  ├─ Currency:   {self.currency_symbol}",
            f"  ├─ Products:   {len(self.products)}",
            f"  ├─ Categories: {len(self.categories)}",
            f"  ├─ Tags:       {len(self.tags)}",
            f"  ├─ Attributes: {len(self.attribute_by_slug)} (with {term_count} terms)",
            f"  ├─ Keywords:   {len(self.category_keywords)} (generated for search index)",
            f"  └─ Vectors:    {vector_count} (for semantic fallback)"
        ]
        
        if self._degraded:
            summary.append("  ❌ Degraded Reasons:")
            for reason in self._degraded_reasons:
                summary.append(f"     - {reason}")
                
        logger.info("\n" + "\n".join(summary) + "\n")

    def _load_from_local_files(self):
        """Loads taxonomies and products from the local data/ JSON files."""
        logger.info(f"StoreLoader: 📁 Loading local data from {DATA_DIR}")
        
        self.currency_symbol = "₹" # Force local testing to INR
        
        self.categories = self._read_json(FILE_MAP["categories"]) or []
        self.tags = self._read_json(FILE_MAP["tags"]) or []
        self.products = self._read_json(FILE_MAP["products"]) or []
        self.all_attributes_raw = self._read_json(FILE_MAP["attributes"]) or []

        self.attribute_terms = {
            int(attr["attribute_id"]): attr.get("terms", [])
            for attr in self.all_attributes_raw 
            if attr.get("attribute_id")
        }

    def _load_from_live_api(self):
        """Fetches taxonomies and products from the live WooCommerce API."""
        logger.info("StoreLoader: 🌐 Fetching data from live WooCommerce API...")
        
        self.currency_symbol = self._fetch_currency_symbol()
        
        # 1. Custom API for Attributes & Terms
        custom_attr_url = f"{self.custom_api_base}/all-attributes"
        logger.info(f"StoreLoader: Fetching attributes from {custom_attr_url}")
        
        try:
            resp = self.session.get(custom_attr_url, timeout=self.timeout)
            resp.raise_for_status()
            self.all_attributes_raw = resp.json()
            
            self.attribute_terms = {
                int(attr["attribute_id"]): attr.get("terms", [])
                for attr in self.all_attributes_raw 
                if attr.get("attribute_id")
            }
        except Exception as e:
            logger.error(f"StoreLoader: Failed to fetch attributes: {e}")
            self.all_attributes_raw = []

        # 2. Categories
        logger.info("StoreLoader: Fetching categories...")
        self.categories = self._fetch_all_pages(f"{self.base}/products/categories", {"hide_empty": True})
        
        # 3. Tags
        logger.info("StoreLoader: Fetching tags...")
        self.tags = self._fetch_all_pages(f"{self.base}/products/tags", {"hide_empty": True})
        
        # 4. Products (Parent products only to build search index)
        logger.info("StoreLoader: Fetching products...")
        self.products, self._expected_product_count = self._fetch_all_pages_with_total(
            f"{self.base}/products", 
            {"status": "publish", "per_page": 100}
        )

    def _fetch_currency_symbol(self) -> str:
        """Fetches the active currency symbol from WooCommerce."""
        logger.info("StoreLoader: Fetching store currency...")
        try:
            url = f"{self.base}/data/currencies/current"
            resp = self.session.get(
                url, 
                params={"consumer_key": self.consumer_key, "consumer_secret": self.consumer_secret},
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            
            symbol = data.get("symbol")
            if symbol:
                return symbol
                
            code = data.get("code", "USD")
            return self._CURRENCY_MAP.get(code.upper(), "$")
            
        except Exception as e:
            logger.warning(f"StoreLoader: Failed to fetch currency symbol, defaulting to $. Error: {e}")
            return "$"

    @staticmethod
    def _currency_code_to_symbol(code: str) -> str:
        """Map ISO 4217 currency code to its symbol."""
        return StoreLoader._CURRENCY_MAP.get(code.upper(), code)

    def _read_json(self, filename: str) -> Optional[List]:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            logger.warning(f"StoreLoader: File not found: {filename}")
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _dump_lookups_for_debugging(self):
        """Dump the processed lookup dictionaries to a file in dev mode for inspection."""
        dump_path = os.path.join(DEV_CACHE_DIR, "lookups_debug.json")
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
                "product_by_name_lower": self.product_by_name_lower,
                "product_name_tokens": self.product_name_tokens,
            }
            os.makedirs(DEV_CACHE_DIR, exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, indent=2)
            logger.info(f"StoreLoader: Dumped lookup dictionaries to {dump_path} for debugging")
        except Exception as e:
            logger.error(f"StoreLoader: Failed to dump lookups: {e}")

    def _validate_load(self):
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

    def start_background_refresh(self):
        if DEV_CACHE_ENABLED:
            logger.info("StoreLoader: 🛑 Background refresh DISABLED in dev mode")
            return

        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def _refresh_loop():
            while True:
                interval = self._retry_interval if self._degraded else self._refresh_interval
                time.sleep(interval)
                label = "🔁 Degraded load retry" if self._degraded else "🔄 Background refresh"
                logger.info(f"StoreLoader: {label} — reloading store data...")
                try:
                    self.load_all()
                except Exception as e:
                    logger.error(f"StoreLoader: {label} failed | error={e}", exc_info=True)

        self._refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
        self._refresh_thread.start()
        logger.info(f"StoreLoader: Background refresh scheduled every {self._refresh_interval // 3600}h")

    # ─────────────────────────────────────────────
    # API PAGINATION HANDLERS
    # ─────────────────────────────────────────────

    def _fetch_all_pages(self, url: str, extra_params: Dict = None, max_retries: int = 3) -> List[Dict]:
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
            if extra_params: params.update(extra_params)

            data = None
            resp = None
            for attempt in range(max_retries):
                try:
                    resp = self.session.get(url, params=params, timeout=self.timeout)
                    if page == 1:
                        logger.debug(f"RAW RESPONSE [{resp.status_code}]: {resp.text[:500]}")
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                    else:
                        logger.error(f"StoreLoader: All retries failed for {url} page {page}")
                        return all_items

            if not data: break
            all_items.extend(data)
            total_pages = int(resp.headers.get("X-WP-TotalPages", 1)) if resp else 1
            if page >= total_pages: break
            page += 1

        return all_items

    def _fetch_all_pages_with_total(self, url: str, extra_params: Dict = None, max_retries: int = 3) -> Tuple[List[Dict], Optional[int]]:
        all_items = []
        page = 1
        per_page = 100
        expected_total = None

        while True:
            params = {
                "per_page": per_page,
                "page": page,
                "consumer_key": self.consumer_key,
                "consumer_secret": self.consumer_secret,
            }
            if extra_params: params.update(extra_params)

            data = None
            resp = None
            for attempt in range(max_retries):
                try:
                    resp = self.session.get(url, params=params, timeout=self.timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                    else:
                        return all_items, expected_total

            if not data: break
            all_items.extend(data)
            
            if resp:
                if expected_total is None:
                    try: expected_total = int(resp.headers.get("X-WP-Total", 0))
                    except: pass
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages: break
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
        except: pass
        return {}

    def _build_store_generic_terms(self) -> set:
        from collections import Counter
        word_counts = Counter()
        valid_cats = [c for c in self.categories if c.get("slug") != "uncategorized" and c.get("count", 0) > 0]
        for cat in valid_cats:
            for word in re.split(r"[\s\-_/&]+", cat.get("name", "").lower()):
                word = word.strip()
                if word and len(word) > 2:
                    word_counts[word] += 1
        generic = set()
        for word, count in word_counts.items():
            if count >= 2:
                solo_count = sum(1 for c in valid_cats if c.get("name", "").lower().strip() == word)
                if (count - solo_count) > solo_count:
                    generic.add(word)
        return generic

    def _build_longest_match_catalog(self):
        """
        Caches a pre-sorted list of all store terms (longest to shortest) 
        for lightning-fast O(1) access during chat message parsing.
        """
        catalog_items = []
        
        # 1. Products
        if hasattr(self, 'product_by_name_lower'):
            for name, data in self.product_by_name_lower.items():
                catalog_items.append((name, 'product', data))
                
        # 2. Categories
        if hasattr(self, 'category_by_name_lower'):
            for name, data in self.category_by_name_lower.items():
                # Prevent 0-product categories from hijacking exact string matches
                if data.get("count", 0) == 0:
                    continue
                catalog_items.append((name, 'category', data))
                
        # 3. Attributes (With Combined "Term + Label" support)
        if hasattr(self, 'all_attributes_raw'):
            for attr in self.all_attributes_raw:
                # Bulletproof label extraction for Woo & Custom APIs
                label_raw = attr.get("attribute_label") or attr.get("name") or attr.get("attribute_name") or ""
                label = label_raw.lower().strip()
                
                for attr_val in attr.get("terms", []):
                    name = attr_val.get("name", "").lower().strip()
                    
                    attr_payload = {
                        'label': label,
                        'slug': attr_val.get("slug"),
                        'taxonomy': attr.get("taxonomy") or attr.get("attribute_name") or "",
                        'name': attr_val.get("name", "")
                    }
                    
                    catalog_items.append((name, 'attribute', attr_payload))
                    
                    # Store combined term + label to prevent Tag-collisions
                    if label and label not in name:
                        combined = f"{name} {label}"
                        catalog_items.append((combined, 'attribute', attr_payload))
                        
        # 4. Tags
        if hasattr(self, 'tag_by_name_lower'):
            for name, data in self.tag_by_name_lower.items():
                if data.get("count", 0) == 0:
                    continue
                catalog_items.append((name, 'tag', data))
        catalog_items.sort(key=lambda x: len(x[0]), reverse=True)
        self.longest_match_catalog = catalog_items

    def _build_lookups(self):
        self._store_generic_terms = self._build_store_generic_terms()

        self.attribute_by_slug = {}
        self.attribute_by_id = {}
        self.category_by_slug = {}
        self.category_by_id = {}
        self.category_by_name_lower = {}
        self.category_slugs_by_name = {}
        self.tag_by_slug = {}
        self.tag_by_id = {}
        self.tag_by_name_lower = {}
        self.product_by_name_lower = {}
        self.product_name_tokens = []

        # Process Attributes (Custom API guarantees this is populated)
        if self.all_attributes_raw:
            for attr in self.all_attributes_raw:
                if not attr.get("visible", True): continue
                taxonomy_slug = attr.get("taxonomy", "")
                attr_id = attr.get("attribute_id")
                entry = {
                    "id": attr_id,
                    "name": attr.get("attribute_label") or attr.get("name") or attr.get("attribute_name") or "",
                    "slug": taxonomy_slug
                }
                self.attribute_by_slug[taxonomy_slug] = entry
                self.attribute_by_id[attr_id] = entry
                self.attribute_terms[attr_id] = attr.get("terms", [])

        for cat in self.categories:
            cat_id = cat["id"]
            name_lower = cat.get("name", "").lower()
            entry = {"id": cat_id, "name": cat["name"], "slug": cat.get("slug", ""), "count": cat.get("count", 0)}
            self.category_by_id[cat_id] = entry
            self.category_by_slug[entry["slug"]] = entry
            self.category_by_name_lower[name_lower] = entry
            if name_lower not in self.category_slugs_by_name:
                self.category_slugs_by_name[name_lower] = []
            self.category_slugs_by_name[name_lower].append(entry["slug"])

            if entry["slug"] != "uncategorized" and entry["count"] > 0:
                self._generate_category_keywords(entry)

        for tag in self.tags:
            name_lower = tag.get("name", "").lower()
            entry = {"id": tag["id"], "name": tag["name"], "slug": tag["slug"], "count": tag.get("count", 0)}
            self.tag_by_id[tag["id"]] = entry
            self.tag_by_slug[tag["slug"]] = entry
            self.tag_by_name_lower[name_lower] = entry

        for product in self.products:
            name = product.get("name", "").strip()
            if not name: continue
            
            name_lower = name.lower()
            entry = {"id": product.get("id"), "name": name, "slug": product.get("slug", "")}
            self.product_by_name_lower[name_lower] = entry
            
            # Token match list
            # words = re.split(r'[\s\-_]+', name_lower)
            # for word in words:
            #     if len(word) > 2 and word not in self._store_generic_terms:
            #         self.product_name_tokens.append((word, entry))

        # 🚀 NEW: Build the sorted catalog for O(1) longest-string matching
        self._build_longest_match_catalog()

    def _generate_category_keywords(self, cat_entry: Dict):
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

    # ─────────────────────────────────────────────
    # QUERY METHODS
    # ─────────────────────────────────────────────
    def get_category_id(self, keyword: str) -> Optional[int]:
        keyword = keyword.lower().strip()

        if keyword in self.category_by_name_lower:
            entry = self.category_by_name_lower[keyword]
            if entry.get("count", 0) > 0:
                return entry["id"]
                
        if keyword in self.category_by_slug:
            entry = self.category_by_slug[keyword]
            if entry.get("count", 0) > 0:
                return entry["id"]
                
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

        for name_lower, entry in self.product_by_name_lower.items():
            if re.search(rf'\b{re.escape(name_lower)}\b', text_lower):
                candidates.append(entry)

        # if not candidates:
        #     for token, entry in self.product_name_tokens:
        #         if re.search(rf'\b{re.escape(token)}\b', text_lower):
        #             candidates.append(entry)

        stop_words = self._store_generic_terms.copy()
        stop_words.update({"sample", "samples", "product", "item", "size", "sizes"})
        
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
                
                # STRICT MATCH FOR DIMENSIONS 
                if needle_clean and term_clean_raw:
                    if re.search(rf'(?<!\d){re.escape(needle_clean)}(?!\d)', term_clean_raw):
                        partial.append(term["id"])
                # STANDARD MATCH FOR TEXT
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

                # STRICT MATCH FOR DIMENSIONS 
                if needle_clean and term_clean_raw:
                    if re.search(rf'(?<!\d){re.escape(needle_clean)}(?!\d)', term_clean_raw):
                        partial_slug = term.get("slug", "")
                else:
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
                "semantic_vectors": len(self.semantic_keys) if self.semantic_keys else 0
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