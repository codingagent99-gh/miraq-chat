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
    REQUEST_TIMEOUT, BROWSER_HEADERS,
    DEV_CACHE_ENABLED, UPDATE_DEV_CACHE_ENABLED,
    CURRENCY_MAP,
)
from tenant_config import TenantConfig
from store_loader.cache import BoundedVariationCache
from store_loader.fetcher import (
    load_from_local_files,
    load_from_live_api,
    load_from_backend_db,
    save_to_local_files,
    dump_lookups_for_debugging,
)
from store_loader.lookup_builder import (
    build_all_lookups,
    build_semantic_vectors,
)
from store_loader.queries import StoreQueryMixin

logger = get_logger("miraq_chat")


def load_vector_model():
    """
    Load the shared SentenceTransformer model once at process startup.
    Pass the returned object into StoreLoader(vector_model=...) — never
    call this per-tenant; one instance serves all loaders in the process.
    """
    logger.info("Loading Semantic Vector Model (all-MiniLM-L6-v2)...")
    try:
        from sentence_transformers import SentenceTransformer
        if DEV_CACHE_ENABLED:
            model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
            logger.info("Loaded vector model from local HuggingFace cache (dev mode, offline).")
        else:
            model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Loaded vector model (online).")
        return model
    except Exception as e:
        if not DEV_CACHE_ENABLED:
            logger.warning(
                f"Could not reach HuggingFace Hub ({type(e).__name__}: {e}). "
                "Retrying with local disk cache only..."
            )
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
                logger.info("Loaded vector model from local HuggingFace cache (offline fallback).")
                return model
            except Exception as e2:
                logger.error(
                    f"Failed to load vector model — not found in local cache either. "
                    f"Semantic fallback will not be available. ({type(e2).__name__}: {e2})"
                )
                return None
        else:
            logger.error(
                f"Failed to load vector model from local cache. "
                f"Has the model been downloaded yet? Run once with DEV_CACHE=false to fetch it. "
                f"({type(e).__name__}: {e})"
            )
            return None


