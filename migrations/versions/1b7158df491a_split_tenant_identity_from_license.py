"""split tenant identity (tenant_id) from license (license_id)

Revision ID: 1b7158df491a
Revises: be0ba327ea80
Create Date: 2026-07-17 12:21:14.815428

Phase 1 of the free-activation / identity-license decoupling plan.

- tenants: adds tenant_id (UUID) as the new primary key. license_id becomes
  a nullable, unique column (still holds the same value it always did for
  every existing row — this migration does not touch any existing
  license_id value). Adds plan (free|pro).
- catalog_snapshots: FK moves from tenants.license_id to tenants.tenant_id.
  The old license_id column on catalog_snapshots is dropped after backfill,
  matching the ORM model (CatalogSnapshot no longer declares license_id).

Backfill judgment call (not spelled out in the plan doc): every tenant row
that exists *before* this migration runs came in through the paid
/provision-tenant path and carries a real verified license_id. Those rows
are explicitly set to plan='pro' here, rather than left at the column's
'free' default, so no currently-paying tenant is misclassified the moment
this migration lands. New tenant rows created after this migration
(license_id set) should pass plan='pro' explicitly from application code;
this migration does not change application code.

Caveat on downgrade: it assumes no catalog_snapshots row's tenant has a
NULL license_id (i.e. no free tenants — see local status_tracker) exist yet,
since it re-populates catalog_snapshots.license_id as NOT NULL from
tenants.license_id. That's true immediately after this migration (Phase 1
does not add /activate-free), but stops being true once a later phase
ships free tenants and this migration is rolled back after that point.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid

# revision identifiers, used by Alembic.
revision = '1b7158df491a'
down_revision = 'be0ba327ea80'
branch_labels = None
depends_on = None

def _is_tenant_database() -> bool:
    """
    No-op guard: skip if this migration is ever run against a per-tenant
    database instead of the main control-plane DB.

    Tenant DBs are always named tenant_<16-char-sha256-hex>
    (see routes/provisioning.py::_derive_db_name). The main DB never
    matches this pattern.
    """
    bind = op.get_bind()
    db_name = bind.engine.url.database or ""
    return db_name.startswith("tenant_") and len(db_name) == len("tenant_") + 16


def upgrade():
    if _is_tenant_database():
        return
    bind = op.get_bind()

    # 1. tenants.tenant_id: add, backfill, NOT NULL
    op.add_column("tenants", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    existing_rows = bind.execute(sa.text("SELECT license_id FROM tenants")).fetchall()
    for row in existing_rows:
        bind.execute(
            sa.text("UPDATE tenants SET tenant_id = :tid WHERE license_id = :lid"),
            {"tid": str(uuid.uuid4()), "lid": row.license_id},
        )
    op.alter_column("tenants", "tenant_id", nullable=False)

    # 2. catalog_snapshots.tenant_id: add + backfill BEFORE tearing down license_id
    op.add_column("catalog_snapshots", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(sa.text("""
        UPDATE catalog_snapshots cs
        SET tenant_id = t.tenant_id
        FROM tenants t
        WHERE cs.license_id = t.license_id
    """))
    op.alter_column("catalog_snapshots", "tenant_id", nullable=False)

    # 3. Remove the FK that depends on tenants_pkey — MUST come before the PK swap
    op.drop_index("ix_catalog_snapshots_license_id", table_name="catalog_snapshots")
    op.drop_constraint("catalog_snapshots_license_id_fkey", "catalog_snapshots", type_="foreignkey")
    op.drop_column("catalog_snapshots", "license_id")

    # 4. Now the tenants PK swap succeeds — nothing depends on the old PK
    op.drop_constraint("tenants_pkey", "tenants", type_="primary")
    op.create_primary_key("tenants_pkey", "tenants", ["tenant_id"])

    # 5. license_id → nullable + explicit unique + index
    op.alter_column("tenants", "license_id", nullable=True)
    op.create_unique_constraint("uq_tenants_license_id", "tenants", ["license_id"])
    op.create_index("ix_tenants_license_id", "tenants", ["license_id"])

    # 6. Rebuild the catalog_snapshots FK against the new target
    op.create_foreign_key(
        "catalog_snapshots_tenant_id_fkey",
        "catalog_snapshots", "tenants", ["tenant_id"], ["tenant_id"],
    )
    op.create_index("ix_catalog_snapshots_tenant_id", "catalog_snapshots", ["tenant_id"])

    # 7. plan column
    op.add_column("tenants", sa.Column("plan", sa.String(length=20), nullable=False, server_default="free"))
    op.create_index("ix_tenants_plan", "tenants", ["plan"])
    op.execute(sa.text("UPDATE tenants SET plan = 'pro' WHERE license_id IS NOT NULL"))
    op.alter_column("tenants", "plan", server_default=None)

def downgrade():
    if _is_tenant_database():
        return

    # ── catalog_snapshots: restore license_id, drop tenant_id ──────────────
    op.add_column(
        "catalog_snapshots", sa.Column("license_id", sa.String(length=128), nullable=True)
    )
    op.execute(
        sa.text(
            """
            UPDATE catalog_snapshots cs
            SET license_id = t.license_id
            FROM tenants t
            WHERE cs.tenant_id = t.tenant_id
            """
        )
    )
    # See module docstring caveat: fails here if any snapshot belongs to a
    # tenant whose license_id is NULL (a free tenant from a later phase).
    op.alter_column("catalog_snapshots", "license_id", nullable=False)
    op.create_foreign_key(
        "catalog_snapshots_license_id_fkey",
        "catalog_snapshots",
        "tenants",
        ["license_id"],
        ["license_id"],
    )
    op.create_index("ix_catalog_snapshots_license_id", "catalog_snapshots", ["license_id"])

    op.drop_index("ix_catalog_snapshots_tenant_id", table_name="catalog_snapshots")
    op.drop_constraint(
        "catalog_snapshots_tenant_id_fkey", "catalog_snapshots", type_="foreignkey"
    )
    op.drop_column("catalog_snapshots", "tenant_id")

    # ── tenants: drop plan ───────────────────────────────────────────────
    op.drop_index("ix_tenants_plan", table_name="tenants")
    op.drop_column("tenants", "plan")

    # ── tenants: restore license_id as PK, drop tenant_id ───────────────
    op.drop_index("ix_tenants_license_id", table_name="tenants")
    op.drop_constraint("uq_tenants_license_id", "tenants", type_="unique")
    op.alter_column("tenants", "license_id", nullable=False)

    op.drop_constraint("tenants_pkey", "tenants", type_="primary")
    op.create_primary_key("tenants_pkey", "tenants", ["license_id"])

    op.drop_column("tenants", "tenant_id")
