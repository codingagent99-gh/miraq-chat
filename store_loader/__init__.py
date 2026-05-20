"""
store_loader — Fetches and caches all WooCommerce taxonomy data.

Public API:
  - StoreLoader — the main class
  - BoundedVariationCache — LRU cache for variation data
"""

import os
import json
import time
import threading
import requests
from typing import List, Dict, Optional

from chat_logger import get_logger
from models.catalog import CatalogAttribute, CatalogCategory, CatalogTag
from store_loader.config import (
    WOO_BASE_URL, CUSTOM_API_BASE_URL,
    WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET,
    REQUEST_TIMEOUT, BROWSER_HEADERS,
    DEV_CACHE_ENABLED, UPDATE_DEV_CACHE_ENABLED,
    CURRENCY_MAP,
    ECOMMERCE_BACKEND, SHOPIFY_STORE_DOMAIN, SHOPIFY_ADMIN_TOKEN,   # ← added
)
from store_loader.cache import BoundedVariationCache
from store_loader.fetcher import (
    load_from_local_files,
    load_from_live_api,
    save_to_local_files,
    dump_lookups_for_debugging,
)
from store_loader.lookup_builder import (
    build_all_lookups,
    build_semantic_vectors,
)
from store_loader.queries import StoreQueryMixin

logger = get_logger("miraq_chat")


