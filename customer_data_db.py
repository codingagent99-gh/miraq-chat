"""
customer_data_db.py - small helpers for the command-line scripts that read or
rewrite shopper data outside the web app (inspect_chat.py, encrypt_existing.py).

Same connection rule as purge_chat_history.py and DBEngineRegistry: DATABASE_URL
points at the control-plane database; a store's database is the same URL with
the database name swapped for tenants.db_name.
"""

from __future__ import annotations

import os
import sys
from typing import List, NamedTuple, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


class TenantDB(NamedTuple):
    tenant_id: str
    db_name: str
    shop: str        # shopify_domain or site_domain, for display
    status: str


def base_dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set (.env)", file=sys.stderr)
        sys.exit(2)
    return dsn


def dsn_for(db_name: str) -> str:
    head, _sep, _old = base_dsn().rpartition("/")
    return f"{head}/{db_name}"


def engine_for(db_name: str):
    return create_engine(dsn_for(db_name), pool_pre_ping=True, hide_parameters=True, future=True)


def control_engine():
    return create_engine(base_dsn(), pool_pre_ping=True, hide_parameters=True, future=True)


def list_tenants(shop: Optional[str] = None, include_inactive: bool = False,
                 missing: Optional[List[TenantDB]] = None) -> List[TenantDB]:
    """Stores to work on. `shop` matches shopify_domain, site_domain or db_name.

    Only stores whose database actually exists on the server: a tenants row
    can outlive its database (a removed or half-provisioned store). Pass a
    list as `missing` to get those rows back, for reporting.
    """
    sql = ("SELECT tenant_id::text, db_name, "
           "COALESCE(shopify_domain, site_domain, db_name), status, "
           "EXISTS (SELECT 1 FROM pg_database d WHERE d.datname = tenants.db_name) "
           "FROM tenants")
    where, params = [], {}
    if not include_inactive:
        where.append("status = 'active'")
    if shop:
        where.append("(:s IN (shopify_domain, site_domain, db_name))")
        params["s"] = shop.strip().lower()
    if where:
        sql += " WHERE " + " AND ".join(where)
    eng = control_engine()
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(sql + " ORDER BY db_name"), params).all()
    finally:
        eng.dispose()
    found = []
    for *fields, exists in rows:
        (found if exists else (missing if missing is not None else [])).append(TenantDB(*fields))
    return found


def has_table(conn, table: str) -> bool:
    return bool(conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar())
