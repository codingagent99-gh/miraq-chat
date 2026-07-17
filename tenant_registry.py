"""
tenant_registry.py — Resident StoreLoader cache, keyed by tenant_id.

- OrderedDict LRU, INSERT-triggered eviction (pop oldest synchronously past cap).
  No timed eviction heartbeat — nothing to coordinate.
- Per-tenant build Lock for single-flight rehydration: two simultaneous misses
  for the same tenant don't both rebuild. (StoreLoader's own instance Lock can't
  do this — a miss has no instance yet; the guard must be keyed by tenant_id.)
- _rehydrate() is the Phase-2/Phase-4 seam: live load now, snapshot later.
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
    def __init__(self, vector_model, app=None):
        self._vector_model = vector_model
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

        _wp_base = (tenant_row.wp_base_url or "").rstrip("/")
        logger.info(f"TenantRegistry: _rehydrate started | tenant={tenant_row.license_id} | wp_base={_wp_base}")

        if not _wp_base:
            logger.error(f"TenantRegistry: wp_base_url is empty | tenant={tenant_row.license_id} — cannot fetch catalog")
            raise RuntimeError(f"wp_base_url is empty for tenant {tenant_row.license_id}")

        logger.info(f"TenantRegistry: building TenantConfig | tenant={tenant_row.license_id}")
        config = TenantConfig(
            wp_base_url=_wp_base,
            woo_base_url=f"{_wp_base}/wp-json/wc/v3",
            woo_store_api_url=f"{_wp_base}/wp-json/wc/store/v1",
            custom_api_base_url=f"{_wp_base}/wp-json/custom-api/v1",
            woo_key=tenant_row.woo_key or "",
            woo_secret=decrypt_secret(tenant_row.woo_secret_encrypted or ""),
            ecommerce_backend="woocommerce",
            license_id=tenant_row.license_id,
            tenant_id=str(tenant_row.tenant_id),
        )

        logger.info(f"TenantRegistry: woo_key={'present' if config.woo_key else 'MISSING'} | tenant={tenant_row.license_id}")
        logger.info(f"TenantRegistry: woo_secret={'present' if config.woo_secret else 'MISSING'} | tenant={tenant_row.license_id}")

        logger.info(f"TenantRegistry: constructing StoreLoader | tenant={tenant_row.license_id}")
        loader = StoreLoader(config=config, vector_model=self._vector_model, app=self._app)
        logger.info(f"TenantRegistry: StoreLoader constructed | tenant={tenant_row.license_id}")

        logger.info(f"TenantRegistry: checking snapshot | tenant={tenant_row.license_id}")
        snapshot = snapshot_store.load(str(tenant_row.tenant_id))

        if snapshot is not None:
            logger.info(f"TenantRegistry: snapshot found — applying | tenant={tenant_row.license_id} | products={len(snapshot.get('products', []))}")
            apply_snapshot_to_loader(loader, snapshot)
            logger.info(f"TenantRegistry: snapshot applied | tenant={tenant_row.license_id}")
        else:
            logger.info(f"TenantRegistry: no snapshot — starting live WooCommerce fetch | tenant={tenant_row.license_id}")
            logger.info(f"TenantRegistry: calling load_all() | tenant={tenant_row.license_id} | url={config.woo_base_url}")
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

        logger.info(f"TenantRegistry: starting background refresh | tenant={tenant_row.license_id}")
        loader.start_background_refresh()
        logger.info(f"TenantRegistry: _rehydrate complete | tenant={tenant_row.license_id}")
        return loader

    # ── catalog-push handling ────────────────────────────────────────────────

    def apply_pushed_catalog(self, tenant_row, data: dict) -> None:
        """
        Applies a full catalog PUSHED by the WordPress plugin (see
        class-catalog-push.php) for a tenant. Used instead of a live pull
        because this host's WAF blocks backend-initiated requests to
        WooCommerce's REST API — the plugin gathers the data itself and
        sends it here, so nothing in this method makes a network call.

        If the tenant's loader is already resident, updates it in place.
        Otherwise builds a fresh loader from the pushed data alone (no
        network call — unlike _rehydrate()'s no-snapshot fallback, which
        calls load_all() and would hit the same WAF block) and adds it to
        the resident cache, so the next chat request for this tenant
        doesn't have to wait on anything.

        Either way, persists a snapshot afterward so an LRU-evicted loader
        rehydrates from this data next time instead of attempting (and
        failing) a live pull.
        """
        tenant_id = str(tenant_row.tenant_id)

        with self._registry_lock:
            loader = self._loaders.get(tenant_id)
            if loader is not None:
                self._loaders.move_to_end(tenant_id)

        if loader is None:
            _wp_base = (tenant_row.wp_base_url or "").rstrip("/")
            config = TenantConfig(
                wp_base_url=_wp_base,
                woo_base_url=f"{_wp_base}/wp-json/wc/v3",
                woo_store_api_url=f"{_wp_base}/wp-json/wc/store/v1",
                custom_api_base_url=f"{_wp_base}/wp-json/custom-api/v1",
                woo_key=tenant_row.woo_key or "",
                woo_secret=decrypt_secret(tenant_row.woo_secret_encrypted or ""),
                ecommerce_backend="woocommerce",
                license_id=tenant_row.license_id,
                tenant_id=str(tenant_row.tenant_id),
            )
            loader = StoreLoader(config=config, vector_model=self._vector_model, app=self._app)
            logger.info(f"TenantRegistry: built fresh loader from pushed catalog | tenant={tenant_id}")

        loader.apply_pushed_catalog(data)

        if loader._degraded:
            logger.warning(
                f"TenantRegistry: pushed catalog left loader degraded — not added to cache | "
                f"tenant={tenant_id} | reasons={loader._degraded_reasons}"
            )
            return

        with self._registry_lock:
            self._loaders[tenant_id] = loader
            self._loaders.move_to_end(tenant_id)
            while len(self._loaders) > _MAX_RESIDENT_LOADERS:
                old_id, _old = self._loaders.popitem(last=False)
                logger.info(f"TenantRegistry: evicted resident loader | tenant={old_id}")

        from tenant_snapshot_store import snapshot_store, loader_to_snapshot_dict
        snapshot_store.save(str(tenant_row.tenant_id), loader_to_snapshot_dict(loader))
        logger.info(f"TenantRegistry: catalog push applied, snapshot saved | tenant={tenant_id}")

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
                