class StoreLoader(StoreQueryMixin):
    """Fetches and caches all WooCommerce / Shopify taxonomy data."""

    _CURRENCY_MAP = CURRENCY_MAP

    def __init__(self):
        self.base             = WOO_BASE_URL
        self.custom_api_base  = CUSTOM_API_BASE_URL
        self.consumer_key     = WOO_CONSUMER_KEY
        self.consumer_secret  = WOO_CONSUMER_SECRET
        self.timeout          = REQUEST_TIMEOUT
        self.shopify_domain      = SHOPIFY_STORE_DOMAIN   # ← added
        self.shopify_admin_token = SHOPIFY_ADMIN_TOKEN    # ← added

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

        # Semantic vector model
        logger.info("Loading Semantic Vector Model (all-MiniLM-L6-v2)...")
        try:
            from sentence_transformers import SentenceTransformer
            if DEV_CACHE_ENABLED:
                self.vector_model = SentenceTransformer('all-MiniLM-L6-v2', local_files_only=True)
                logger.info("Loaded vector model from local HuggingFace cache (dev mode, offline).")
            else:
                self.vector_model = SentenceTransformer('all-MiniLM-L6-v2')
                logger.info("Loaded vector model (online).")
        except Exception as e:
            if not DEV_CACHE_ENABLED:
                logger.warning(
                    f"Could not reach HuggingFace Hub ({type(e).__name__}: {e}). "
                    "Retrying with local disk cache only..."
                )
                try:
                    from sentence_transformers import SentenceTransformer
                    self.vector_model = SentenceTransformer('all-MiniLM-L6-v2', local_files_only=True)
                    logger.info("Loaded vector model from local HuggingFace cache (offline fallback).")
                except Exception as e2:
                    logger.error(
                        f"Failed to load vector model — not found in local cache either. "
                        f"Semantic fallback will not be available. ({type(e2).__name__}: {e2})"
                    )
                    self.vector_model = None
            else:
                logger.error(
                    f"Failed to load vector model from local cache. "
                    f"Has the model been downloaded yet? Run once with DEV_CACHE=false to fetch it. "
                    f"({type(e).__name__}: {e})"
                )
                self.vector_model = None

        self.semantic_dictionary: Dict = {}
        self.semantic_tensors = None
        self.semantic_keys: List = []

        # Raw data
        self.categories: List[Dict] = []
        self.tags: List[Dict] = []
        self.attributes: List[Dict] = []
        self.products: List[Dict] = []
        self.all_attributes_raw: List[Dict] = []

        # Lookup indexes
        self.category_by_id: Dict[int, Dict] = {}
        self.category_by_name_lower: Dict[str, Dict] = {}
        self.category_slugs_by_name: Dict[str, List[str]] = {}
        self.tag_by_id: Dict[int, Dict] = {}
        self.product_by_name_lower: Dict[str, Dict] = {}
        self.product_name_tokens: List[tuple] = []
        self.longest_match_catalog: List[tuple] = []
        self.category_keywords: Dict[str, int] = {}
        self._store_generic_terms: set = set()
        self._category_synonyms: Dict[str, str] = self._load_category_synonyms()
        self.product_variation_schema: Dict[int, Dict] = {}
        self.variation_detail_cache = BoundedVariationCache(max_size=200, ttl=3600)
        self.attribute_by_id: Dict[int, Dict] = {}
        self.attribute_by_key: Dict[str, CatalogAttribute] = {}
        self.tag_by_name_lower: Dict[str, Dict] = {}
        self.category_by_key: Dict[str, CatalogCategory] = {}
        self.tag_by_key: Dict[str, CatalogTag] = {}
        self.currency_symbol: str = "$"

        # State
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

    # ─── Loading orchestration ───

    def load_all(self):
        """Load store data from the configured backend.

        Backend selection (ECOMMERCE_BACKEND env var):
          - "shopify"     → live Shopify GraphQL API (always, no dev cache)
          - "woocommerce" → local JSON files when DEV_CACHE=true, else live API
        """
        # Fix #2: skip if a load is already in progress instead of queuing behind it.
        # Two concurrent loads would double the request rate and worsen 429 storms.
        if not self._lock.acquire(blocking=False):
            logger.warning("StoreLoader: load_all() already in progress — skipping this trigger.")
            return

        try:
            # ── Fetch raw data ────────────────────────────────────────
            if ECOMMERCE_BACKEND == "shopify":
                from store_loader.shopify_fetcher import load_from_shopify
                data = load_from_shopify(
                    store_domain=self.shopify_domain,
                    admin_token=self.shopify_admin_token,
                )
                self._loaded_from_cache = False
            elif DEV_CACHE_ENABLED:
                data = load_from_local_files()
                self._loaded_from_cache = True
            else:
                data = load_from_live_api(
                    self.session, self.base, self.custom_api_base,
                    self.consumer_key, self.consumer_secret, self.timeout,
                )
                self._loaded_from_cache = False

                # Fix #1: only persist the dev cache when the fetch actually
                # returned meaningful data.  Saving empty lists would wipe any
                # previously-good cache files on the disk.
                if UPDATE_DEV_CACHE_ENABLED:
                    if data["products"] and data["categories"]:
                        save_to_local_files(
                            data["categories"], data["tags"],
                            data["all_attributes_raw"], data["products"],
                        )
                        logger.info("StoreLoader: ✅ Dev cache files updated from live API")
                    else:
                        logger.warning(
                            f"StoreLoader: ⚠️  Skipping dev cache update — fetch returned "
                            f"{len(data['products'])} products / {len(data['categories'])} categories. "
                            "Existing cache files preserved."
                        )

            # ── Apply fetched data ────────────────────────────────────
            self.categories         = data["categories"]
            self.tags               = data["tags"]
            self.products           = data["products"]
            self.all_attributes_raw = data["all_attributes_raw"]
            self.currency_symbol    = data["currency_symbol"]
            self._expected_product_count = data.get("expected_product_count")

            # ── Build indexes ─────────────────────────────────────────
            build_all_lookups(self)
            self._validate_load()
            self._last_loaded = time.time()

            if self.vector_model:
                build_semantic_vectors(self)

            if DEV_CACHE_ENABLED:
                dump_lookups_for_debugging(self)

            self._log_load_summary()

        except Exception as e:
            self._degraded = True
            self._degraded_reasons = [str(e)]
            logger.error(f"StoreLoader: ❌ Failed to load store data: {e}", exc_info=True)

        finally:
            self._lock.release()

        # Run scanner outside the lock
        threading.Thread(target=self._run_scanner_async, daemon=True).start()
        
    def sync_from_webhook(self):
        """Background function triggered by WordPress Action Webhooks."""
        logger.info("Webhook triggered! Refreshing WooCommerce data in background...")
        try:
            self.load_all()
        except Exception as e:
            logger.error(f"Webhook sync failed: {e}", exc_info=True)

    def start_background_refresh(self):
        """Start the background thread that periodically reloads store data."""
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

    # ─── Private helpers ───

    @staticmethod
    def _load_category_synonyms() -> Dict[str, str]:
        raw = os.getenv("CATEGORY_SYNONYMS", "{}")
        try:
            synonyms = json.loads(raw)
            if isinstance(synonyms, dict):
                return {k.lower(): v.lower() for k, v in synonyms.items()}
        except Exception:
            pass
        return {}

    @staticmethod
    def _currency_code_to_symbol(code: str) -> str:
        return CURRENCY_MAP.get(code.upper(), code)

    def _run_scanner_async(self):
        try:
            from conflict_scanner import run_conflict_simulation
            self.conflicts = run_conflict_simulation(self)
        except Exception as e:
            logger.error(f"StoreLoader: Conflict scanner failed: {e}", exc_info=True)

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

    def _log_load_summary(self):
        if ECOMMERCE_BACKEND == "shopify":
            mode = "Live Shopify GraphQL API"
        elif DEV_CACHE_ENABLED:
            mode = "Local Dev Cache"
        else:
            mode = "Live WooCommerce API"

        status     = "⚠️ DEGRADED" if self._degraded else "✅ HEALTHY"
        attr_count = sum(len(a.terms) for a in self.attribute_by_key.values())
        vector_count = len(self.semantic_keys) if self.semantic_keys else 0

        summary = [
            f"StoreLoader: Initialization Complete [{status}]",
            f"  ├─ Mode:       {mode}",
            f"  ├─ Currency:   {self.currency_symbol}",
            f"  ├─ Products:   {len(self.products)}",
            f"  ├─ Categories: {len(self.categories)}",
            f"  ├─ Tags:       {len(self.tags)}",
            f"  ├─ Attributes: {len(self.attribute_by_key)} (with {attr_count} terms)",
            f"  ├─ Keywords:   {len(self.category_keywords)} (generated for search index)",
            f"  └─ Vectors:    {vector_count} (for semantic fallback)",
        ]
        if self._degraded:
            summary.append("  ❌ Degraded Reasons:")
            for reason in self._degraded_reasons:
                summary.append(f"     - {reason}")
        logger.info("\n" + "\n".join(summary) + "\n")