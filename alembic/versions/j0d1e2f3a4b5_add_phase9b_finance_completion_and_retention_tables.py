"""add_phase9b_finance_completion_and_retention_tables

Revision ID: j0d1e2f3a4b5
Revises: i9c0d1e2f3a4
Create Date: 2026-10-25 21:55:00.000000

SentinelOS Phase 9B Finance & Billing Domain Completion Migration:
1. Creates persistent canonical service_plans catalog table and deterministically seeds canonical tariff packages.
2. Extends invoices table with dynamic GST columns (tax_treatment, cgst_amount, sgst_amount, igst_amount, place_of_supply, supplier_gstin, customer_gstin, pdf_storage_key).
3. Adds gateway_order_id to payments table for gateway order reconciliation.
4. Creates payment_webhook_events deduplication table for idempotent callback handling.
5. Creates billing_job_runs table for durable recurring billing and dunning scheduler logs.
6. Creates notification_outbox table for durable asynchronous customer notifications.
7. Safe, reversible, dialect-aware migration with idempotent execution guards.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'j0d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'i9c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CANONICAL_SEEDS = [
    ("PLAN-PRE-HERO-1.5G", "Hero Unlimited 1.5GB/Day (28d)", "RESIDENTIAL", "PREPAID", 299.00, "INR", 50, 42.00, 1.50, 28, "Prepaid - Daily Unlimited", 101, "Popular daily unlimited consumer pack with standard high-speed broadband and unlimited voice."),
    ("PLAN-PRE-SUPER-5G", "Super 5G 2GB/Day + Hotstar (28d)", "RESIDENTIAL", "PREPAID", 349.00, "INR", 100, 56.00, 2.00, 28, "Prepaid - 5G High Speed", 102, "High-speed 100 Mbps broadband bundle with daily 2GB quota and bundled OTT streaming."),
    ("PLAN-PRE-VALUE-VOICE", "Voice & Value 1GB/Day (28d)", "RESIDENTIAL", "PREPAID", 239.00, "INR", 30, 28.00, 1.00, 28, "Prepaid - Value Voice", 103, "Economical voice and light data pack for essential connectivity."),
    ("PLAN-PRE-TRUE-5G-56D", "True 5G Unlimited 2GB/Day (56d)", "RESIDENTIAL", "PREPAID", 579.00, "INR", 100, 112.00, 2.00, 56, "Prepaid - 5G High Speed", 104, "Extended 56-day high-speed package for medium-term prepaid subscribers."),
    ("PLAN-PRE-SAVER-84D", "Super Saver 1.5GB/Day (84d)", "RESIDENTIAL", "PREPAID", 719.00, "INR", 50, 126.00, 1.50, 84, "Prepaid - Long-term Bundle", 105, "Quarterly long-term value bundle delivering consistent daily bandwidth."),
    ("PLAN-PRE-ANNUAL-5G", "Annual 5G All-Access 2.5GB/Day (365d)", "RESIDENTIAL", "PREPAID", 2999.00, "INR", 150, 912.50, 2.50, 365, "Prepaid - Long-term Bundle", 106, "Full-year premium 365-day subscription with priority queueing and high data allowance."),
    ("PLAN-POST-INFINITY-75G", "Postpaid Individual Infinity 75GB", "RESIDENTIAL", "POSTPAID", 399.00, "INR", 100, 75.00, None, 30, "Postpaid - Individual Infinity", 201, "Entry-level postpaid plan with 75GB rollover high-speed data, unlimited voice, and 200GB data rollover."),
    ("PLAN-POST-FAMILY-2SIM", "Postpaid Family Plus 2-Connection", "RESIDENTIAL", "POSTPAID", 699.00, "INR", 150, 150.00, None, 30, "Postpaid - Family Share", 202, "Dual-connection household broadband plan sharing 150GB high-speed pool with individual usage controls."),
    ("PLAN-POST-ENTERPRISE-GIGA", "Enterprise Gigabit Dedicated Fiber", "ENTERPRISE", "POSTPAID", 3499.00, "INR", 1000, 0.00, None, 30, "Enterprise - Dedicated Leased", 301, "Symmetrical 1 Gbps enterprise internet leased line with 99.9% uptime SLA and dedicated static IPv4 /29 pool.")
]


def upgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    # 1. service_plans catalog
    if 'service_plans' not in existing_tables:
        op.create_table(
            'service_plans',
            sa.Column('id', postgresql.UUID(as_uuid=True) if bind.dialect.name == 'postgresql' else sa.CHAR(36), primary_key=True),
            sa.Column('plan_code', sa.String(50), nullable=False, unique=True),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('account_type', sa.String(32), nullable=False, server_default='RESIDENTIAL'),
            sa.Column('billing_type', sa.String(32), nullable=False, server_default='POSTPAID'),
            sa.Column('price', sa.Numeric(12, 2), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False, server_default='INR'),
            sa.Column('speed_mbps', sa.Integer(), nullable=False, server_default='50'),
            sa.Column('data_quota_gb', sa.Numeric(10, 2), nullable=False, server_default='0.00'),
            sa.Column('daily_quota_gb', sa.Numeric(10, 2), nullable=True),
            sa.Column('validity_days', sa.Integer(), nullable=False, server_default='28'),
            sa.Column('segment', sa.String(50), nullable=False),
            sa.Column('service_vlan', sa.Integer(), nullable=False, server_default='101'),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1' if bind.dialect.name == 'sqlite' else 'true')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_service_plans_plan_code', 'service_plans', ['plan_code'], unique=True)
        op.create_index('ix_service_plans_type_active', 'service_plans', ['billing_type', 'is_active'])

        # Deterministic idempotent seeding of canonical plans
        import uuid
        plan_table = sa.table(
            'service_plans',
            sa.column('id', postgresql.UUID(as_uuid=True) if bind.dialect.name == 'postgresql' else sa.CHAR(36)),
            sa.column('plan_code', sa.String),
            sa.column('name', sa.String),
            sa.column('account_type', sa.String),
            sa.column('billing_type', sa.String),
            sa.column('price', sa.Numeric),
            sa.column('currency', sa.String),
            sa.column('speed_mbps', sa.Integer),
            sa.column('data_quota_gb', sa.Numeric),
            sa.column('daily_quota_gb', sa.Numeric),
            sa.column('validity_days', sa.Integer),
            sa.column('segment', sa.String),
            sa.column('service_vlan', sa.Integer),
            sa.column('description', sa.Text),
            sa.column('is_active', sa.Boolean),
        )
        for seed in CANONICAL_SEEDS:
            uid_val = str(uuid.uuid4())
            op.execute(
                plan_table.insert().values(
                    id=uid_val,
                    plan_code=seed[0],
                    name=seed[1],
                    account_type=seed[2],
                    billing_type=seed[3],
                    price=seed[4],
                    currency=seed[5],
                    speed_mbps=seed[6],
                    data_quota_gb=seed[7],
                    daily_quota_gb=seed[8],
                    validity_days=seed[9],
                    segment=seed[10],
                    service_vlan=seed[11],
                    description=seed[12],
                    is_active=True
                )
            )

    # 2. invoices table extensions (Dynamic GST columns)
    if 'invoices' in existing_tables:
        cols = {c['name'] for c in inspector.get_columns('invoices')}
        if 'tax_treatment' not in cols:
            op.add_column('invoices', sa.Column('tax_treatment', sa.String(32), nullable=False, server_default='INTRA_STATE'))
        if 'cgst_amount' not in cols:
            op.add_column('invoices', sa.Column('cgst_amount', sa.Numeric(12, 2), nullable=False, server_default='0.00'))
        if 'sgst_amount' not in cols:
            op.add_column('invoices', sa.Column('sgst_amount', sa.Numeric(12, 2), nullable=False, server_default='0.00'))
        if 'igst_amount' not in cols:
            op.add_column('invoices', sa.Column('igst_amount', sa.Numeric(12, 2), nullable=False, server_default='0.00'))
        if 'place_of_supply' not in cols:
            op.add_column('invoices', sa.Column('place_of_supply', sa.String(100), nullable=True))
        if 'supplier_gstin' not in cols:
            op.add_column('invoices', sa.Column('supplier_gstin', sa.String(32), nullable=True))
        if 'customer_gstin' not in cols:
            op.add_column('invoices', sa.Column('customer_gstin', sa.String(32), nullable=True))
        if 'pdf_storage_key' not in cols:
            op.add_column('invoices', sa.Column('pdf_storage_key', sa.String(255), nullable=True))

    # 3. payments table gateway_order_id extension
    if 'payments' in existing_tables:
        p_cols = {c['name'] for c in inspector.get_columns('payments')}
        if 'gateway_order_id' not in p_cols:
            op.add_column('payments', sa.Column('gateway_order_id', sa.String(100), nullable=True))
            op.create_index('ix_payments_gateway_order_id', 'payments', ['gateway_order_id'], unique=True)

    # 4. payment_webhook_events deduplication table
    if 'payment_webhook_events' not in existing_tables:
        op.create_table(
            'payment_webhook_events',
            sa.Column('id', postgresql.UUID(as_uuid=True) if bind.dialect.name == 'postgresql' else sa.CHAR(36), primary_key=True),
            sa.Column('event_id', sa.String(100), nullable=False, unique=True),
            sa.Column('provider', sa.String(50), nullable=False, server_default='razorpay'),
            sa.Column('event_type', sa.String(100), nullable=False),
            sa.Column('payment_id', postgresql.UUID(as_uuid=True) if bind.dialect.name == 'postgresql' else sa.CHAR(36), sa.ForeignKey('payments.id', ondelete='SET NULL'), nullable=True),
            sa.Column('order_id', sa.String(100), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=False),
            sa.Column('status', sa.String(32), nullable=False, server_default='PROCESSED'),
            sa.Column('received_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('processed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_webhook_events_event_id', 'payment_webhook_events', ['event_id'], unique=True)
        op.create_index('ix_webhook_events_provider_type', 'payment_webhook_events', ['provider', 'event_type'])

    # 5. billing_job_runs durable execution table
    if 'billing_job_runs' not in existing_tables:
        op.create_table(
            'billing_job_runs',
            sa.Column('id', postgresql.UUID(as_uuid=True) if bind.dialect.name == 'postgresql' else sa.CHAR(36), primary_key=True),
            sa.Column('job_type', sa.String(50), nullable=False),
            sa.Column('market_id', sa.String(50), nullable=True),
            sa.Column('status', sa.String(32), nullable=False, server_default='RUNNING'),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('start_time', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('finish_time', sa.DateTime(), nullable=True),
            sa.Column('records_processed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('records_succeeded', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('records_failed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('records_skipped', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('error_details', sa.Text(), nullable=True),
            sa.Column('summary_metadata', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_billing_job_runs_type_status', 'billing_job_runs', ['job_type', 'status'])

    # 6. notification_outbox table
    if 'notification_outbox' not in existing_tables:
        op.create_table(
            'notification_outbox',
            sa.Column('id', postgresql.UUID(as_uuid=True) if bind.dialect.name == 'postgresql' else sa.CHAR(36), primary_key=True),
            sa.Column('market_id', sa.String(50), nullable=False, server_default='mumbai'),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('recipient', sa.String(100), nullable=False),
            sa.Column('channel', sa.String(32), nullable=False, server_default='SMS'),
            sa.Column('event_type', sa.String(50), nullable=False),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('status', sa.String(32), nullable=False, server_default='QUEUED'),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('idempotency_key', sa.String(100), nullable=False, unique=True),
            sa.Column('is_simulated', sa.Boolean(), nullable=False, server_default=sa.text('1' if bind.dialect.name == 'sqlite' else 'true')),
            sa.Column('scheduled_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('sent_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_notif_outbox_idempotency_key', 'notification_outbox', ['idempotency_key'], unique=True)
        op.create_index('ix_notif_outbox_status_sched', 'notification_outbox', ['status', 'scheduled_at'])


def downgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    if 'notification_outbox' in existing_tables:
        op.drop_table('notification_outbox')
    if 'billing_job_runs' in existing_tables:
        op.drop_table('billing_job_runs')
    if 'payment_webhook_events' in existing_tables:
        op.drop_table('payment_webhook_events')
    if 'service_plans' in existing_tables:
        op.drop_table('service_plans')
    if 'payments' in existing_tables:
        try:
            op.drop_index('ix_payments_gateway_order_id', table_name='payments')
        except Exception:
            pass
        try:
            op.drop_column('payments', 'gateway_order_id')
        except Exception:
            pass
    if 'invoices' in existing_tables:
        for c in ('pdf_storage_key', 'customer_gstin', 'supplier_gstin', 'place_of_supply', 'igst_amount', 'sgst_amount', 'cgst_amount', 'tax_treatment'):
            try:
                op.drop_column('invoices', c)
            except Exception:
                pass
