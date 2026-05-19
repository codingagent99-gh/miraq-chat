"""change customer_id to bigint for shopify id support

Revision ID: a1b2c3d4e5f6
Revises: 
Create Date: 2026-05-19

Why: Shopify customer IDs are 64-bit integers (e.g. 10512323150122),
     which exceed PostgreSQL INTEGER's max of 2,147,483,647.
     WooCommerce used small sequential IDs so this never surfaced before.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = 'a1b2c3d4e5f6'
down_revision = None   # ← set this to your current latest revision ID
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'conversations',
        'customer_id',
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade():
    # WARNING: this will truncate any customer_id > 2,147,483,647
    op.alter_column(
        'conversations',
        'customer_id',
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )