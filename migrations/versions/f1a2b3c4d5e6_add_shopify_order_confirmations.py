"""add shopify_order_confirmations

Revision ID: f1a2b3c4d5e6
Revises: a1b2c3d4e5f6
Create Date: 2026-09-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'shopify_order_confirmations',
        # Natural PK — one row per widget session that completed a Shopify
        # checkout. Populated by the orders/paid-filtered webhook receiver
        # (routes/shopify.py), read by the /chat/order-status polling route.
        sa.Column('session_id',       sa.String(255),             primary_key=True, nullable=False),
        sa.Column('order_id',         sa.String(255),             nullable=False),
        sa.Column('order_number',     sa.String(255),             nullable=True),
        sa.Column('delivered',        sa.Boolean(),                nullable=False, server_default='false'),
        sa.Column('created_at',       sa.DateTime(timezone=True), nullable=False,
                   server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('shopify_order_confirmations')