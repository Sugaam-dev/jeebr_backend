"""add_inventory_management_tables

Revision ID: m3f4a5b6c7d8
Revises: l2f3a4b5c6d7
Create Date: 2026-10-26 19:00:00.000000

SentinelOS Carrier Inventory & Asset Management Migration:
1. Creates equipment_catalogs for master SKU/hardware catalog.
2. Creates warehouses for multi-market depot & storage locations.
3. Creates warehouse_stocks with quantity constraints (non-negative, reserved <= on-hand).
4. Creates inventory_devices for serialized tracking (ONT, router, MAC, status, assigned order/customer).
5. Creates stock_movements ledger for immutable audit trail of inventory operations.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'm3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'l2f3a4b5c6d7'
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

    # 1. equipment_catalogs
    if 'equipment_catalogs' not in existing_tables:
        op.create_table(
            'equipment_catalogs',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('sku', sa.String(length=64), nullable=False),
            sa.Column('name', sa.String(length=128), nullable=False),
            sa.Column('equipment_type', sa.String(length=32), nullable=False),
            sa.Column('model', sa.String(length=64), nullable=False),
            sa.Column('manufacturer', sa.String(length=64), nullable=False),
            sa.Column('is_serialized', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('hardware_specs', sa.JSON(), nullable=True),
            sa.Column('warranty_months', sa.Integer(), server_default='12', nullable=False),
            sa.Column('unit_cost_inr', sa.Numeric(precision=10, scale=2), server_default='0.00', nullable=False),
            sa.Column('status', sa.String(length=32), server_default='ACTIVE', nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        )
        op.create_index('ix_equipment_catalogs_sku', 'equipment_catalogs', ['sku'], unique=True)
        op.create_index('ix_equipment_catalogs_equipment_type', 'equipment_catalogs', ['equipment_type'])
        op.create_index('ix_equipment_catalogs_status', 'equipment_catalogs', ['status'])

    # 2. warehouses
    if 'warehouses' not in existing_tables:
        op.create_table(
            'warehouses',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('warehouse_code', sa.String(length=32), nullable=False),
            sa.Column('name', sa.String(length=128), nullable=False),
            sa.Column('market_id', sa.String(length=32), nullable=False),
            sa.Column('address', sa.String(length=255), nullable=False),
            sa.Column('locality', sa.String(length=64), nullable=True),
            sa.Column('contact_person', sa.String(length=64), nullable=True),
            sa.Column('contact_phone', sa.String(length=32), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        )
        op.create_index('ix_warehouses_warehouse_code', 'warehouses', ['warehouse_code'], unique=True)
        op.create_index('ix_warehouses_market_id', 'warehouses', ['market_id'])

    # 3. warehouse_stocks
    if 'warehouse_stocks' not in existing_tables:
        op.create_table(
            'warehouse_stocks',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouses.id', ondelete='CASCADE'), nullable=False),
            sa.Column('catalog_id', sa.Integer(), sa.ForeignKey('equipment_catalogs.id', ondelete='CASCADE'), nullable=False),
            sa.Column('quantity_on_hand', sa.Integer(), server_default='0', nullable=False),
            sa.Column('quantity_reserved', sa.Integer(), server_default='0', nullable=False),
            sa.Column('min_threshold', sa.Integer(), server_default='10', nullable=False),
            sa.Column('max_threshold', sa.Integer(), server_default='500', nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.UniqueConstraint('warehouse_id', 'catalog_id', name='uq_warehouse_catalog_stock'),
            sa.CheckConstraint('quantity_on_hand >= 0', name='chk_stock_on_hand_non_negative'),
            sa.CheckConstraint('quantity_reserved >= 0', name='chk_stock_reserved_non_negative'),
            sa.CheckConstraint('quantity_reserved <= quantity_on_hand', name='chk_stock_reserved_within_bounds'),
        )
        op.create_index('ix_warehouse_stocks_warehouse_id', 'warehouse_stocks', ['warehouse_id'])
        op.create_index('ix_warehouse_stocks_catalog_id', 'warehouse_stocks', ['catalog_id'])

    # 4. inventory_devices
    if 'inventory_devices' not in existing_tables:
        op.create_table(
            'inventory_devices',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('serial_number', sa.String(length=64), nullable=False),
            sa.Column('mac_address', sa.String(length=32), nullable=True),
            sa.Column('catalog_id', sa.Integer(), sa.ForeignKey('equipment_catalogs.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('warehouse_id', sa.Integer(), sa.ForeignKey('warehouses.id', ondelete='SET NULL'), nullable=True),
            sa.Column('market_id', sa.String(length=32), nullable=False),
            sa.Column('status', sa.String(length=32), server_default='IN_STOCK', nullable=False),
            sa.Column('assigned_order_id', sa.Integer(), sa.ForeignKey('service_provisioning_orders.id', ondelete='SET NULL'), nullable=True),
            sa.Column('assigned_customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True),
            sa.Column('installed_at', sa.DateTime(), nullable=True),
            sa.Column('returned_at', sa.DateTime(), nullable=True),
            sa.Column('warranty_expires_at', sa.DateTime(), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        )
        op.create_index('ix_inventory_devices_serial_number', 'inventory_devices', ['serial_number'], unique=True)
        op.create_index('ix_inventory_devices_mac_address', 'inventory_devices', ['mac_address'], unique=True)
        op.create_index('ix_inventory_devices_catalog_id', 'inventory_devices', ['catalog_id'])
        op.create_index('ix_inventory_devices_warehouse_id', 'inventory_devices', ['warehouse_id'])
        op.create_index('ix_inventory_devices_market_id', 'inventory_devices', ['market_id'])
        op.create_index('ix_inventory_devices_status', 'inventory_devices', ['status'])
        op.create_index('ix_inventory_devices_assigned_order_id', 'inventory_devices', ['assigned_order_id'])
        op.create_index('ix_inventory_devices_assigned_customer_id', 'inventory_devices', ['assigned_customer_id'])

    # 5. stock_movements
    if 'stock_movements' not in existing_tables:
        op.create_table(
            'stock_movements',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column('movement_code', sa.String(length=64), nullable=False),
            sa.Column('movement_type', sa.String(length=32), nullable=False),
            sa.Column('catalog_id', sa.Integer(), sa.ForeignKey('equipment_catalogs.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('from_warehouse_id', sa.Integer(), sa.ForeignKey('warehouses.id', ondelete='SET NULL'), nullable=True),
            sa.Column('to_warehouse_id', sa.Integer(), sa.ForeignKey('warehouses.id', ondelete='SET NULL'), nullable=True),
            sa.Column('quantity', sa.Integer(), nullable=False),
            sa.Column('serial_numbers', sa.JSON(), nullable=True),
            sa.Column('reference_type', sa.String(length=64), nullable=True),
            sa.Column('reference_id', sa.String(length=64), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('actor_name', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        )
        op.create_index('ix_stock_movements_movement_code', 'stock_movements', ['movement_code'], unique=True)
        op.create_index('ix_stock_movements_movement_type', 'stock_movements', ['movement_type'])
        op.create_index('ix_stock_movements_catalog_id', 'stock_movements', ['catalog_id'])
        op.create_index('ix_stock_movements_from_warehouse_id', 'stock_movements', ['from_warehouse_id'])
        op.create_index('ix_stock_movements_to_warehouse_id', 'stock_movements', ['to_warehouse_id'])


def downgrade() -> None:
    bind = op.get_bind()
    context = op.get_context()
    if getattr(context, 'as_sql', False) or 'MockConnection' in type(bind).__name__:
        existing_tables = {'stock_movements', 'inventory_devices', 'warehouse_stocks', 'warehouses', 'equipment_catalogs'}
    else:
        inspector = sa.inspect(bind)
        existing_tables = set(inspector.get_table_names())

    if 'stock_movements' in existing_tables:
        op.drop_table('stock_movements')
    if 'inventory_devices' in existing_tables:
        op.drop_table('inventory_devices')
    if 'warehouse_stocks' in existing_tables:
        op.drop_table('warehouse_stocks')
    if 'warehouses' in existing_tables:
        op.drop_table('warehouses')
    if 'equipment_catalogs' in existing_tables:
        op.drop_table('equipment_catalogs')
