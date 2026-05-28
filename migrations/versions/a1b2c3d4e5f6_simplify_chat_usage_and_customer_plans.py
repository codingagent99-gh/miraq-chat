"""simplify chat_usage and customer_plans to store-level

Revision ID: a1b2c3d4e5f6
Revises: e5ef94059f68
Create Date: 2026-05-28 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'e5ef94059f68'
branch_labels = None
depends_on = None


def upgrade():
    # Drop old tables entirely — schema is incompatible, data is not worth migrating
    op.drop_table('chat_usage')
    op.drop_table('customer_plans')

    # Recreate chat_usage: one row per day, no tenant keys
    op.create_table(
        'chat_usage',
        sa.Column('usage_date',     sa.Date(),    primary_key=True, nullable=False),
        sa.Column('question_count', sa.Integer(), nullable=False, server_default='0'),
    )

    # Recreate customer_plans: single row (id=1) represents this store's plan
    op.create_table(
        'customer_plans',
        sa.Column('id',            sa.Integer(),                  primary_key=True, nullable=False),
        sa.Column('is_premium',    sa.Boolean(),                  nullable=False, server_default='false'),
        sa.Column('premium_since', sa.DateTime(timezone=True),    nullable=True),
        sa.Column('premium_until', sa.DateTime(timezone=True),    nullable=True),
        sa.Column('plan_ref',      sa.String(255),                nullable=True),
        sa.Column('updated_at',    sa.DateTime(timezone=True),    nullable=True),
    )

    # Seed the single plan row so CustomerPlan.get() always finds id=1
    op.execute(
        "INSERT INTO customer_plans (id, is_premium) VALUES (1, false)"
    )


def downgrade():
    op.drop_table('chat_usage')
    op.drop_table('customer_plans')

    # Restore old chat_usage with composite PK
    op.create_table(
        'chat_usage',
        sa.Column('customer_id',    sa.String(255), primary_key=True, nullable=False),
        sa.Column('platform',       sa.String(20),  primary_key=True, nullable=False),
        sa.Column('usage_date',     sa.Date(),      primary_key=True, nullable=False),
        sa.Column('question_count', sa.Integer(),   nullable=False, server_default='0'),
    )

    # Restore old customer_plans with composite PK
    op.create_table(
        'customer_plans',
        sa.Column('customer_id',   sa.String(255),             primary_key=True, nullable=False),
        sa.Column('platform',      sa.String(20),              primary_key=True, nullable=False),
        sa.Column('is_premium',    sa.Boolean(),               nullable=False, server_default='false'),
        sa.Column('premium_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('premium_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('plan_ref',      sa.String(255),             nullable=True),
        sa.Column('updated_at',    sa.DateTime(timezone=True), nullable=True),
    )