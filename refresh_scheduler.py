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
import os
import time
import threading

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# How long an archived tenant's database is kept before it is dropped.
#
# /deactivate-tenant no longer drops anything: it archives, and this window is
# what makes that call reversible. A teardown fired by a leaked credential, or
# by a staging clone carrying a copy of the real store's credentials, can be
# undone by reprovisioning with the same tenant_uuid — until the sweep below
# runs. 0 is honoured (drop on the next tick) for anyone who wants the old
# behaviour back.
TENANT_ARCHIVE_GRACE_DAYS = int(os.getenv("TENANT_ARCHIVE_GRACE_DAYS", "7"))

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

    def _reap_non_live_loaders(self):
        try:
            with self._app.app_context():
                import uuid as uuid_mod
                from models import Tenant
                from store_registry import get_engine_registry

                resident = [tid for tid, _ in self._registry.resident_loaders()]
                if not resident:
                    return

                ids = []
                for tid in resident:
                    try:
                        ids.append(uuid_mod.UUID(str(tid)))
                    except ValueError:
                        logger.warning(f"RefreshScheduler: resident loader with a non-UUID id | tenant={tid!r}")

                # "warming" counts as live: a build may be running right now.
                live = {
                    str(t.tenant_id): t for t in Tenant.query.filter(
                        Tenant.tenant_id.in_(ids),
                        Tenant.status.in_(["active", "provision_failed", "warming"]),
                    ).all()
                }
                gone = {
                    str(t.tenant_id): t for t in Tenant.query.filter(Tenant.tenant_id.in_(ids)).all()
                    if str(t.tenant_id) not in live
                }

                engines = get_engine_registry()
                for tid in resident:
                    if str(tid) in live:
                        continue
                    row = gone.get(str(tid))
                    self._registry.evict(str(tid))
                    if engines is not None and row is not None and row.db_name:
                        engines.dispose_for(row.db_name)
                    logger.info(
                        f"RefreshScheduler: evicted loader for a non-live tenant | tenant={tid} "
                        f"status={row.status if row is not None else 'row missing'}"
                    )
        except Exception as e:
            logger.error(f"RefreshScheduler: reap of non-live loaders failed | {e}", exc_info=True)

    def _loop(self):
        while not self._stop.wait(self._tick):
            try:
                self._scan_once()
            except Exception as e:
                logger.error(f"RefreshScheduler: scan failed | error={e}", exc_info=True)

    def _scan_once(self):
        from tenant_snapshot_store import snapshot_store, loader_to_snapshot_dict

        # ── Reap loaders whose tenant is no longer live ─────────────────────
        # Runs FIRST, so nothing below touches a torn-down tenant.
        #
        # Teardown (uninstall, deactivate) evicts the loader and disposes the
        # engine only in the worker that handled the request. Every other
        # gunicorn worker keeps its own TenantRegistry, and there the loader
        # stays resident. The token sweep below would then find the
        # shopify_tokens row gone, treat it as "expired/missing", and try a
        # client_credentials refresh for an app the store has just uninstalled
        # — failing and logging an error every tick, per worker, until that
        # worker restarted. The catalog sweep would keep probing the store
        # with a revoked token the same way. Checking status here fixes it
        # within one tick, in every worker, with no cross-process signalling.
        self._reap_non_live_loaders()

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

        # ── Widget branding refresh ─────────────────────────────────────────
        # Runs every tick, but is_widget_branding_stale() gates real fetches
        # to once per 24h per tenant (and skips non-Woo tenants). This is what
        # lets /widget-config be a pure DB read instead of a live call to the
        # tenant's WordPress site on every widget load.
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

        # ── Drop databases of long-archived tenants ─────────────────────────
        # The deferred half of teardown. Rows stay for audit with db_name
        # intact; only the physical database goes. Tenants whose database is
        # already gone are skipped via one pg_database query rather than a
        # per-row existence check, because archived rows accumulate forever.
        try:
            with self._app.app_context():
                from datetime import datetime, timezone, timedelta
                from flask import current_app
                from models import Tenant
                from tenant_db_provisioner import (
                    drop_tenant_database, list_existing_databases, TenantDBProvisionError,
                )

                cutoff = datetime.now(timezone.utc) - timedelta(days=TENANT_ARCHIVE_GRACE_DAYS)
                expired = Tenant.query.filter(
                    Tenant.status == "archived",
                    Tenant.archived_at.isnot(None),
                    Tenant.archived_at < cutoff,
                ).all()

                if expired:
                    base_dsn = current_app.config["SQLALCHEMY_DATABASE_URI"]
                    existing = list_existing_databases(base_dsn)
                    for tenant in expired:
                        if tenant.db_name not in existing:
                            continue   # already dropped on an earlier tick
                        try:
                            drop_tenant_database(base_dsn, tenant.db_name)
                            logger.info(
                                f"RefreshScheduler: dropped archived tenant database | "
                                f"tenant={tenant.tenant_id} db={tenant.db_name} "
                                f"archived_at={tenant.archived_at}"
                            )
                        except TenantDBProvisionError as e:
                            # Left in `existing` for the next tick — a failed
                            # drop must not silently become a skipped one.
                            logger.error(
                                f"RefreshScheduler: drop failed | tenant={tenant.tenant_id} "
                                f"db={tenant.db_name} | {e}"
                            )
        except Exception as e:
            logger.error(f"RefreshScheduler: archived tenant sweep failed | {e}", exc_info=True)

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