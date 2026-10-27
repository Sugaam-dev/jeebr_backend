"""add_provisioning_order_id_index

Revision ID: k1e2f3a4b5c6
Revises: j0d1e2f3a4b5
Create Date: 2026-10-26 14:10:00.000000

SentinelOS Gate 2 Provisioning-to-Billing Workflow Integrity Migration:
1. Adds unique index on billing_subscriptions(provisioning_order_id) where provisioning_order_id IS NOT NULL.
2. Ensures database-level idempotency and prevents duplicate subscription creation for the same provisioning order.
3. Safe, dialect-aware, and idempotent execution guards for SQLite and PostgreSQL.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'k1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'j0d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        op.create_index(
            'ix_billing_subscriptions_provisioning_order_id',
            'billing_subscriptions',
            ['provisioning_order_id'],
            unique=True,
            sqlite_where=sa.text('provisioning_order_id IS NOT NULL'),
            postgresql_where=sa.text('provisioning_order_id IS NOT NULL')
        )
        return

    insp = sa.inspect(bind)
    indexes = [idx['name'] for idx in insp.get_indexes('billing_subscriptions')]
    if 'ix_billing_subscriptions_provisioning_order_id' not in indexes:
        op.create_index(
            'ix_billing_subscriptions_provisioning_order_id',
            'billing_subscriptions',
            ['provisioning_order_id'],
            unique=True,
            sqlite_where=sa.text('provisioning_order_id IS NOT NULL'),
            postgresql_where=sa.text('provisioning_order_id IS NOT NULL')
        )


def downgrade():
    op.drop_index('ix_billing_subscriptions_provisioning_order_id', table_name='billing_subscriptions')