class StoreLoader(StoreQueryMixin):
    """Fetches and caches all WooCommerce / Shopify taxonomy data."""

    _CURRENCY_MAP = CURRENCY_MAP

    def __init__(self, config: TenantConfig, vector_model, app=None):
        """
        Args:
            config:       Per-tenant credentials and URLs.
            vector_model: Shared SentenceTransformer instance, loaded once at startup
                          by load_vector_model() and injected here — never duplicated
                          per tenant.
            app:          Flask app instance, forwarded to ShopifyTokenManager so it
                          can open app contexts for DB access in background threads.
        """
        self._config      = config
        self._flask_app   = app
        self.license_id       = config.license_id
        self.base             = config.woo_base_url
        self.custom_api_base  = config.custom_api_base_url
        self.consumer_key     = config.woo_key
        self.consumer_secret  = config.woo_secret
        self.timeout          = REQUEST_TIMEOUT
        self.shopify_domain   = config.shopify_domain

        # ── Shopify token manager ─────────────────────────────────────────────
        # If we have OAuth credentials, use the token manager (auto-refresh).
        # Fall back to config.shopify_admin_token for local dev.
        self._token_manager = None
        if config.ecommerce_backend == "shopify":
            if config.shopify_client_id and config.shopify_client_secret:
                from store_loader.shopify_token_manager import ShopifyTokenManager
                self._token_manager = ShopifyTokenManager(app=app)
                logger.info("StoreLoader: Shopify token manager initialised (auto-refresh enabled)")
            elif config.shopify_admin_token:
                logger.warning(
                    "StoreLoader: shopify_client_id/secret not set — "
                    "falling back to hardcoded shopify_admin_token (expires daily!)"
                )
            else:
                logger.error(
                    "StoreLoader: Shopify backend selected but no credentials found. "
                    "Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET in .env"
                )

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

        # Semantic vector model — loaded once at startup via load_vector_model()
        # and injected here. Never instantiated per-tenant.
        self.vector_model = vector_model

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
        self._consecutive_failures: int = 0
        self._max_retry_interval: int = 4 * 3600
        self._refresh_thread: Optional[threading.Thread] = None
        self._degraded: bool = False
        self._degraded_reasons: list = []
        self._expected_product_count: Optional[int] = None
        self._loaded_from_cache: bool = False
        self.conflicts: List[Dict] = []

    # ─── Token helper ───

    def _get_shopify_token(self) -> str:
        """
        Return the current Shopify access token.
        Uses the token manager when available, otherwise falls back to the
        hardcoded env value (dev / legacy).
        """
        if self._token_manager:
            return self._token_manager.get_token()
        return self._config.shopify_admin_token

    # ─── Loading orchestration ───

    def load_all(self):
        """Load store data from the configured backend.

        Backend selection (config.ecommerce_backend):
          - "shopify"     → live Shopify GraphQL API (always, no dev cache)
          - "woocommerce" → local JSON files when DEV_CACHE=true, else live API
        """
        if not self._lock.acquire(blocking=False):
            logger.warning("StoreLoader: load_all() already in progress — skipping this trigger.")
            return

        try:
            loaded_via_push = False

            # ── Fetch raw data ────────────────────────────────────────
            if self._config.ecommerce_backend == "shopify":
                from store_loader.shopify_fetcher import load_from_shopify
                data = load_from_shopify(
                    store_domain=self.shopify_domain,
                    admin_token=self._get_shopify_token(),
                )
                self._loaded_from_cache = False

                if UPDATE_DEV_CACHE_ENABLED:
                    if data["products"] and data["categories"]:
                        save_to_local_files(
                            data["categories"], data["tags"],
                            data["all_attributes_raw"], data["products"],
                        )
                        logger.info("StoreLoader: ✅ Dev cache files updated from Shopify")

                        # Verify the folder was actually created
                        from store_loader.config import DATA_DIR
                        if os.path.isdir(DATA_DIR):
                            files = os.listdir(DATA_DIR)
                            logger.info(f"StoreLoader: 📁 Cache folder confirmed at '{DATA_DIR}' | files={files}")
                        else:
                            logger.error(f"StoreLoader: ❌ Cache folder NOT found at '{DATA_DIR}' after save")
            else:
                # Pushed data (Postgres, from the WordPress plugin) takes
                # priority over dev-cache/live-pull when present. A DB
                # error here (missing app context, connection hiccup) must
                # NOT fail the whole load — it just means "no push
                # available," same as a genuinely empty table — so the
                # lookup is wrapped and any exception falls through.
                pushed = self._try_load_from_backend_db()

                if pushed is not None:
                    data = pushed
                    self._loaded_from_cache = False
                    loaded_via_push = True
                    logger.info(f"StoreLoader: 📥 Loaded catalog from plugin push | tenant={self.license_id}")
                elif DEV_CACHE_ENABLED:
                    data = load_from_local_files()
                    self._loaded_from_cache = True
                else:
                    data = load_from_live_api(
                        self.session, self.base, self.custom_api_base,
                        self.consumer_key, self.consumer_secret, self.timeout,
                    )
                    self._loaded_from_cache = False

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
            # Pushed payloads have no X-WP-Total to compare against — not a
            # paginated live fetch — so treat the pushed list as
            # self-consistently authoritative rather than trusting an
            # expected_product_count key the plugin never actually sends.
            # Mirrors apply_pushed_catalog()'s identical reasoning below.
            if loaded_via_push:
                self._expected_product_count = len(data["products"])
            else:
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

    def apply_pushed_catalog(self, data: dict) -> None:
        """
        Applies a full catalog PUSHED by the WordPress plugin
        (class-catalog-push.php), bypassing load_from_live_api() entirely.

        Used because this host's WAF blocks backend-initiated requests to
        WooCommerce's REST API — the plugin gathers the same data itself
        (internal REST dispatch, never leaves the WP server) and pushes it
        here instead of us pulling it.

        `data` must have the same shape load_from_live_api() /
        load_from_local_files() return: categories, tags, products,
        all_attributes_raw, currency_symbol, and (optionally)
        expected_product_count.
        """
        if not self._lock.acquire(blocking=False):
            logger.warning("StoreLoader: apply_pushed_catalog() skipped — a load is already in progress.")
            return

        try:
            self.categories         = data["categories"]
            self.tags               = data["tags"]
            self.products           = data["products"]
            self.all_attributes_raw = data["all_attributes_raw"]
            self.currency_symbol    = data.get("currency_symbol", self.currency_symbol)
            # No X-WP-Total header to compare against here (this isn't a
            # paginated live fetch) — the pushed product list IS the total,
            # so treat it as 100% to avoid a false "partial products" flag
            # in _validate_load().
            self._expected_product_count = len(data["products"])
            self._loaded_from_cache = False

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
            logger.error(f"StoreLoader: failed to apply pushed catalog: {e}", exc_info=True)

        finally:
            self._lock.release()

        threading.Thread(target=self._run_scanner_async, daemon=True).start()

    def due_for_refresh(self) -> bool:
        """
        True if this loader's catalog should be reloaded now. Carries the old
        cadence so the shared scheduler (refresh_scheduler.py) can decide per
        loader without owning any timing state itself:
          - dev mode  → never (catalog is pinned to local cache)
          - degraded  → retry on the short interval
          - healthy   → refresh on the normal interval
        """
        if DEV_CACHE_ENABLED:
            return False
        if self._last_loaded is None:
            return True  # never successfully loaded — try as soon as scheduled
        if self._degraded:
            backoff = min(
                self._retry_interval * (2 ** self._consecutive_failures),
                self._max_retry_interval
            )
            return (time.time() - self._last_loaded) >= backoff
        return (time.time() - self._last_loaded) >= self._refresh_interval

    def start_background_refresh(self):
        """
        Boot only the Shopify token-manager refresh loop (if active).

        Catalog refresh is NO LONGER per-loader: a single shared scheduler
        (refresh_scheduler.py) walks resident loaders and calls load_all() on
        those whose due_for_refresh() is True. A per-loader daemon thread would
        hold a strong reference to its loader and defeat LRU eviction in the
        tenant registry, so it has been removed deliberately.
        """
        if self._token_manager:
            self._token_manager.start()

        if DEV_CACHE_ENABLED:
            logger.info("StoreLoader: 🛑 Catalog background refresh DISABLED in dev mode")
            return

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

        if self._degraded:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0

    def _try_load_from_backend_db(self) -> Optional[dict]:
        """
        Wraps load_from_backend_db() so a DB error (no app context, a
        connection hiccup, etc.) degrades gracefully to dev-cache/live-pull
        instead of failing load_all() outright. This path is a preference
        over live pull, never a hard requirement — live pull must always
        still work as the safety net.
        """
        try:
            return load_from_backend_db(self.license_id)
        except Exception as e:
            logger.warning(
                f"StoreLoader: load_from_backend_db() failed "
                f"({type(e).__name__}: {e}) — falling back to dev-cache/live-pull "
                f"| tenant={self.license_id}"
            )
            return None

    def _log_load_summary(self):
        if self._config.ecommerce_backend == "shopify":
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