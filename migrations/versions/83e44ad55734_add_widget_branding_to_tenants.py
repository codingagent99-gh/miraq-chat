"""add widget branding to tenants

Revision ID: 83e44ad55734
Revises: 11a150d3635e
Create Date: 2026-07-15 03:24:47.438262

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '83e44ad55734'
down_revision = '11a150d3635e'
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
    op.add_column("tenants", sa.Column("widget_logo_url", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("widget_header_text", sa.Text(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("widget_config_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    if _is_tenant_database():
        return
    op.drop_column("tenants", "widget_config_fetched_at")
    op.drop_column("tenants", "widget_header_text")
    op.drop_column("tenants", "widget_logo_url")