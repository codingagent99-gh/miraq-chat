#!/usr/bin/env python3
"""dev_bootstrap_db.py — create the MAIN control-plane schema on an empty
database and stamp Alembic at head.

WHY THIS EXISTS
───────────────
`flask db upgrade` cannot bootstrap a fresh main database. The migration
chain's base revision (11a150d3635e) does:

    op.add_column("tenants", sa.Column("build_attempts", ...))

with down_revision = None. It ALTERS the tenants table; nothing in the chain
ever CREATES it. The table originally came from db.create_all() before Alembic
was adopted, and the chain was started from an already-populated schema.

So on an empty DB every path fails the same way:
  - server.py deliberately does NOT call db.create_all()
    ("Schema creation/updates are owned entirely by Alembic")
  - `flask db upgrade` dies on revision 1 with
    relation "tenants" does not exist

This script closes that loop the same way per-tenant databases are already
bootstrapped in routes/provisioning.py: create_all() from the models, then
write the head revision straight into alembic_version.

That is correct rather than a shortcut, because the models ARE head state —
every column the four migrations add (build_attempts, widget_*, tenant_id
split) is already declared on the model classes, and catalog_snapshots is a
model too. create_all() produces the schema those migrations would have
produced, so stamping at head is an accurate claim, not a papering-over.

SCOPE
─────
Creates every table in the model metadata: tenants, catalog_snapshots,
conversations, messages, chat_usage, shopify_tokens. The last four are
per-tenant tables and are redundant in the control-plane DB — but this is
exactly what provisioning already does in the other direction (it runs
create_all() against tenant DBs, which creates a tenants table there too;
the _is_tenant_database() guards in the migrations exist because of it).
Symmetric with existing behaviour, and harmless.

SAFETY
──────
Refuses to touch a database that already has a tenants table unless --force is
given. This is a bootstrap tool, not a migration tool: on a database that is
already set up, `flask db upgrade` is the right command.
"""
import argparse
import os
import sys
import urllib.parse

import psycopg2

MODEL_TABLES = (
    "tenants", "catalog_snapshots",
    "conversations", "messages", "chat_usage", "shopify_tokens",
)


def _table_exists(dsn, table):
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        return cur.fetchone()[0] is not None
    finally:
        conn.close()


def _ensure_database(dsn):
    """CREATE DATABASE if absent — mirrors server.ensure_database_exists()."""
    parts = urllib.parse.urlparse(dsn)
    db_name = parts.path[1:]
    admin = psycopg2.connect(
        dbname="postgres", user=parts.username, password=parts.password,
        host=parts.hostname, port=parts.port,
    )
    admin.autocommit = True
    try:
        cur = admin.cursor()
        cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone():
            print(f"  database {db_name!r} already exists")
            return
        # Identifier can't be parameterised; quote it so an odd name is still safe.
        cur.execute(f'CREATE DATABASE "{db_name}"')
        print(f"  created database {db_name!r}")
    finally:
        admin.close()


def _create_schema(dsn):
    """create_all() against the main DSN, in a throwaway app.

    A throwaway Flask app rather than importing server.py: importing server
    starts the vector model load, the refresh scheduler and the CORS seed —
    all of which either take ~10s or hit the very tables we are about to
    create.
    """
    from flask import Flask
    from models import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = dsn
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()
        created = sorted(db.metadata.tables.keys())
    print(f"  created tables: {', '.join(created)}")

    try:
        with app.app_context():
            db.engine.dispose()
    except Exception:
        pass


def _stamp_head(dsn, migrations_dir):
    """Write the current head revision into alembic_version.

    Deliberately via psycopg2 rather than alembic's stamp command — the same
    choice provisioning._stamp_alembic_version() makes, for the same reason
    (Flask-SQLAlchemy's pool hangs on a throwaway app's stamp()).
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", migrations_dir)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if not head:
        print("  ! no alembic head found — skipping stamp", file=sys.stderr)
        return None

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS alembic_version (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
        """)
        cur.execute("DELETE FROM alembic_version")
        cur.execute("INSERT INTO alembic_version (version_num) VALUES (%s)", (head,))
    finally:
        conn.close()
    print(f"  stamped alembic_version at head {head}")
    return head


def main():
    p = argparse.ArgumentParser(
        description="Create the main control-plane schema on an empty DB and stamp Alembic at head.",
    )
    p.add_argument("--dsn", default=None,
                   help="postgresql://user:pass@host:port/dbname. "
                        "Defaults to $DATABASE_URL / $SQLALCHEMY_DATABASE_URI.")
    p.add_argument("--migrations-dir", default="migrations")
    p.add_argument("--force", action="store_true",
                   help="proceed even if a tenants table already exists")
    args = p.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL") or os.environ.get("SQLALCHEMY_DATABASE_URI")
    if not dsn:
        raise SystemExit(
            "No DSN. Pass --dsn or set DATABASE_URL / SQLALCHEMY_DATABASE_URI."
        )

    if not os.path.isdir(args.migrations_dir):
        raise SystemExit(
            f"migrations dir not found: {args.migrations_dir!r} — "
            "run this from the backend root, or pass --migrations-dir."
        )

    safe = urllib.parse.urlparse(dsn)
    print(f"target: {safe.hostname}:{safe.port}{safe.path}")

    print("\n[1/3] ensuring database exists ...")
    _ensure_database(dsn)

    if not args.force and _table_exists(dsn, "tenants"):
        raise SystemExit(
            "\n  'tenants' already exists — refusing to run.\n"
            "  This is a bootstrap tool for an EMPTY database. On a database "
            "that is already set up, use `flask db upgrade`.\n"
            "  Pass --force only if you know create_all() is a no-op here."
        )

    print("\n[2/3] creating schema from models ...")
    _create_schema(dsn)

    print("\n[3/3] stamping alembic ...")
    head = _stamp_head(dsn, args.migrations_dir)

    print("\nDone. Start the server, then provision:")
    print("  python dev_provision.py --license-id dev-miraq ...")
    if head:
        print(f"\nAlembic is at {head}; future `flask db upgrade` runs will "
              "apply only new revisions.")


if __name__ == "__main__":
    main()