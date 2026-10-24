"""add_phase7b_network_topology_tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-15 13:16:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Create 'network_devices' table
    if 'network_devices' not in existing_tables:
        op.create_table(
            'network_devices',
            sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('device_type', sa.String(length=50), nullable=False),
            sa.Column('device_name', sa.String(length=100), nullable=False),
            sa.Column('device_code', sa.String(length=50), nullable=False, unique=True),
            sa.Column('serial_number', sa.String(length=100), nullable=True),
            sa.Column('vendor', sa.String(length=100), nullable=True, server_default='Synthetic Vendor'),
            sa.Column('model', sa.String(length=100), nullable=True, server_default='Standard'),
            sa.Column('status', sa.String(length=50), nullable=True, server_default='HEALTHY'),
            sa.Column('latitude', sa.Float(), nullable=True),
            sa.Column('longitude', sa.Float(), nullable=True),
            sa.Column('area', sa.String(length=100), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('parent_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='SET NULL'), nullable=True),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('node_id', sa.Integer(), sa.ForeignKey('nodes.id', ondelete='SET NULL'), nullable=True),
            sa.Column('total_ports', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('active_ports', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('optical_rx_dbm', sa.Float(), nullable=True),
            sa.Column('optical_tx_dbm', sa.Float(), nullable=True),
            sa.Column('temperature_c', sa.Float(), nullable=True),
            sa.Column('uptime_seconds', sa.Integer(), nullable=True, server_default='86400'),
            sa.Column('last_boot_at', sa.DateTime(), nullable=True),
            sa.Column('cpu_utilization_pct', sa.Float(), nullable=True),
            sa.Column('memory_utilization_pct', sa.Float(), nullable=True),
            sa.Column('memory_total_gb', sa.Float(), nullable=True, server_default='16.0'),
            sa.Column('memory_used_gb', sa.Float(), nullable=True),
            sa.Column('memory_available_gb', sa.Float(), nullable=True),
            sa.Column('health_reasons', sa.Text(), nullable=True),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        )
        op.create_index('ix_network_devices_id', 'network_devices', ['id'])
        op.create_index('ix_network_devices_market_id', 'network_devices', ['market_id'])
        op.create_index('ix_network_devices_device_type', 'network_devices', ['device_type'])
        op.create_index('ix_network_devices_device_name', 'network_devices', ['device_name'])
        op.create_index('ix_network_devices_device_code', 'network_devices', ['device_code'])
        op.create_index('ix_network_devices_status', 'network_devices', ['status'])
        op.create_index('ix_network_devices_parent_id', 'network_devices', ['parent_id'])
        op.create_index('ix_network_devices_customer_id', 'network_devices', ['customer_id'])

    # 2. Create 'olt_ports' table
    if 'olt_ports' not in existing_tables:
        op.create_table(
            'olt_ports',
            sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
            sa.Column('device_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('port_number', sa.String(length=50), nullable=False),
            sa.Column('technology', sa.String(length=50), nullable=True, server_default='GPON'),
            sa.Column('status', sa.String(length=50), nullable=True, server_default='HEALTHY'),
            sa.Column('connected_clients', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('capacity', sa.Integer(), nullable=True, server_default='64'),
            sa.Column('utilization_pct', sa.Float(), nullable=True, server_default='0.0'),
            sa.Column('tx_power_dbm', sa.Float(), nullable=True, server_default='3.5'),
            sa.Column('rx_power_dbm', sa.Float(), nullable=True, server_default='-20.0'),
            sa.Column('last_updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        )
        op.create_index('ix_olt_ports_id', 'olt_ports', ['id'])
        op.create_index('ix_olt_ports_device_id', 'olt_ports', ['device_id'])

    # 3. Create 'network_links' table
    if 'network_links' not in existing_tables:
        op.create_table(
            'network_links',
            sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('source_device_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('target_device_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('link_type', sa.String(length=50), nullable=True, server_default='LOGICAL'),
            sa.Column('status', sa.String(length=50), nullable=True, server_default='HEALTHY'),
            sa.Column('distance_meters', sa.Float(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        )
        op.create_index('ix_network_links_id', 'network_links', ['id'])
        op.create_index('ix_network_links_market_id', 'network_links', ['market_id'])
        op.create_index('ix_network_links_source_device_id', 'network_links', ['source_device_id'])
        op.create_index('ix_network_links_target_device_id', 'network_links', ['target_device_id'])

    # 4. Create 'network_alarms' table
    if 'network_alarms' not in existing_tables:
        op.create_table(
            'network_alarms',
            sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
            sa.Column('alarm_code', sa.String(length=100), nullable=False, unique=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('device_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('severity', sa.String(length=50), nullable=True, server_default='WARNING'),
            sa.Column('code', sa.String(length=100), nullable=False),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('status', sa.String(length=50), nullable=True, server_default='ACTIVE'),
            sa.Column('first_seen_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
            sa.Column('cleared_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_network_alarms_id', 'network_alarms', ['id'])
        op.create_index('ix_network_alarms_alarm_code', 'network_alarms', ['alarm_code'])
        op.create_index('ix_network_alarms_market_id', 'network_alarms', ['market_id'])
        op.create_index('ix_network_alarms_device_id', 'network_alarms', ['device_id'])
        op.create_index('ix_network_alarms_severity', 'network_alarms', ['severity'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'network_alarms' in existing_tables:
        op.drop_table('network_alarms')
    if 'network_links' in existing_tables:
        op.drop_table('network_links')
    if 'olt_ports' in existing_tables:
        op.drop_table('olt_ports')
    if 'network_devices' in existing_tables:
        op.drop_table('network_devices')
