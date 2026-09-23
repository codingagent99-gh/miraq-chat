"""
tenant_registry.py — Resident StoreLoader cache, keyed by tenant_id.

- OrderedDict LRU, INSERT-triggered eviction (pop oldest synchronously past cap).
  No timed eviction heartbeat — nothing to coordinate.
- Per-tenant build Lock for single-flight rehydration: two simultaneous misses
  for the same tenant don't both rebuild. (StoreLoader's own instance Lock can't
  do this — a miss has no instance yet; the guard must be keyed by tenant_id.)
- _rehydrate() is the Phase-3/Phase-4 seam: live load now, snapshot later.

Deviations from the reference this was adapted from:
  - No vector_model parameter anywhere. This codebase's StoreLoader takes no
    vector_model (see store_loader/__init__.py) — the semantic vector model
    was removed for GIL/memory reasons, superseded by fuzzy typo-correction.
  - _rehydrate() builds TenantConfig.ecommerce_backend from tenant_row.ecommerce_backend,
    not a hardcoded "woocommerce" — this is the Stage-2 seam the conversion
    plan calls for.
  - _rehydrate() calls loader.start_background_refresh() — which now only
    starts the Shopify token loop (a no-op for Stage 1's Woo tenants). It
    used to also spawn a permanent per-loader catalog-refresh thread with no
    stop signal, which would have leaked one live thread per evicted tenant
    forever, defeating this registry's LRU eviction. Phase 5's
    refresh_scheduler.py resolved this by moving catalog-version polling
    into a single shared scheduler that walks resident loaders on its own
    tick instead — see that module and StoreLoader.start_background_refresh's
    docstring for the fuller history.
  - apply_pushed_catalog() is dropped entirely. It exists in the reference
    to support a WordPress-plugin catalog push, needed only because that
    project's tenant host WAFs backend-initiated calls to WooCommerce's REST
    API. Per the conversion plan (D2), adopt that only if a tenant's host
    turns out to need it — nothing here does yet.
"""

from __future__ import annotations
import os
import threading
from collections import OrderedDict
from typing import Optional, List, Tuple

from chat_logger import get_logger
from tenant_config import TenantConfig
from tenant_crypto import decrypt_secret
from store_loader import StoreLoader

logger = get_logger("miraq_chat")

_MAX_RESIDENT_LOADERS = int(os.getenv("TENANT_LOADER_LRU_SIZE", "35"))

# How long get_loader() waits to acquire the build lock before giving up.
# Prevents silent hangs when a prior build thread is stuck on an HTTP timeout.
_BUILD_LOCK_TIMEOUT = 10  # seconds


