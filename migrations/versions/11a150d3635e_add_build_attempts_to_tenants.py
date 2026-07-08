"""add build_attempts to tenants (main control-plane db only)

Revision ID: 11a150d3635e
Revises: 
Create Date: 2026-07-08 16:53:23.770082
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '11a150d3635e'
down_revision = None
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
    op.add_column(
        "tenants",
        sa.Column("build_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("tenants", "build_attempts", server_default=None)


def downgrade():
    if _is_tenant_database():
        return
    op.drop_column("tenants", "build_attempts")