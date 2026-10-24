"""add_phase8_device_monitoring_tables

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-15 13:17:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Create 'device_metric_thresholds' table
    if 'device_metric_thresholds' not in existing_tables:
        op.create_table(
            'device_metric_thresholds',
            sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='all'),
            sa.Column('device_type', sa.String(length=50), nullable=False),
            sa.Column('metric_type', sa.String(length=50), nullable=False),
            sa.Column('warning_threshold', sa.Float(), nullable=False),
            sa.Column('critical_threshold', sa.Float(), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=True, server_default='%'),
            sa.Column('enabled', sa.Boolean(), nullable=True, server_default=sa.text('true')),
            sa.Column('updated_by_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        )
        op.create_index('ix_device_metric_thresholds_id', 'device_metric_thresholds', ['id'])
        op.create_index('ix_device_metric_thresholds_market_id', 'device_metric_thresholds', ['market_id'])
        op.create_index('ix_device_metric_thresholds_device_type', 'device_metric_thresholds', ['device_type'])
        op.create_index('ix_device_metric_thresholds_metric_type', 'device_metric_thresholds', ['metric_type'])

    # 2. Create 'device_metric_records' table
    if 'device_metric_records' not in existing_tables:
        op.create_table(
            'device_metric_records',
            sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
            sa.Column('device_id', sa.Integer(), sa.ForeignKey('network_devices.id', ondelete='CASCADE'), nullable=False),
            sa.Column('market_id', sa.String(length=50), nullable=False, server_default='mumbai'),
            sa.Column('metric_type', sa.String(length=50), nullable=False),
            sa.Column('value', sa.Float(), nullable=False),
            sa.Column('unit', sa.String(length=20), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=True, server_default='NORMAL'),
            sa.Column('timestamp', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        )
        op.create_index('ix_device_metric_records_id', 'device_metric_records', ['id'])
        op.create_index('ix_device_metric_records_device_id', 'device_metric_records', ['device_id'])
        op.create_index('ix_device_metric_records_market_id', 'device_metric_records', ['market_id'])
        op.create_index('ix_device_metric_records_metric_type', 'device_metric_records', ['metric_type'])
        op.create_index('ix_device_metric_records_timestamp', 'device_metric_records', ['timestamp'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'device_metric_records' in existing_tables:
        op.drop_table('device_metric_records')
    if 'device_metric_thresholds' in existing_tables:
        op.drop_table('device_metric_thresholds')