class TenantRegistry:
    def __init__(self, app=None):
        self._app = app

        self._loaders: "OrderedDict[str, StoreLoader]" = OrderedDict()
        self._registry_lock = threading.Lock()
        self._build_locks: dict[str, threading.Lock] = {}
        self._build_locks_guard = threading.Lock()

    # ── scheduler interface ────────────────────────────────────────────────────

    def resident_loaders(self) -> List[Tuple[str, StoreLoader]]:
        with self._registry_lock:
            return list(self._loaders.items())

    # ── resolution ──────────────────────────────────────────────────────────────

    def get_loader(self, tenant_row) -> StoreLoader:
        tenant_id = str(tenant_row.tenant_id)
        logger.info(f"TenantRegistry: get_loader called | tenant={tenant_id}")

        # Fast path — already resident.
        with self._registry_lock:
            loader = self._loaders.get(tenant_id)
            if loader is not None:
                self._loaders.move_to_end(tenant_id)
                logger.info(f"TenantRegistry: cache hit | tenant={tenant_id}")
                return loader

        logger.info(f"TenantRegistry: cache miss — acquiring build lock | tenant={tenant_id}")

        build_lock = self._build_lock_for(tenant_id)
        acquired = build_lock.acquire(timeout=_BUILD_LOCK_TIMEOUT)

        if not acquired:
            logger.error(
                f"TenantRegistry: build lock timeout after {_BUILD_LOCK_TIMEOUT}s | tenant={tenant_id} "
                f"— another build thread is likely stuck on an HTTP call"
            )
            raise RuntimeError(
                f"Build lock timeout for tenant {tenant_id} — "
                f"a prior build may be stuck. Check for HTTP timeouts in StoreLoader logs."
            )

        logger.info(f"TenantRegistry: build lock acquired | tenant={tenant_id}")

        try:
            with self._registry_lock:
                loader = self._loaders.get(tenant_id)
                if loader is not None:
                    self._loaders.move_to_end(tenant_id)
                    logger.info(f"TenantRegistry: cache hit after lock wait | tenant={tenant_id}")
                    return loader

            logger.info(f"TenantRegistry: starting _rehydrate | tenant={tenant_id}")
            loader = self._rehydrate(tenant_row)
            logger.info(f"TenantRegistry: _rehydrate complete | tenant={tenant_id}")

            with self._registry_lock:
                self._loaders[tenant_id] = loader
                self._loaders.move_to_end(tenant_id)
                while len(self._loaders) > _MAX_RESIDENT_LOADERS:
                    old_id, _old = self._loaders.popitem(last=False)
                    logger.info(f"TenantRegistry: evicted resident loader | tenant={old_id}")

            logger.info(f"TenantRegistry: loader registered in cache | tenant={tenant_id}")
            return loader

        finally:
            build_lock.release()
            logger.info(f"TenantRegistry: build lock released | tenant={tenant_id}")

    def _build_lock_for(self, tenant_id: str) -> threading.Lock:
        with self._build_locks_guard:
            lock = self._build_locks.get(tenant_id)
            if lock is None:
                lock = threading.Lock()
                self._build_locks[tenant_id] = lock
            return lock

    # ── the Phase-4 rehydrate ─────────────────────────────────────────────────

    def _rehydrate(self, tenant_row) -> StoreLoader:
        from tenant_snapshot_store import snapshot_store, apply_snapshot_to_loader, loader_to_snapshot_dict

        logger.info(f"TenantRegistry: _rehydrate started | tenant={tenant_row.license_id} | backend={tenant_row.ecommerce_backend}")

        if tenant_row.ecommerce_backend == "shopify":
            logger.info(f"TenantRegistry: building TenantConfig (shopify) | tenant={tenant_row.license_id}")
            config = TenantConfig(
                # Woo fields are required (no default) on TenantConfig but
                # meaningless for a Shopify tenant — empty, not omitted.
                wp_base_url="",
                woo_base_url="",
                woo_store_api_url="",
                custom_api_base_url="",
                woo_key="",
                woo_secret="",
                ecommerce_backend="shopify",
                license_id=tenant_row.license_id,
                tenant_id=str(tenant_row.tenant_id),
                # Only the domain is per-tenant. The app's client_id /
                # client_secret come from app_config at the point of use —
                # they are the same for every store (see TenantConfig).
                shopify_domain=tenant_row.shopify_domain or "",
            )
            logger.info(
                f"TenantRegistry: shopify_domain={config.shopify_domain!r} | "
                f"tenant={tenant_row.license_id}"
            )
        else:
            _wp_base = (tenant_row.wp_base_url or "").rstrip("/")
            logger.info(f"TenantRegistry: wp_base={_wp_base} | tenant={tenant_row.license_id}")

            if not _wp_base:
                logger.error(f"TenantRegistry: wp_base_url is empty | tenant={tenant_row.license_id} — cannot fetch catalog")
                raise RuntimeError(f"wp_base_url is empty for tenant {tenant_row.license_id}")

            logger.info(f"TenantRegistry: building TenantConfig (woocommerce) | tenant={tenant_row.license_id}")
            _features = dict(tenant_row.features or {})
            config = TenantConfig(
                wp_base_url=_wp_base,
                woo_base_url=f"{_wp_base}/wp-json/wc/v3",
                woo_store_api_url=f"{_wp_base}/wp-json/wc/store/v1",
                custom_api_base_url=f"{_wp_base}/wp-json/custom-api/v1",
                woo_key=tenant_row.woo_key or "",
                woo_secret=decrypt_secret(tenant_row.woo_secret_encrypted or ""),
                ecommerce_backend=tenant_row.ecommerce_backend,
                license_id=tenant_row.license_id,
                tenant_id=str(tenant_row.tenant_id),
                http_profile=_features.get("http_profile") or "",
                http_profile_pinned=bool(_features.get("http_profile_pinned")),
                http_extra_headers=_features.get("http_extra_headers") or {},
            )
            logger.info(f"TenantRegistry: woo_key={'present' if config.woo_key else 'MISSING'} | tenant={tenant_row.license_id}")
            logger.info(f"TenantRegistry: woo_secret={'present' if config.woo_secret else 'MISSING'} | tenant={tenant_row.license_id}")
            logger.info(
                f"TenantRegistry: http_profile={config.http_profile or '<default>'} | "
                f"pinned={config.http_profile_pinned} | tenant={tenant_row.license_id}"
            )

        logger.info(f"TenantRegistry: constructing StoreLoader | tenant={tenant_row.license_id}")
        loader = StoreLoader(config=config, app=self._app)
        logger.info(f"TenantRegistry: StoreLoader constructed | tenant={tenant_row.license_id}")

        logger.info(f"TenantRegistry: checking snapshot | tenant={tenant_row.license_id}")
        snapshot = snapshot_store.load(str(tenant_row.tenant_id))

        if snapshot is not None:
            logger.info(f"TenantRegistry: snapshot found — applying | tenant={tenant_row.license_id} | products={len(snapshot.get('products', []))}")
            apply_snapshot_to_loader(loader, snapshot)
            logger.info(f"TenantRegistry: snapshot applied | tenant={tenant_row.license_id}")
        else:
            logger.info(f"TenantRegistry: no snapshot — starting live fetch | tenant={tenant_row.license_id}")
            logger.info(f"TenantRegistry: calling load_all() | tenant={tenant_row.license_id} | backend={config.ecommerce_backend}")
            loader.load_all()
            logger.info(
                f"TenantRegistry: load_all() returned | tenant={tenant_row.license_id} | "
                f"degraded={loader._degraded} | products={len(loader.products)} | "
                f"categories={len(loader.categories)}"
            )
            if loader._degraded:
                logger.warning(
                    f"TenantRegistry: loader degraded after live fetch | tenant={tenant_row.license_id} | "
                    f"reasons={loader._degraded_reasons}"
                )
            else:
                logger.info(f"TenantRegistry: saving snapshot | tenant={tenant_row.license_id}")
                snapshot_store.save(str(tenant_row.tenant_id), loader_to_snapshot_dict(loader))
                logger.info(f"TenantRegistry: snapshot saved | tenant={tenant_row.license_id}")

        # Does this loader's one-time startup work: the initial Shopify
        # token fetch if this is a Shopify tenant (a no-op for Woo tenants).
        # Catalog refresh is NOT started here: a single shared
        # RefreshScheduler (see refresh_scheduler.py) walks resident loaders
        # on its own tick instead — for BOTH catalog refresh and Shopify
        # token refresh (ShopifyTokenManager.check_and_refresh_if_needed) —
        # which is what makes LRU eviction here actually work. See this
        # module's docstring.
        loader.start_background_refresh()
        logger.info(f"TenantRegistry: _rehydrate complete | tenant={tenant_row.license_id}")
        return loader

    def get_build_lock(self, tenant_id: str) -> threading.Lock:
        return self._build_lock_for(tenant_id)

    def evict(self, tenant_id: str) -> None:
        logger.info(f"TenantRegistry: evict called | tenant={tenant_id}")
        build_lock = self._build_lock_for(tenant_id)
        acquired = build_lock.acquire(timeout=2)
        try:
            with self._registry_lock:
                removed = self._loaders.pop(tenant_id, None)
            logger.info(
                f"TenantRegistry: evict complete | tenant={tenant_id} | "
                f"was_resident={removed is not None} | lock_acquired={acquired}"
            )
        finally:
            if acquired:
                build_lock.release()