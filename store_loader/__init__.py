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
    save_to_local_files,
    dump_lookups_for_debugging,
    fetch_catalog_version,
)
from store_loader.lookup_builder import build_all_lookups
from store_loader.queries import StoreQueryMixin

logger = get_logger("miraq_chat")


class StoreLoader(StoreQueryMixin):
    """Fetches and caches all WooCommerce / Shopify taxonomy data."""

    _CURRENCY_MAP = CURRENCY_MAP

    def __init__(self, config: TenantConfig, app=None):
        """
        Args:
            config: Per-tenant credentials and URLs, built per-request by
                    tenant_registry.py's _rehydrate() from the tenants table.
            app:    Flask app instance, forwarded to ShopifyTokenManager so
                    it can open app contexts for DB access in background
                    threads.
        """
        self._config          = config
        self._flask_app       = app
        self.license_id       = config.license_id
        self.tenant_id        = config.tenant_id
        self.ecommerce_backend = config.ecommerce_backend
        self.wp_base_url      = config.wp_base_url
        self.base             = config.woo_base_url
        self.custom_api_base  = config.custom_api_base_url
        self.consumer_key     = config.woo_key
        self.consumer_secret  = config.woo_secret
        self.timeout          = REQUEST_TIMEOUT
        self.shopify_domain   = config.shopify_domain

        # ── Shopify token manager ─────────────────────────────────────────────
        # The app's client credentials are process-wide (app_config), not
        # per-tenant, so the gate is: are they configured at all, and does
        # this tenant have a domain to hold a token for. Falls back to
        # config.shopify_admin_token for local dev.
        self._token_manager = None
        if config.ecommerce_backend == "shopify":
            from app_config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
            if SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET and config.shopify_domain:
                from store_loader.shopify_token_manager import ShopifyTokenManager
                self._token_manager = ShopifyTokenManager(config=config, app=app)
                logger.info(
                    "StoreLoader: Shopify token manager initialised "
                    f"(auto-refresh enabled) | domain={config.shopify_domain}"
                )
            elif config.shopify_admin_token:
                logger.warning(
                    "StoreLoader: app credentials or shopify_domain missing — "
                    "falling back to hardcoded shopify_admin_token (expires daily!)"
                )
            elif not config.shopify_domain:
                logger.error(
                    "StoreLoader: Shopify tenant has no shopify_domain. The OAuth "
                    "callback should have set it at install time."
                )
            else:
                logger.error(
                    "StoreLoader: Shopify backend selected but the app's credentials "
                    "are not configured. Set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET "
                    "in .env — these are app-wide, not per tenant."
                )

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

        # NOTE: the all-MiniLM-L6-v2 semantic vector model and its
        # tag/attribute/category tensors were removed — utils/typo_correction.py
        # already fuzzy-matches the same catalog vocabulary before Phase 1, and
        # this model was the only CPU-bound inference on the request path
        # (GIL-serializing) plus a large per-worker memory cost.

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
        # Ceiling, not cadence. The catalog-version probe below is what
        # normally triggers a reload; this is the backstop that still fires if
        # the probe is unavailable, or if the catalog changed in some way the
        # fingerprint does not cover.
        self._refresh_interval: int = 6 * 3600
        self._retry_interval: int = 2 * 60
        self._catalog_version: Optional[str] = None
        self._degraded: bool = False
        self._degraded_reasons: list = []
        self._expected_product_count: Optional[int] = None
        self._loaded_from_cache: bool = False

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

        Backend selection (self.ecommerce_backend, from TenantConfig):
          - "shopify"     → live Shopify GraphQL API (always, no dev cache)
          - "woocommerce" → local JSON files when DEV_CACHE=true, else live API
        """
        if not self._lock.acquire(blocking=False):
            logger.warning("StoreLoader: load_all() already in progress — skipping this trigger.")
            return

        try:
            # ── Fetch raw data ────────────────────────────────────────
            if self.ecommerce_backend == "shopify":
                from store_loader.shopify_fetcher import load_from_shopify
                data = load_from_shopify(
                    store_domain=self.shopify_domain,
                    admin_token=self._get_shopify_token(),
                    token_manager=self._token_manager,
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

            # ── Build indexes, then publish raw data + indexes together ──
            # Request threads never take self._lock, so anything assigned to
            # self here is visible to in-flight requests immediately. The raw
            # lists and the indexes derived from them are staged and swapped in
            # one update, so a reader sees the old catalog or the new one and
            # never a mix of the two.
            build_all_lookups(self, raw={
                "categories":              data["categories"],
                "tags":                    data["tags"],
                "products":                data["products"],
                "all_attributes_raw":      data["all_attributes_raw"],
                "currency_symbol":         data["currency_symbol"],
                "_expected_product_count": data.get("expected_product_count"),
            })
            self._validate_load()
            self._last_loaded = time.time()

            if DEV_CACHE_ENABLED:
                dump_lookups_for_debugging(self)

            self._log_load_summary()

        except Exception as e:
            self._degraded = True
            self._degraded_reasons = [str(e)]
            logger.error(f"StoreLoader: ❌ Failed to load store data: {e}", exc_info=True)

        finally:
            self._lock.release()

    def sync_from_webhook(self):
        """Background function triggered by WordPress Action Webhooks."""
        logger.info("Webhook triggered! Refreshing WooCommerce data in background...")
        try:
            self.load_all()
        except Exception as e:
            logger.error(f"Webhook sync failed: {e}", exc_info=True)

    def start_background_refresh(self):
        """
        Do this loader's one-time startup work: the initial Shopify token
        fetch, if this is a Shopify tenant. Despite the name, nothing here
        starts a background THREAD anymore for either concern:

        Catalog refresh is NO LONGER per-loader: a single shared scheduler
        (refresh_scheduler.py) walks resident loaders each tick and calls
        load_all() on whichever ones _reason_to_reload() says are due.

        Shopify token refresh is the same story — ShopifyTokenManager.start()
        does the initial fetch only; the same shared scheduler calls
        check_and_refresh_if_needed() per tick instead of the token manager
        running its own loop.

        Both used to be a per-loader/per-manager daemon thread holding a
        strong reference to itself for the life of the process —
        TenantRegistry's LRU eviction popped the loader from its dict, but
        the thread kept everything it touched alive and polling forever, one
        leaked thread per evicted tenant. Removed deliberately; see
        tenant_registry.py's module docstring for the fuller history.
        """
        # Do the initial Shopify token fetch (if this is a Shopify tenant).
        #
        # Guarded on purpose: the token manager must not be able to take the
        # rest of startup down with it. A token problem must stay a token
        # problem.
        if self._token_manager:
            try:
                self._token_manager.start()
            except Exception as e:
                logger.error(
                    f"StoreLoader: Shopify token manager failed to start — {e}",
                    exc_info=True,
                )

        if DEV_CACHE_ENABLED:
            logger.info("StoreLoader: 🛑 Catalog background refresh DISABLED in dev mode")

    def _reason_to_reload(self) -> Optional[str]:
        """Decide whether this loader should reload right now. None = stay put.

        Called once per tick by the shared RefreshScheduler for every
        resident loader — replaces what used to be embedded in this
        loader's own per-thread sleep loop (see start_background_refresh's
        docstring for why that thread is gone). The cadence logic is
        unchanged from that loop, just relocated here as a single callable:

          - degraded  → retry on the flat _retry_interval cadence,
            unconditionally. A broken loader wants to just try again, not
            wait on a catalog-version probe it may never see change while
            the underlying fetch itself is failing.
          - healthy   → the catalog-version probe below. Order matters
            there: the probe is consulted and its result recorded BEFORE
            load_all() runs, not after — an edit landing mid-reload then
            leaves self._catalog_version pointing at the token observed
            before the reload, so the next tick sees a different one and
            reloads again. Recording it after a successful load would
            swallow that edit until the interval backstop.

        A probe that returns None is explicitly NOT treated as "unchanged" —
        see fetch_catalog_version. The interval check still runs, so an
        unreachable or missing endpoint gives back the old interval-only
        behaviour rather than freezing the catalog.
        """
        if self._degraded:
            elapsed = time.time() - (self._last_loaded or 0)
            if elapsed >= self._retry_interval:
                return "🔁 Degraded load retry"
            return None

        elapsed = time.time() - (self._last_loaded or 0)

        # Shopify has no equivalent endpoint (the probe lives in the
        # WooCommerce plugin), so that backend stays on the plain timer.
        if self.ecommerce_backend != "shopify":
            version = fetch_catalog_version(
                self.session, self.custom_api_base,
                self.consumer_key, self.consumer_secret,
            )
            if version and version != self._catalog_version:
                previous = self._catalog_version
                self._catalog_version = version
                if previous is None:
                    # First successful probe since boot. Nothing is known to
                    # have changed; just record the baseline and let the
                    # interval check below decide.
                    logger.info(f"StoreLoader: catalog version baseline = {version[:12]}")
                else:
                    return f"📥 Catalog changed ({previous[:12]} → {version[:12]})"

        if elapsed >= self._refresh_interval:
            return f"🔄 Interval backstop ({int(elapsed // 3600)}h since last load)"

        return None

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
        if self.ecommerce_backend == "shopify":
            mode = "Live Shopify GraphQL API"
        elif DEV_CACHE_ENABLED:
            mode = "Local Dev Cache"
        else:
            mode = "Live WooCommerce API"

        status     = "⚠️ DEGRADED" if self._degraded else "✅ HEALTHY"
        attr_count = sum(len(a.terms) for a in self.attribute_by_key.values())

        summary = [
            f"StoreLoader: Initialization Complete [{status}]",
            f"  ├─ Mode:       {mode}",
            f"  ├─ Currency:   {self.currency_symbol}",
            f"  ├─ Products:   {len(self.products)}",
            f"  ├─ Categories: {len(self.categories)}",
            f"  ├─ Tags:       {len(self.tags)}",
            f"  ├─ Attributes: {len(self.attribute_by_key)} (with {attr_count} terms)",
            f"  └─ Keywords:   {len(self.category_keywords)} (generated for search index)",
        ]
        if self._degraded:
            summary.append("  ❌ Degraded Reasons:")
            for reason in self._degraded_reasons:
                summary.append(f"     - {reason}")
        logger.info("\n" + "\n".join(summary) + "\n")