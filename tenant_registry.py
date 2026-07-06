"""
tenant_registry.py — Resident StoreLoader cache, keyed by license_id.

- OrderedDict LRU, INSERT-triggered eviction (pop oldest synchronously past cap).
  No timed eviction heartbeat — nothing to coordinate.
- Per-tenant build Lock for single-flight rehydration: two simultaneous misses
  for the same tenant don't both rebuild. (StoreLoader's own instance Lock can't
  do this — a miss has no instance yet; the guard must be keyed by license_id.)
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


class TenantRegistry:
    def __init__(self, vector_model, app=None):
        """
        Args:
            vector_model: the ONE shared SentenceTransformer (from load_vector_model()).
            app:          Flask app, forwarded to each StoreLoader.
        """
        self._vector_model = vector_model
        self._app = app

        self._loaders: "OrderedDict[str, StoreLoader]" = OrderedDict()
        self._registry_lock = threading.Lock()          # guards the OrderedDict
        self._build_locks: dict[str, threading.Lock] = {}  # per-tenant single-flight
        self._build_locks_guard = threading.Lock()       # guards _build_locks

    # ── scheduler interface ────────────────────────────────────────────────────

    def resident_loaders(self) -> List[Tuple[str, StoreLoader]]:
        with self._registry_lock:
            return list(self._loaders.items())

    # ── resolution ──────────────────────────────────────────────────────────────

    def get_loader(self, tenant_row) -> StoreLoader:
        """
        Return the resident loader for tenant_row.license_id, rehydrating on miss.
        Single-flight: concurrent misses for the same tenant rebuild once.
        """
        license_id = tenant_row.license_id

        # Fast path — already resident.
        with self._registry_lock:
            loader = self._loaders.get(license_id)
            if loader is not None:
                self._loaders.move_to_end(license_id)
                return loader

        # Miss — acquire this tenant's build lock (created once, reused).
        build_lock = self._build_lock_for(license_id)
        with build_lock:
            # Re-check: another thread may have built it while we waited.
            with self._registry_lock:
                loader = self._loaders.get(license_id)
                if loader is not None:
                    self._loaders.move_to_end(license_id)
                    return loader

            loader = self._rehydrate(tenant_row)

            with self._registry_lock:
                self._loaders[license_id] = loader
                self._loaders.move_to_end(license_id)
                # Insert-triggered eviction: drop oldest past the cap.
                while len(self._loaders) > _MAX_RESIDENT_LOADERS:
                    old_id, _old = self._loaders.popitem(last=False)
                    logger.info(f"TenantRegistry: evicted resident loader | tenant={old_id}")
            return loader

    def _build_lock_for(self, license_id: str) -> threading.Lock:
        with self._build_locks_guard:
            lock = self._build_locks.get(license_id)
            if lock is None:
                lock = threading.Lock()
                self._build_locks[license_id] = lock
            return lock

    # ── the Phase-2 / Phase-4 seam ───────────────────────────────────────────────

    def _rehydrate(self, tenant_row) -> StoreLoader:
        """
        PHASE 4: try the persisted snapshot first (seconds, no network calls).
        Falls back to a live load_all() only if no snapshot exists yet — in
        steady state this shouldn't fire, since the provisioner builds the
        snapshot once at activation before the tenant is marked 'active'.
        """
        from tenant_snapshot_store import snapshot_store, apply_snapshot_to_loader, loader_to_snapshot_dict
        _wp_base = (tenant_row.wp_base_url or "").rstrip("/")
        logger.info(f"TenantRegistry: _rehydrate started | tenant={tenant_row.license_id} wp_base={_wp_base}")

        config = TenantConfig(
            wp_base_url=_wp_base,
            woo_base_url=f"{_wp_base}/wp-json/wc/v3",
            woo_store_api_url=f"{_wp_base}/wp-json/wc/store/v1",
            custom_api_base_url=f"{_wp_base}/wp-json/custom-api/v1",
            woo_key=tenant_row.woo_key or "",
            woo_secret=decrypt_secret(tenant_row.woo_secret_encrypted or ""),
            ecommerce_backend="woocommerce",
        )
        loader = StoreLoader(config=config, vector_model=self._vector_model, app=self._app)
        logger.info(f"TenantRegistry: checking snapshot | tenant={tenant_row.license_id}")
        snapshot = snapshot_store.load(tenant_row.license_id)
        logger.info(f"TenantRegistry: snapshot={'found' if snapshot else 'not found'} | tenant={tenant_row.license_id}")

        if snapshot is not None:
            logger.info(f"TenantRegistry: rehydrating tenant={tenant_row.license_id} from snapshot")
            apply_snapshot_to_loader(loader, snapshot)
        else:
            logger.info(f"TenantRegistry: starting live load | tenant={tenant_row.license_id}")
            loader.load_all()
            logger.info(f"TenantRegistry: live load complete | tenant={tenant_row.license_id} degraded={loader._degraded}")
            if not loader._degraded:
                snapshot_store.save(tenant_row.license_id, loader_to_snapshot_dict(loader))
                logger.info(f"TenantRegistry: snapshot persisted after build | tenant={tenant_row.license_id}")

        loader.start_background_refresh()
        return loader
    
    def get_build_lock(self, license_id: str) -> threading.Lock:
        """
        Public accessor so the provisioner can share the exact same per-tenant
        lock used here for RAM-miss rehydration — preventing a double
        activation (plugin retry) and a concurrent rehydrate from both kicking
        off a full WooCommerce fetch + vector encode for the same tenant.
        """
        return self._build_lock_for(license_id)
    
    def evict(self, license_id: str) -> None:
        # Try to acquire the build lock with a short timeout so deactivation
        # isn't blocked by an in-progress build thread. If we can't acquire it
        # in time, evict anyway — the build thread will finish and find the
        # tenant archived, then exit cleanly.
        build_lock = self._build_lock_for(license_id)
        acquired = build_lock.acquire(timeout=2)
        try:
            with self._registry_lock:
                self._loaders.pop(license_id, None)
            logger.info(f"TenantRegistry: evicted | tenant={license_id} | lock_acquired={acquired}")
        finally:
            if acquired:
                build_lock.release()