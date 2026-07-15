"""add_catalog_snapshots_table

Revision ID: be0ba327ea80
Revises: 83e44ad55734
Create Date: 2026-07-15 13:16:49.058770

Adds catalog_snapshots to the control-plane DB. Each row is one full
catalog payload pushed by the WordPress plugin (see handlers/catalog_push.py
route /tenant-catalog-push). Table lives in the MAIN miraq_chat DB
alongside `tenants`, NOT in per-tenant DBs — no per-tenant migration walk
is required for this change.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'be0ba327ea80'
down_revision = '83e44ad55734'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'catalog_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'license_id',
            sa.String(length=128),
            sa.ForeignKey('tenants.license_id'),
            nullable=False,
        ),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('product_count', sa.Integer(), nullable=True),
        sa.Column('payload_bytes', sa.Integer(), nullable=True),
        sa.Column(
            'received_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=True,
        ),
    )
    op.create_index(
        'ix_catalog_snapshots_license_id',
        'catalog_snapshots',
        ['license_id'],
    )
    op.create_index(
        'ix_catalog_snapshots_received_at',
        'catalog_snapshots',
        ['received_at'],
    )


def downgrade():
    op.drop_index('ix_catalog_snapshots_received_at', table_name='catalog_snapshots')
    op.drop_index('ix_catalog_snapshots_license_id', table_name='catalog_snapshots')
    op.drop_table('catalog_snapshots')