"""add tenant build/migration tracking columns

Revision ID: f7a8b9c0d1e2
Revises: a1b2c3d4e5f6
Create Date: 2026-06-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f7a8b9c0d1e2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tenants', sa.Column('last_build_error', sa.Text(), nullable=True))
    op.add_column('tenants', sa.Column('schema_migrated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('tenants', 'schema_migrated_at')
    op.drop_column('tenants', 'last_build_error')