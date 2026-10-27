"""add_tariffs_and_vouchers

Revision ID: n4e5f6a7b8c9
Revises: m3f4a5b6c7d8
Create Date: 2026-10-26 22:30:00.000000

SentinelOS Tariff Plans & Voucher Management Migration:
1. Creates tariff_plans for versioned pricing, charge breakdowns, GST rates, and effective dates.
2. Creates vouchers for promotional, courtesy, and retention coupon campaigns.
3. Adds voucher_id foreign key linkage to voucher_redemptions.
4. Adds concurrency protection index on inventory_devices assigned active orders.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'n4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'm3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    is_offline_or_mock = getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__

    if is_offline_or_mock:
        existing_tables = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    guid_type = postgresql.UUID(as_uuid=True) if bind and bind.dialect.name == 'postgresql' else sa.CHAR(32)

    # 1. tariff_plans
    if 'tariff_plans' not in existing_tables:
        op.create_table(
            'tariff_plans',
            sa.Column('id', guid_type, primary_key=True),
            sa.Column('tariff_code', sa.String(50), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('service_plan_code', sa.String(50), sa.ForeignKey('service_plans.plan_code', ondelete='RESTRICT'), nullable=False),
            sa.Column('market_id', sa.String(32), server_default='ALL', nullable=False),
            sa.Column('version', sa.Integer(), server_default='1', nullable=False),
            sa.Column('billing_type', sa.String(32), server_default='POSTPAID', nullable=False),
            sa.Column('billing_frequency', sa.String(32), server_default='MONTHLY', nullable=False),
            sa.Column('billing_timing', sa.String(32), server_default='IN_ADVANCE', nullable=False),
            sa.Column('currency', sa.String(3), server_default='INR', nullable=False),
            sa.Column('base_recurring_charge', sa.Numeric(12, 2), server_default='0.00', nullable=False),
            sa.Column('setup_fee', sa.Numeric(12, 2), server_default='0.00', nullable=False),
            sa.Column('usage_rate_per_gb', sa.Numeric(12, 2), server_default='0.00', nullable=False),
            sa.Column('tax_rate_percent', sa.Numeric(5, 2), server_default='18.00', nullable=False),
            sa.Column('discount_percentage', sa.Numeric(5, 2), server_default='0.00', nullable=False),
            sa.Column('effective_from', sa.DateTime(), nullable=False),
            sa.Column('effective_to', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(32), server_default='ACTIVE', nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE', 'ARCHIVED')", name='chk_tariff_status'),
            sa.CheckConstraint("billing_type IN ('PREPAID', 'POSTPAID')", name='chk_tariff_billing_type'),
            sa.CheckConstraint("billing_timing IN ('IN_ADVANCE', 'IN_ARREARS')", name='chk_tariff_billing_timing'),
            sa.CheckConstraint("base_recurring_charge >= 0", name='chk_tariff_base_charge_positive'),
            sa.CheckConstraint("setup_fee >= 0", name='chk_tariff_setup_fee_positive'),
            sa.CheckConstraint("usage_rate_per_gb >= 0", name='chk_tariff_usage_rate_positive'),
            sa.CheckConstraint("tax_rate_percent >= 0 AND tax_rate_percent <= 100", name='chk_tariff_tax_rate_bounds'),
            sa.CheckConstraint("discount_percentage >= 0 AND discount_percentage <= 100", name='chk_tariff_discount_bounds'),
        )
        op.create_index('ix_tariff_plans_code', 'tariff_plans', ['tariff_code'], unique=True)
        op.create_index('ix_tariff_plans_code_market', 'tariff_plans', ['tariff_code', 'market_id'])
        op.create_index('ix_tariff_plans_lookup', 'tariff_plans', ['service_plan_code', 'market_id', 'status'])
        op.create_index('ix_tariff_plans_status', 'tariff_plans', ['status'])

    # 2. vouchers
    if 'vouchers' not in existing_tables:
        op.create_table(
            'vouchers',
            sa.Column('id', guid_type, primary_key=True),
            sa.Column('voucher_code', sa.String(64), nullable=False),
            sa.Column('campaign_name', sa.String(100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('voucher_type', sa.String(50), nullable=False),
            sa.Column('discount_amount', sa.Numeric(12, 2), server_default='0.00', nullable=False),
            sa.Column('discount_percent', sa.Numeric(5, 2), server_default='0.00', nullable=False),
            sa.Column('max_discount_cap', sa.Numeric(12, 2), nullable=True),
            sa.Column('min_invoice_amount', sa.Numeric(12, 2), server_default='0.00', nullable=False),
            sa.Column('benefit_payload', sa.JSON(), nullable=True),
            sa.Column('max_redemptions', sa.Integer(), server_default='1', nullable=False),
            sa.Column('current_redemptions', sa.Integer(), server_default='0', nullable=False),
            sa.Column('per_customer_limit', sa.Integer(), server_default='1', nullable=False),
            sa.Column('valid_from', sa.DateTime(), nullable=False),
            sa.Column('valid_until', sa.DateTime(), nullable=False),
            sa.Column('market_id', sa.String(50), server_default='ALL', nullable=False),
            sa.Column('applicable_plan_id', sa.String(50), nullable=True),
            sa.Column('status', sa.String(32), server_default='ACTIVE', nullable=False),
            sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.CheckConstraint("status IN ('ACTIVE', 'PAUSED', 'EXPIRED', 'EXHAUSTED', 'REVOKED')", name='chk_voucher_status'),
            sa.CheckConstraint("voucher_type IN ('BILL_DISCOUNT', 'PERCENT_DISCOUNT', 'SPEED_BOOST', 'DATA_TOPUP', 'OTT_PASS')", name='chk_voucher_type'),
            sa.CheckConstraint("max_redemptions >= 1", name='chk_voucher_max_redemptions'),
            sa.CheckConstraint("current_redemptions >= 0", name='chk_voucher_current_redemptions_positive'),
            sa.CheckConstraint("current_redemptions <= max_redemptions", name='chk_voucher_redemptions_within_bounds'),
            sa.CheckConstraint("per_customer_limit >= 1", name='chk_voucher_per_customer_limit'),
            sa.CheckConstraint("discount_amount >= 0", name='chk_voucher_discount_amount_positive'),
            sa.CheckConstraint("discount_percent >= 0 AND discount_percent <= 100", name='chk_voucher_discount_percent_bounds'),
        )
        op.create_index('ix_vouchers_code', 'vouchers', ['voucher_code'], unique=True)
        op.create_index('ix_vouchers_market_status', 'vouchers', ['market_id', 'status'])
        op.create_index('ix_vouchers_validity', 'vouchers', ['valid_from', 'valid_until', 'status'])

    # 3. Add voucher_id to voucher_redemptions
    if is_offline_or_mock:
        op.add_column('voucher_redemptions', sa.Column('voucher_id', guid_type, sa.ForeignKey('vouchers.id', ondelete='SET NULL'), nullable=True))
        op.create_index('ix_voucher_redemptions_voucher_id', 'voucher_redemptions', ['voucher_id'])
    else:
        vr_columns = [c['name'] for c in inspector.get_columns('voucher_redemptions')]
        if 'voucher_id' not in vr_columns:
            fk = sa.ForeignKey('vouchers.id', name='fk_voucher_redemptions_voucher_id', ondelete='SET NULL')
            if bind and bind.dialect.name == 'sqlite':
                with op.batch_alter_table('voucher_redemptions') as batch_op:
                    batch_op.add_column(sa.Column('voucher_id', guid_type, fk, nullable=True))
                    batch_op.create_index('ix_voucher_redemptions_voucher_id', ['voucher_id'])
            else:
                op.add_column('voucher_redemptions', sa.Column('voucher_id', guid_type, fk, nullable=True))
                op.create_index('ix_voucher_redemptions_voucher_id', 'voucher_redemptions', ['voucher_id'])

    # 4. Partial index on inventory_devices assigned active orders
    if is_offline_or_mock:
        op.create_index(
            'ix_inventory_devices_assigned_order_active',
            'inventory_devices',
            ['assigned_order_id'],
            unique=True,
            sqlite_where=sa.text("assigned_order_id IS NOT NULL AND status IN ('RESERVED', 'INSTALLED')"),
            postgresql_where=sa.text("assigned_order_id IS NOT NULL AND status IN ('RESERVED', 'INSTALLED')")
        )
    else:
        existing_indexes = [idx['name'] for idx in inspector.get_indexes('inventory_devices')]
        if 'ix_inventory_devices_assigned_order_active' not in existing_indexes:
            op.create_index(
                'ix_inventory_devices_assigned_order_active',
                'inventory_devices',
                ['assigned_order_id'],
                unique=True,
                sqlite_where=sa.text("assigned_order_id IS NOT NULL AND status IN ('RESERVED', 'INSTALLED')"),
                postgresql_where=sa.text("assigned_order_id IS NOT NULL AND status IN ('RESERVED', 'INSTALLED')")
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = (bind.dialect.name == 'sqlite') if bind else False

    # 1. Drop partial index
    try:
        op.drop_index('ix_inventory_devices_assigned_order_active', table_name='inventory_devices')
    except Exception:
        pass

    # 2. Drop voucher_id from voucher_redemptions using batch mode for SQLite
    if is_sqlite:
        with op.batch_alter_table('voucher_redemptions') as batch_op:
            try:
                batch_op.drop_index('ix_voucher_redemptions_voucher_id')
            except Exception:
                pass
            batch_op.drop_column('voucher_id')
    else:
        try:
            op.drop_index('ix_voucher_redemptions_voucher_id', table_name='voucher_redemptions')
        except Exception:
            pass
        op.drop_column('voucher_redemptions', 'voucher_id')

    # 3. Drop tables
    op.drop_table('vouchers')
    op.drop_table('tariff_plans')
