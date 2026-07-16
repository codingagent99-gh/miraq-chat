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

        # ── Retry stuck tenants ───────────────────────────────────────────────
        try:
            with self._app.app_context():
                from models import Tenant, db
                from datetime import datetime, timezone, timedelta

                stuck = Tenant.query.filter(
                    Tenant.status.in_(["warming", "provision_failed"]),
                    Tenant.schema_migrated_at.isnot(None),
                    Tenant.archived_at.is_(None),
                    Tenant.build_attempts < 5,   # give up after 5 tries; stop matching this sweep
                    Tenant.created_at < datetime.now(timezone.utc) - timedelta(minutes=60)
                ).all()

                for tenant in stuck:
                    logger.info(f"RefreshScheduler: retrying stuck tenant | license_id={tenant.license_id} status={tenant.status}")
                    from routes.provisioning import _start_background_build
                    _start_background_build(tenant.license_id, self._app)
        except Exception as e:
            logger.error(f"RefreshScheduler: stuck tenant sweep failed | {e}", exc_info=True)

        # ── Widget branding refresh ─────────────────────────────────────────
        # Runs every tick (5 min) but is_widget_branding_stale() gates actual
        # fetches to once per 24h per tenant — this is what stops the 429s,
        # replacing "fetch on every widget load" with "fetch once a day".
        try:
            with self._app.app_context():
                from models import Tenant
                from widget_branding import (
                    fetch_and_store_widget_branding, is_widget_branding_stale,
                )

                active_tenants = Tenant.query.filter(
                    Tenant.status == "active",
                    Tenant.archived_at.is_(None),
                ).all()
                for tenant in active_tenants:
                    if is_widget_branding_stale(tenant):
                        fetch_and_store_widget_branding(tenant)
        except Exception as e:
            logger.error(f"RefreshScheduler: widget branding sweep failed | {e}", exc_info=True)

       # ── Normal catalog refresh ─────────────────────────────────────────────
        # Wrapped in app_context() because loader.load_all() reaches into the
        # tenant DB via _try_load_from_backend_db() (which needs Flask's
        # SQLAlchemy session, and therefore an app context). Matches the same
        # pattern the two sweeps above already use.
        try:
            with self._app.app_context():
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
        except Exception as e:
            logger.error(f"RefreshScheduler: catalog refresh sweep failed | {e}", exc_info=True)
        
        