"""
refresh_scheduler.py — ONE shared daemon that refreshes resident tenant catalogs.

Replaces the per-loader refresh thread (see StoreLoader.start_background_refresh).
Holds NO long-lived reference to any loader: each tick it asks the registry for
the currently-resident loaders, refreshes those due, and drops the references —
so an LRU-evicted loader becomes garbage-collectable.
"""

from __future__ import annotations
import time
import threading

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# How often the scheduler wakes to scan. The per-loader cadence still lives in
# StoreLoader.due_for_refresh(); this is just the poll granularity.
_TICK_SECONDS = 5 * 60


class RefreshScheduler:
    def __init__(self, registry, app=None, tick_seconds: int = _TICK_SECONDS):
        """
        Args:
            registry: the TenantRegistry — must expose resident_loaders() -> list.
            app:      Flask app, so load_all()'s DB-touching paths (and the token
                      manager) can open an app context from this thread.
            tick_seconds: poll granularity.
        """
        self._registry = registry
        self._app = app
        self._tick = tick_seconds
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="miraq-refresh-scheduler", daemon=True
        )
        self._thread.start()
        logger.info(
            f"RefreshScheduler: started (tick={self._tick}s) — "
            "shared across all resident tenants"
        )

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self._tick):
            try:
                self._scan_once()
            except Exception as e:
                logger.error(f"RefreshScheduler: scan failed | error={e}", exc_info=True)

    def _scan_once(self):
        from tenant_snapshot_store import snapshot_store, loader_to_snapshot_dict

        loaders = list(self._registry.resident_loaders())
        for license_id, loader in loaders:
            try:
                if loader.due_for_refresh():
                    label = "🔁 Degraded retry" if loader._degraded else "🔄 Refresh"
                    logger.info(f"RefreshScheduler: {label} — tenant={license_id}")
                    loader.load_all()
                    if license_id != "__default__" and not loader._degraded:
                        snapshot_store.save(license_id, loader_to_snapshot_dict(loader))
                        logger.info(f"RefreshScheduler: snapshot updated | tenant={license_id}")
            except Exception as e:
                logger.error(f"RefreshScheduler: refresh failed | tenant={license_id} | error={e}", exc_info=True)
        loaders = None