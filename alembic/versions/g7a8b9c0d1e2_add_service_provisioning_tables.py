"""add_service_provisioning_tables

Revision ID: g7a8b9c0d1e2
Revises: a7b8c9d0e1f2
Create Date: 2026-10-24 18:30:00.000000

Service Provisioning & Automated Service Delivery:
Adds authoritative service provisioning orders and audit state transition logs
while preserving existing CRM, topology, and ticketing tables.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'g7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = set()
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    from sqlalchemy.dialects import postgresql
    crm_cust_id_type = postgresql.UUID(as_uuid=True) if bind.dialect.name == "postgresql" else sa.CHAR(36)

    # 1. Create 'service_provisioning_orders' table
    if 'service_provisioning_orders' not in existing_tables:
        op.create_table(
            'service_provisioning_orders',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('order_code', sa.String(length=50), nullable=False),
            sa.Column('idempotency_key', sa.String(length=100), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('crm_customer_id', crm_cust_id_type, sa.ForeignKey('crm_customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('customer_name', sa.String(length=150), nullable=False),
            sa.Column('customer_code', sa.String(length=50), nullable=False),
            sa.Column('account_type', sa.String(length=50), nullable=False, server_default='RESIDENTIAL'),
            sa.Column('service_address', sa.String(length=255), nullable=True),
            sa.Column('locality', sa.String(length=100), nullable=False),
            sa.Column('serviceability_status', sa.String(length=50), nullable=False, server_default='SERVICEABLE'),
            sa.Column('latitude', sa.Float(), nullable=True),
            sa.Column('longitude', sa.Float(), nullable=True),
            sa.Column('plan_id', sa.String(length=50), nullable=False),
            sa.Column('plan_name', sa.String(length=100), nullable=False),
            sa.Column('plan_price', sa.Float(), nullable=False),
            sa.Column('speed_mbps', sa.Integer(), nullable=False, server_default='100'),
            sa.Column('data_quota_gb', sa.Float(), nullable=False, server_default='100.0'),
            sa.Column('validity_days', sa.Integer(), nullable=False, server_default='28'),
            sa.Column('assigned_device_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='SET NULL'), nullable=True),
            sa.Column('assigned_device_code', sa.String(length=50), nullable=True),
            sa.Column('assigned_port_id', sa.Integer(), sa.ForeignKey('olt_ports.id', ondelete='SET NULL'), nullable=True),
            sa.Column('olt_device_code', sa.String(length=50), nullable=True),
            sa.Column('service_vlan', sa.Integer(), nullable=True),
            sa.Column('radius_username', sa.String(length=100), nullable=True),
            sa.Column('status', sa.String(length=50), nullable=False, server_default='REQUESTED'),
            sa.Column('customer_lifecycle_status', sa.String(length=50), nullable=False, server_default='ACTIVE'),
            sa.Column('subscription_status', sa.String(length=50), nullable=False, server_default='PENDING'),
            sa.Column('execution_mode', sa.String(length=50), nullable=False, server_default='SIMULATED'),
            sa.Column('provider_name', sa.String(length=100), nullable=False, server_default='SyntheticProvisioningProvider'),
            sa.Column('telemetry_source', sa.String(length=100), nullable=False, server_default='Synthetic / Demo Telemetry'),
            sa.Column('failure_code', sa.String(length=100), nullable=True),
            sa.Column('failure_reason', sa.Text(), nullable=True),
            sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('max_retries', sa.Integer(), nullable=False, server_default='3'),
            sa.Column('requested_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('requested_by_email', sa.String(length=150), nullable=True),
            sa.Column('approved_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('approved_by_email', sa.String(length=150), nullable=True),
            sa.Column('approved_at', sa.DateTime(), nullable=True),
            sa.Column('provisioned_at', sa.DateTime(), nullable=True),
            sa.Column('cancelled_at', sa.DateTime(), nullable=True),
            sa.Column('cancellation_reason', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_provisioning_orders_code', 'service_provisioning_orders', ['order_code'], unique=True)
        op.create_index('ix_provisioning_orders_idempotency', 'service_provisioning_orders', ['idempotency_key'], unique=True)
        op.create_index('ix_provisioning_orders_market', 'service_provisioning_orders', ['market_id'])
        op.create_index('ix_provisioning_orders_customer', 'service_provisioning_orders', ['customer_id'])
        op.create_index('ix_provisioning_orders_status', 'service_provisioning_orders', ['status'])

    # 2. Create 'provisioning_logs' table
    if 'provisioning_logs' not in existing_tables:
        op.create_table(
            'provisioning_logs',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('order_id', sa.Integer(), sa.ForeignKey('service_provisioning_orders.id', ondelete='CASCADE'), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('from_status', sa.String(length=50), nullable=False),
            sa.Column('to_status', sa.String(length=50), nullable=False),
            sa.Column('action', sa.String(length=100), nullable=False),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('actor_email', sa.String(length=150), nullable=True),
            sa.Column('actor_role', sa.String(length=50), nullable=True),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('execution_mode', sa.String(length=50), nullable=False, server_default='SIMULATED'),
            sa.Column('details', sa.JSON(), nullable=True),
            sa.Column('timestamp', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_provisioning_logs_order', 'provisioning_logs', ['order_id'])
        op.create_index('ix_provisioning_logs_market', 'provisioning_logs', ['market_id'])
        op.create_index('ix_provisioning_logs_timestamp', 'provisioning_logs', ['timestamp'])


def downgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = {'provisioning_logs', 'service_provisioning_orders'}
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    if 'provisioning_logs' in existing_tables:
        op.drop_table('provisioning_logs')
    if 'service_provisioning_orders' in existing_tables:
        op.drop_table('service_provisioning_orders')
