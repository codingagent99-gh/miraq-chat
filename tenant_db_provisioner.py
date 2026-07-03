"""
tenant_db_provisioner.py — Creates a tenant's dedicated Postgres database.

Idempotent: safe on every activation/re-activation, including a plugin retry
double-firing /provision-tenant — CREATE DATABASE is skipped if it exists.

db_name is attacker-influenced in origin (derived from licenseId, which
arrives in an external payload), so it's validated against a hard allow-list
before reaching CREATE DATABASE, and quoted via psycopg2.sql rather than
f-string interpolated. Phase 3's _derive_db_name() already produces hash-based
names that satisfy this allow-list by construction — this is defense-in-depth,
not a replacement for that.
"""

from __future__ import annotations
import re
import urllib.parse

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from chat_logger import get_logger

logger = get_logger("miraq_chat")

# Kept identical to db_engine_registry.py's allow-list deliberately — a
# db_name that fails this can never reach CREATE DATABASE or engine creation.
_VALID_DB_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class TenantDBProvisionError(Exception):
    pass


def _maintenance_dsn(base_dsn: str) -> str:
    """Swap the database segment of base_dsn for the 'postgres' maintenance DB."""
    result = urllib.parse.urlparse(base_dsn)
    return result._replace(path="/postgres").geturl()


def ensure_tenant_database(base_dsn: str, db_name: str) -> bool:
    """
    Idempotently create the tenant's dedicated database if missing.
    Returns True if just created, False if it already existed.
    Raises TenantDBProvisionError on validation or connection failure.
    """
    if not _VALID_DB_NAME.match(db_name or ""):
        raise TenantDBProvisionError(f"Refusing to provision unsafe db_name: {db_name!r}")

    dsn = _maintenance_dsn(base_dsn)
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:
        raise TenantDBProvisionError(f"Could not connect to maintenance DB: {e}") from e

    try:
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)  # CREATE DATABASE can't run in a transaction
        cur = conn.cursor()

        cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone():
            logger.info(f"TenantDBProvisioner: database already exists | db={db_name}")
            return False

        # db_name is already validated above; sql.Identifier is still used
        # rather than f-string interpolation because CREATE DATABASE can't
        # take a parameterised identifier through execute() at all — this is
        # psycopg2's correct way to quote an identifier safely.
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        logger.info(f"TenantDBProvisioner: ✅ created database | db={db_name}")
        return True
    except Exception as e:
        raise TenantDBProvisionError(f"Failed to create database {db_name!r}: {e}") from e
    finally:
        conn.close()
        
def drop_tenant_database(base_dsn: str, db_name: str) -> None:
    """
    Terminate active connections and DROP the tenant database.
    Called only from /deactivate-tenant after both registries are cleared.

    Raises TenantDBProvisionError if the database doesn't exist (idempotent
    from the caller's point of view — already gone is fine) or on failure.
    """
    if not _VALID_DB_NAME.match(db_name or ""):
        raise TenantDBProvisionError(f"Refusing to drop unsafe db_name: {db_name!r}")

    dsn = _maintenance_dsn(base_dsn)
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:
        raise TenantDBProvisionError(f"Could not connect to maintenance DB: {e}") from e

    try:
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()

        # Check it exists — missing DB is treated as already-deleted (idempotent).
        cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db_name,))
        if not cur.fetchone():
            logger.info(f"TenantDBProvisioner: database already gone | db={db_name}")
            return

        # Terminate all active connections before dropping — DROP DATABASE fails
        # if any client is still connected, even with autocommit.
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (db_name,)
        )
        terminated = cur.rowcount
        if terminated:
            logger.info(f"TenantDBProvisioner: terminated {terminated} connection(s) | db={db_name}")

        cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))
        logger.info(f"TenantDBProvisioner: ✅ dropped database | db={db_name}")
    except Exception as e:
        raise TenantDBProvisionError(f"Failed to drop database {db_name!r}: {e}") from e
    finally:
        conn.close()