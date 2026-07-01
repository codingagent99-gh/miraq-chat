"""
migration_runner.py — Applies the project's existing Alembic migration
history (migrations/, alembic.ini, env.py — all UNCHANGED) to an arbitrary
tenant database.

Why a throwaway Flask app: env.py resolves its target database via
current_app.extensions['migrate'].db.engine — i.e. whatever app is on top of
Flask's app-context stack when migrations run, not a value passed in
directly. A throwaway app with its own SQLALCHEMY_DATABASE_URI set to the
tenant's DSN makes current_app resolve there for the duration of one call,
without ever touching the main app's shared, globally-mutable config — which
matters here since other requests and the refresh scheduler run concurrently
in the same process.

One throwaway app per call. Nothing is held past the function returning —
the engine it creates is explicitly disposed rather than left for GC, since
a batch run (run_migrations_for_all_tenants) could otherwise transiently
accumulate many open connections before garbage collection catches up.
"""

from __future__ import annotations
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional

from flask import Flask
from flask_migrate import Migrate, upgrade as flask_migrate_upgrade

from models import db, Tenant
from chat_logger import get_logger

logger = get_logger("miraq_chat")

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


class MigrationRunError(Exception):
    pass


def build_tenant_dsn(base_dsn: str, db_name: str) -> str:
    """Swap the database segment of base_dsn for db_name."""
    head, _sep, _old = base_dsn.rpartition("/")
    return f"{head}/{db_name}"


def _safe_dsn(dsn: str) -> str:
    """Strip credentials before logging."""
    if "@" in dsn:
        scheme, _, rest = dsn.partition("://")
        _, _, host_part = rest.partition("@")
        return f"{scheme}://***@{host_part}"
    return dsn


def run_migrations_for_dsn(dsn: str) -> None:
    """
    Apply the full migration history (alembic upgrade head) to the database
    at `dsn`. Safe on a brand-new empty database (creates every table from
    scratch) or one partway through an interrupted prior run — Alembic's own
    version table makes re-running idempotent; only un-applied revisions run.
    """
    throwaway = Flask(__name__)
    throwaway.config["SQLALCHEMY_DATABASE_URI"] = dsn
    throwaway.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(throwaway)
    Migrate(throwaway, db, directory=_MIGRATIONS_DIR)

    try:
        with throwaway.app_context():
            flask_migrate_upgrade(directory=_MIGRATIONS_DIR)
        logger.info(f"MigrationRunner: ✅ applied | dsn={_safe_dsn(dsn)}")
    except Exception as e:
        raise MigrationRunError(f"Migration failed for {_safe_dsn(dsn)}: {e}") from e
    finally:
        # Explicit dispose rather than relying on GC timing — matters for the
        # batch path below, which constructs many of these in a loop.
        try:
            with throwaway.app_context():
                db.engine.dispose()
        except Exception:
            pass


@dataclass
class TenantMigrationResult:
    license_id: str
    success: bool
    error: Optional[str] = None


def run_migrations_for_all_tenants(base_dsn: str, only_license_id: Optional[str] = None) -> List[TenantMigrationResult]:
    """
    Batch path: applies migrations to every tenant database (or just one, via
    only_license_id — for retrying a single failure without re-running the
    whole fleet). One tenant's failure does not abort the rest; each result
    is tracked independently, and schema_migrated_at / last_build_error are
    updated on the Tenant row so retries are visible and resumable.

    Call this from a CLI/admin action when rolling out a new migration across
    existing tenants — NOT on the request path.
    """
    query = Tenant.query
    if only_license_id:
        query = query.filter_by(license_id=only_license_id)

    results: List[TenantMigrationResult] = []
    for tenant in query.all():
        dsn = build_tenant_dsn(base_dsn, tenant.db_name)
        try:
            run_migrations_for_dsn(dsn)
            tenant.schema_migrated_at = datetime.now(timezone.utc)
            tenant.last_build_error = None
            results.append(TenantMigrationResult(tenant.license_id, success=True))
        except MigrationRunError as e:
            logger.error(f"MigrationRunner: tenant failed | license_id={tenant.license_id} | {e}")
            tenant.last_build_error = str(e)
            results.append(TenantMigrationResult(tenant.license_id, success=False, error=str(e)))
        db.session.commit()  # per-tenant commit — one failure doesn't roll back prior successes

    succeeded = sum(1 for r in results if r.success)
    logger.info(f"MigrationRunner: batch complete | {succeeded}/{len(results)} succeeded")
    return results