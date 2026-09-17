"""
refresh_scheduler.py — ONE shared daemon that refreshes resident tenant catalogs.

Replaces the per-loader refresh thread (see StoreLoader.start_background_refresh's
docstring for why that thread had to go — it held a strong reference to its own
loader forever, defeating TenantRegistry's LRU eviction). Holds NO long-lived
reference to any loader: each tick it asks the registry for the currently-resident
loaders, refreshes those due, and drops the references — so an LRU-evicted loader
becomes garbage-collectable.
"""

from __future__ import annotations
import time
import threading

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# How often the scheduler wakes to scan. The per-loader cadence lives in
# StoreLoader._reason_to_reload() (catalog-version probe + interval backstop,
# same cadence values as before — see that method); this is just the poll
# granularity for the scan itself.
#
# This is coarser than the retired per-loader thread's own 60s poll
# (CATALOG_POLL_INTERVAL, now unused) — a deliberate trade for a shared
# scheduler: one tick here walks every resident tenant, so the interval
# has to account for N tenants' worth of fetch_catalog_version() calls, not
# just one. Tune via tick_seconds if 5 minutes is too coarse for catalog
# staleness at your current tenant count.
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
                from models import Tenant
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
                    _start_background_build(tenant.tenant_id, self._app)
        except Exception as e:
            logger.error(f"RefreshScheduler: stuck tenant sweep failed | {e}", exc_info=True)

        # ── Widget branding refresh — omitted ───────────────────────────────
        # The reference runs a widget-branding fetch sweep here (gated to once
        # per 24h per tenant). Not included — widget_branding.py is optional
        # per the conversion plan and nothing has been built against it yet.
        # Add both if that's wanted later.

        # ── Shopify token refresh ───────────────────────────────────────────────
        # Same reasoning as the catalog-refresh sweep below: ShopifyTokenManager
        # no longer runs its own background thread (see its
        # check_and_refresh_if_needed docstring) — this sweep is what actually
        # drives it now, for whichever Shopify tenants are currently resident.
        # A no-op for Woo tenants (loader._token_manager is None for them).
        try:
            with self._app.app_context():
                for tenant_id, loader in self._registry.resident_loaders():
                    token_manager = getattr(loader, "_token_manager", None)
                    if token_manager is not None:
                        token_manager.check_and_refresh_if_needed()
        except Exception as e:
            logger.error(f"RefreshScheduler: shopify token sweep failed | {e}", exc_info=True)

        # ── Normal catalog refresh ─────────────────────────────────────────────
        # Wrapped in app_context() so load_all()'s DB-touching paths have a
        # Flask/SQLAlchemy session available, same as the sweeps above.
        try:
            with self._app.app_context():
                loaders = list(self._registry.resident_loaders())
                for tenant_id, loader in loaders:
                    try:
                        reason = loader._reason_to_reload()
                        if reason:
                            logger.info(f"RefreshScheduler: {reason} — tenant={tenant_id}")
                            loader.load_all()
                            if not loader._degraded:
                                snapshot_store.save(tenant_id, loader_to_snapshot_dict(loader))
                                logger.info(f"RefreshScheduler: snapshot updated | tenant={tenant_id}")
                    except Exception as e:
                        logger.error(f"RefreshScheduler: refresh failed | tenant={tenant_id} | error={e}", exc_info=True)
                loaders = None
        except Exception as e:
            logger.error(f"RefreshScheduler: catalog refresh sweep failed | {e}", exc_info=True)