"""
app/models_inventory.py

SentinelOS Carrier Inventory & Asset Management Domain Models.
Supports:
1. Equipment Catalog (ONTs, Routers, Splitters, SFPs, Accessories) with SKUs, hardware specifications, and serialized flags.
2. Multi-Market Warehouses and Storage Locations.
3. Warehouse Stock aggregates with minimum/maximum threshold monitoring and negative-stock prevention.
4. Individual Serialized Device Tracking (serial numbers, MAC addresses, warranty, customer & provisioning order assignment).
5. Immutable Stock Movements ledger (Receipts, Transfers, Reservations, Releases, Consumptions, Returns, Damage Write-offs).
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey,
    Numeric, Text, JSON, CheckConstraint, UniqueConstraint, Index, text
)
from sqlalchemy.orm import relationship
from app.database import Base


class EquipmentCatalog(Base):
    """
    Operator master equipment and asset catalog.
    Defines hardware models, technical specifications, and whether items require serial-level tracking.
    """
    __tablename__ = 'equipment_catalogs'

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String(64), unique=True, index=True, nullable=False)  # e.g. ONT-GPON-WIFI5-4GE
    name = Column(String(128), nullable=False)
    equipment_type = Column(String(32), index=True, nullable=False)  # ONT, ROUTER, SWITCH, SPLITTER, SFP, ACCESSORY, CABLE
    model = Column(String(64), nullable=False)  # e.g. HG8145V5, G-140W-ME
    manufacturer = Column(String(64), nullable=False)  # Huawei, Nokia, ZTE, TP-Link, D-Link
    is_serialized = Column(Boolean, default=True, nullable=False)
    hardware_specs = Column(JSON, nullable=True)  # ports, speed, optical wavelength, tx/rx range
    warranty_months = Column(Integer, default=12, nullable=False)
    unit_cost_inr = Column(Numeric(10, 2), default=0.00, nullable=False)
    status = Column(String(32), default='ACTIVE', index=True, nullable=False)  # ACTIVE, DEPRECATED, END_OF_LIFE

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    stocks = relationship('WarehouseStock', back_populates='catalog', cascade='all, delete-orphan')
    devices = relationship('InventoryDevice', back_populates='catalog')
    movements = relationship('StockMovement', back_populates='catalog')


class Warehouse(Base):
    """
    Regional equipment depot, warehouse, or transit storage facility.
    Bound to regional operational markets.
    """
    __tablename__ = 'warehouses'

    id = Column(Integer, primary_key=True, index=True)
    warehouse_code = Column(String(32), unique=True, index=True, nullable=False)  # e.g. WH-MUM-CENTRAL
    name = Column(String(128), nullable=False)
    market_id = Column(String(32), index=True, nullable=False)  # mumbai, kolkata
    address = Column(String(255), nullable=False)
    locality = Column(String(64), nullable=True)
    contact_person = Column(String(64), nullable=True)
    contact_phone = Column(String(32), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    stocks = relationship('WarehouseStock', back_populates='warehouse', cascade='all, delete-orphan')
    devices = relationship('InventoryDevice', back_populates='warehouse')


class WarehouseStock(Base):
    """
    Aggregated inventory stock balance by product and warehouse.
    Enforces non-negative stock constraints and threshold monitoring.
    """
    __tablename__ = 'warehouse_stocks'
    __table_args__ = (
        UniqueConstraint('warehouse_id', 'catalog_id', name='uq_warehouse_catalog_stock'),
        CheckConstraint('quantity_on_hand >= 0', name='chk_stock_on_hand_non_negative'),
        CheckConstraint('quantity_reserved >= 0', name='chk_stock_reserved_non_negative'),
        CheckConstraint('quantity_reserved <= quantity_on_hand', name='chk_stock_reserved_within_bounds'),
    )

    id = Column(Integer, primary_key=True, index=True)
    warehouse_id = Column(Integer, ForeignKey('warehouses.id', ondelete='CASCADE'), nullable=False, index=True)
    catalog_id = Column(Integer, ForeignKey('equipment_catalogs.id', ondelete='CASCADE'), nullable=False, index=True)

    quantity_on_hand = Column(Integer, default=0, nullable=False)
    quantity_reserved = Column(Integer, default=0, nullable=False)
    min_threshold = Column(Integer, default=10, nullable=False)
    max_threshold = Column(Integer, default=500, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    warehouse = relationship('Warehouse', back_populates='stocks')
    catalog = relationship('EquipmentCatalog', back_populates='stocks')

    @property
    def quantity_available(self) -> int:
        return max(0, (self.quantity_on_hand or 0) - (self.quantity_reserved or 0))


class InventoryDevice(Base):
    """
    Individual tracked serialized hardware asset (ONT, Router, Switch).
    Maintains authoritative physical device lifecycle, customer linkage, and warranty tracking.
    """
    __tablename__ = 'inventory_devices'

    id = Column(Integer, primary_key=True, index=True)
    serial_number = Column(String(64), unique=True, index=True, nullable=False)
    mac_address = Column(String(32), unique=True, index=True, nullable=True)

    catalog_id = Column(Integer, ForeignKey('equipment_catalogs.id', ondelete='RESTRICT'), nullable=False, index=True)
    warehouse_id = Column(Integer, ForeignKey('warehouses.id', ondelete='SET NULL'), nullable=True, index=True)
    market_id = Column(String(32), index=True, nullable=False)

    # Lifecycle Status: IN_STOCK, RESERVED, IN_TRANSIT, INSTALLED, RETURNED, DAMAGED, QUARANTINE, RETIRED
    status = Column(String(32), default='IN_STOCK', index=True, nullable=False)

    # Assignment linkages
    assigned_order_id = Column(Integer, ForeignKey('service_provisioning_orders.id', ondelete='SET NULL'), nullable=True, index=True)
    assigned_customer_id = Column(Integer, ForeignKey('customers.id', ondelete='SET NULL'), nullable=True, index=True)

    installed_at = Column(DateTime, nullable=True)
    returned_at = Column(DateTime, nullable=True)
    warranty_expires_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    catalog = relationship('EquipmentCatalog', back_populates='devices')
    warehouse = relationship('Warehouse', back_populates='devices')
    assigned_order = relationship('ServiceActivationOrder')
    assigned_customer = relationship('Customer')

    __table_args__ = (
        Index(
            'ix_inventory_devices_assigned_order_active',
            'assigned_order_id',
            unique=True,
            sqlite_where=text("assigned_order_id IS NOT NULL AND status IN ('RESERVED', 'INSTALLED')"),
            postgresql_where=text("assigned_order_id IS NOT NULL AND status IN ('RESERVED', 'INSTALLED')")
        ),
    )


class StockMovement(Base):
    """
    Immutable audit ledger of all inventory transactions.
    Tracks receipts, transfers, reservations, consumptions, and returns.
    """
    __tablename__ = 'stock_movements'

    id = Column(Integer, primary_key=True, index=True)
    movement_code = Column(String(64), unique=True, index=True, nullable=False)  # e.g. MOV-MUM-2026-0001
    movement_type = Column(String(32), index=True, nullable=False)  # RECEIPT, TRANSFER, RESERVATION, RELEASE, CONSUMPTION, RETURN, DAMAGE_WRITE_OFF, RETIREMENT

    catalog_id = Column(Integer, ForeignKey('equipment_catalogs.id', ondelete='RESTRICT'), nullable=False, index=True)
    from_warehouse_id = Column(Integer, ForeignKey('warehouses.id', ondelete='SET NULL'), nullable=True, index=True)
    to_warehouse_id = Column(Integer, ForeignKey('warehouses.id', ondelete='SET NULL'), nullable=True, index=True)

    quantity = Column(Integer, nullable=False)
    serial_numbers = Column(JSON, nullable=True)  # List of serialized device strings involved

    reference_type = Column(String(64), nullable=True)  # PROVISIONING_ORDER, TICKET, PURCHASE_ORDER, MANUAL_ADJUSTMENT
    reference_id = Column(String(64), nullable=True)  # e.g. order_code, ticket_code

    notes = Column(Text, nullable=True)
    actor_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    actor_name = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    catalog = relationship('EquipmentCatalog', back_populates='movements')
    from_warehouse = relationship('Warehouse', foreign_keys=[from_warehouse_id])
    to_warehouse = relationship('Warehouse', foreign_keys=[to_warehouse_id])
    actor = relationship('User')
