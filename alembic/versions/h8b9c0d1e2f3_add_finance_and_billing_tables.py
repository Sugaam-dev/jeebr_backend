"""add_finance_and_billing_tables

Revision ID: h8b9c0d1e2f3
Revises: g7a8b9c0d1e2
Create Date: 2026-10-24 23:30:00.000000

SentinelOS Finance & Billing Domain Migration:
Creates authoritative billing accounts, service subscriptions, invoice line items,
payments, payment allocations, credit notes, refunds, double-entry general ledger
(journal entries and lines), and voucher redemption records.
Also extends invoices table with fixed-precision financial columns.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'h8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'g7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = set()
        existing_columns = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())
        existing_columns = {col['name'] for col in inspector.get_columns('invoices')} if 'invoices' in existing_tables else set()

    from sqlalchemy.dialects import postgresql
    guid_type = postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)

    # 1. Create 'billing_accounts'
    if 'billing_accounts' not in existing_tables:
        op.create_table(
            'billing_accounts',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('account_number', sa.String(length=50), nullable=False),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=True),
            sa.Column('crm_customer_id', guid_type, sa.ForeignKey('crm_customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('billing_type', sa.String(length=32), nullable=False, server_default='POSTPAID'),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('balance', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('credit_limit', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
            sa.Column('payment_term_days', sa.Integer(), nullable=False, server_default='15'),
            sa.Column('auto_pay_enabled', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("billing_type IN ('PREPAID', 'POSTPAID')", name='ck_billing_account_type'),
            sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')", name='ck_billing_account_status'),
        )
        op.create_index('ix_billing_accounts_account_number', 'billing_accounts', ['account_number'], unique=True)
        op.create_index('ix_billing_accounts_customer_id', 'billing_accounts', ['customer_id'])
        op.create_index('ix_billing_accounts_crm_customer_id', 'billing_accounts', ['crm_customer_id'])
        op.create_index('ix_billing_accounts_market_id', 'billing_accounts', ['market_id'])
        op.create_index('ix_billing_accounts_status', 'billing_accounts', ['status'])
        op.create_index('ix_billing_accounts_market_status', 'billing_accounts', ['market_id', 'status'])

    # 2. Create 'billing_subscriptions'
    if 'billing_subscriptions' not in existing_tables:
        op.create_table(
            'billing_subscriptions',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('subscription_code', sa.String(length=50), nullable=False),
            sa.Column('billing_account_id', guid_type, sa.ForeignKey('billing_accounts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('crm_customer_id', guid_type, sa.ForeignKey('crm_customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('plan_id', sa.String(length=50), nullable=False),
            sa.Column('plan_name', sa.String(length=100), nullable=False),
            sa.Column('billing_type', sa.String(length=32), nullable=False, server_default='POSTPAID'),
            sa.Column('billing_frequency', sa.String(length=32), nullable=False, server_default='MONTHLY'),
            sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
            sa.Column('start_date', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('current_period_start', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('current_period_end', sa.DateTime(), nullable=False),
            sa.Column('next_billing_date', sa.DateTime(), nullable=True),
            sa.Column('cancelled_at', sa.DateTime(), nullable=True),
            sa.Column('cancellation_reason', sa.Text(), nullable=True),
            sa.Column('provisioning_order_id', sa.Integer(), sa.ForeignKey('service_provisioning_orders.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("status IN ('PENDING', 'ACTIVE', 'SUSPENDED', 'CANCELLED', 'EXPIRED')", name='ck_billing_subscription_status'),
            sa.CheckConstraint("billing_type IN ('PREPAID', 'POSTPAID')", name='ck_billing_subscription_type'),
        )
        op.create_index('ix_billing_subscriptions_code', 'billing_subscriptions', ['subscription_code'], unique=True)
        op.create_index('ix_billing_subscriptions_account_id', 'billing_subscriptions', ['billing_account_id'])
        op.create_index('ix_billing_subscriptions_customer_id', 'billing_subscriptions', ['customer_id'])
        op.create_index('ix_billing_subscriptions_plan_id', 'billing_subscriptions', ['plan_id'])
        op.create_index('ix_billing_subscriptions_status', 'billing_subscriptions', ['status'])
        op.create_index('ix_billing_subscriptions_market_status', 'billing_subscriptions', ['market_id', 'status'])

    # 3. Extend 'invoices' table with financial fixed-precision columns
    invoice_columns = [
        ('billing_account_id', guid_type, sa.ForeignKey('billing_accounts.id', ondelete='SET NULL'), True),
        ('subscription_id', guid_type, sa.ForeignKey('billing_subscriptions.id', ondelete='SET NULL'), True),
        ('currency', sa.String(length=3), None, False, 'INR'),
        ('subtotal', sa.Numeric(precision=12, scale=2), None, False, '0.00'),
        ('tax_amount', sa.Numeric(precision=12, scale=2), None, False, '0.00'),
        ('discount_amount', sa.Numeric(precision=12, scale=2), None, False, '0.00'),
        ('total_amount', sa.Numeric(precision=12, scale=2), None, False, '0.00'),
        ('paid_amount', sa.Numeric(precision=12, scale=2), None, False, '0.00'),
        ('outstanding_amount', sa.Numeric(precision=12, scale=2), None, False, '0.00'),
        ('billing_period_start', sa.DateTime(), None, True),
        ('billing_period_end', sa.DateTime(), None, True),
        ('idempotency_key', sa.String(length=100), None, True),
        ('notes', sa.Text(), None, True),
    ]
    for col_info in invoice_columns:
        col_name = col_info[0]
        if col_name not in existing_columns:
            col_type = col_info[1]
            fk = col_info[2]
            nullable = col_info[3]
            default = col_info[4] if len(col_info) > 4 else None
            col_args = [col_type]
            if fk is not None:
                col_args.append(fk)
            op.add_column(
                'invoices',
                sa.Column(col_name, *col_args, nullable=nullable, server_default=default)
            )

    # Backfill legacy invoices where total_amount was initialized to 0.00
    try:
        op.execute(
            sa.text(
                "UPDATE invoices SET total_amount = billed_amount, subtotal = billed_amount, "
                "paid_amount = CASE WHEN status = 'Paid' THEN billed_amount ELSE 0.00 END, "
                "outstanding_amount = CASE WHEN status = 'Paid' THEN 0.00 ELSE billed_amount END "
                "WHERE (total_amount IS NULL OR total_amount = 0.00) AND billed_amount > 0"
            )
        )
    except Exception:
        pass

    # 4. Create 'invoice_line_items'
    if 'invoice_line_items' not in existing_tables:
        op.create_table(
            'invoice_line_items',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('invoices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('subscription_id', guid_type, sa.ForeignKey('billing_subscriptions.id', ondelete='SET NULL'), nullable=True),
            sa.Column('description', sa.String(length=255), nullable=False),
            sa.Column('quantity', sa.Numeric(precision=10, scale=2), nullable=False, server_default='1.00'),
            sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=False, server_default='18.00'),
            sa.Column('tax_amount', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('discount_amount', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('total_amount', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_invoice_line_items_invoice_id', 'invoice_line_items', ['invoice_id'])
        op.create_index('ix_invoice_line_items_subscription_id', 'invoice_line_items', ['subscription_id'])

    # 5. Create 'payments'
    if 'payments' not in existing_tables:
        op.create_table(
            'payments',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('payment_reference', sa.String(length=50), nullable=False),
            sa.Column('billing_account_id', guid_type, sa.ForeignKey('billing_accounts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('payment_method', sa.String(length=50), nullable=False, server_default='UPI'),
            sa.Column('provider_name', sa.String(length=100), nullable=False, server_default='SIMULATED_GATEWAY'),
            sa.Column('provider_transaction_id', sa.String(length=100), nullable=True),
            sa.Column('idempotency_key', sa.String(length=100), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
            sa.Column('allocated_amount', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('unallocated_amount', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('failure_reason', sa.Text(), nullable=True),
            sa.Column('is_simulated', sa.Boolean(), nullable=False, server_default=sa.text('1' if bind.dialect.name == 'sqlite' else 'true')),
            sa.Column('payment_date', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("status IN ('PENDING', 'COMPLETED', 'FAILED', 'REFUNDED', 'PARTIALLY_ALLOCATED', 'ALLOCATED')", name='ck_payment_status'),
        )
        op.create_index('ix_payments_reference', 'payments', ['payment_reference'], unique=True)
        op.create_index('ix_payments_idempotency', 'payments', ['idempotency_key'], unique=True)
        op.create_index('ix_payments_provider_tx', 'payments', ['provider_transaction_id'], unique=True)
        op.create_index('ix_payments_account_id', 'payments', ['billing_account_id'])
        op.create_index('ix_payments_status', 'payments', ['status'])
        op.create_index('ix_payments_market_status', 'payments', ['market_id', 'status'])

    # 6. Create 'payment_allocations'
    if 'payment_allocations' not in existing_tables:
        op.create_table(
            'payment_allocations',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('payment_id', guid_type, sa.ForeignKey('payments.id', ondelete='CASCADE'), nullable=False),
            sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('invoices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('allocated_amount', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('allocated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_payment_allocations_payment_id', 'payment_allocations', ['payment_id'])
        op.create_index('ix_payment_allocations_invoice_id', 'payment_allocations', ['invoice_id'])

    # 7. Create 'credit_notes'
    if 'credit_notes' not in existing_tables:
        op.create_table(
            'credit_notes',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('credit_note_code', sa.String(length=50), nullable=False),
            sa.Column('billing_account_id', guid_type, sa.ForeignKey('billing_accounts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('invoices.id', ondelete='SET NULL'), nullable=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('reason', sa.String(length=255), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='ISSUED'),
            sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_credit_notes_code', 'credit_notes', ['credit_note_code'], unique=True)
        op.create_index('ix_credit_notes_account_id', 'credit_notes', ['billing_account_id'])

    # 8. Create 'refund_records'
    if 'refund_records' not in existing_tables:
        op.create_table(
            'refund_records',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('refund_code', sa.String(length=50), nullable=False),
            sa.Column('payment_id', guid_type, sa.ForeignKey('payments.id', ondelete='CASCADE'), nullable=False),
            sa.Column('billing_account_id', guid_type, sa.ForeignKey('billing_accounts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
            sa.Column('reason', sa.String(length=255), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='COMPLETED'),
            sa.Column('provider_refund_id', sa.String(length=100), nullable=True),
            sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_refund_records_code', 'refund_records', ['refund_code'], unique=True)
        op.create_index('ix_refund_records_payment_id', 'refund_records', ['payment_id'])

    # 9. Create 'journal_entries'
    if 'journal_entries' not in existing_tables:
        op.create_table(
            'journal_entries',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('entry_number', sa.String(length=50), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('reference_type', sa.String(length=50), nullable=False),
            sa.Column('reference_id', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=False),
            sa.Column('posted_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('is_reversed', sa.Boolean(), nullable=False, server_default=sa.text('0' if bind.dialect.name == 'sqlite' else 'false')),
            sa.Column('reversal_entry_id', guid_type, sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_journal_entries_entry_number', 'journal_entries', ['entry_number'], unique=True)
        op.create_index('ix_journal_entries_reference', 'journal_entries', ['reference_type', 'reference_id'])
        op.create_index('ix_journal_entries_market_id', 'journal_entries', ['market_id'])

    # 10. Create 'journal_lines'
    if 'journal_lines' not in existing_tables:
        op.create_table(
            'journal_lines',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('entry_id', guid_type, sa.ForeignKey('journal_entries.id', ondelete='CASCADE'), nullable=False),
            sa.Column('account_code', sa.String(length=20), nullable=False),
            sa.Column('account_name', sa.String(length=100), nullable=False),
            sa.Column('debit', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('credit', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
        )
        op.create_index('ix_journal_lines_entry_id', 'journal_lines', ['entry_id'])
        op.create_index('ix_journal_lines_account_code', 'journal_lines', ['account_code'])

    # 11. Create 'voucher_redemptions'
    if 'voucher_redemptions' not in existing_tables:
        op.create_table(
            'voucher_redemptions',
            sa.Column('id', guid_type, primary_key=True, nullable=False),
            sa.Column('voucher_code', sa.String(length=64), nullable=False),
            sa.Column('voucher_type', sa.String(length=50), nullable=False),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('crm_customer_id', guid_type, sa.ForeignKey('crm_customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
            sa.Column('benefit_details', sa.JSON(), nullable=True),
            sa.Column('financial_credit_amount', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0.00'),
            sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('invoices.id', ondelete='SET NULL'), nullable=True),
            sa.Column('provisioning_order_id', sa.Integer(), sa.ForeignKey('service_provisioning_orders.id', ondelete='SET NULL'), nullable=True),
            sa.Column('idempotency_key', sa.String(length=100), nullable=False),
            sa.Column('redeemed_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('redeemed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('failure_reason', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("voucher_type IN ('SPEED_BOOST', 'DATA_TOPUP', 'BILL_DISCOUNT', 'OTT_PASS')", name='ck_voucher_redemption_type'),
            sa.CheckConstraint("status IN ('PENDING', 'FULFILLED', 'FAILED', 'RECONCILIATION_REQUIRED')", name='ck_voucher_redemption_status'),
        )
        op.create_index('ix_voucher_redemptions_idempotency', 'voucher_redemptions', ['idempotency_key'], unique=True)
        op.create_index('ix_voucher_redemptions_code', 'voucher_redemptions', ['voucher_code'])
        op.create_index('ix_voucher_redemptions_cust_code', 'voucher_redemptions', ['customer_id', 'voucher_code'])
        op.create_index('ix_voucher_redemptions_status', 'voucher_redemptions', ['status'])


def downgrade() -> None:
    op.drop_table('voucher_redemptions')
    op.drop_table('journal_lines')
    op.drop_table('journal_entries')
    op.drop_table('refund_records')
    op.drop_table('credit_notes')
    op.drop_table('payment_allocations')
    op.drop_table('payments')
    op.drop_table('invoice_line_items')
    op.drop_table('billing_subscriptions')
    op.drop_table('billing_accounts')
