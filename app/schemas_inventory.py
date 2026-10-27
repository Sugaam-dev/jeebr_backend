"""
app/schemas_inventory.py

Pydantic validation and serialization schemas for SentinelOS Carrier Inventory & Asset Management.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Equipment Catalog Schemas
# ---------------------------------------------------------------------------

class EquipmentCatalogBase(BaseModel):
    sku: str = Field(..., max_length=64, description="Unique stock keeping unit code")
    name: str = Field(..., max_length=128, description="Product catalog name")
    equipment_type: str = Field(..., max_length=32, description="ONT, ROUTER, SWITCH, SPLITTER, SFP, ACCESSORY, CABLE")
    model: str = Field(..., max_length=64, description="Manufacturer model designation")
    manufacturer: str = Field(..., max_length=64, description="Hardware vendor")
    is_serialized: bool = Field(True, description="Whether individual device serial numbers are tracked")
    hardware_specs: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Technical specifications")
    warranty_months: int = Field(12, ge=0, description="Standard vendor warranty in months")
    unit_cost_inr: float = Field(0.00, ge=0, description="Unit acquisition cost in INR")
    status: str = Field("ACTIVE", description="ACTIVE, DEPRECATED, END_OF_LIFE")


class EquipmentCatalogCreate(EquipmentCatalogBase):
    pass


class EquipmentCatalogUpdate(BaseModel):
    name: Optional[str] = None
    equipment_type: Optional[str] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None
    is_serialized: Optional[bool] = None
    hardware_specs: Optional[Dict[str, Any]] = None
    warranty_months: Optional[int] = None
    unit_cost_inr: Optional[float] = None
    status: Optional[str] = None


class EquipmentCatalogResponse(EquipmentCatalogBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Warehouse Schemas
# ---------------------------------------------------------------------------

class WarehouseBase(BaseModel):
    warehouse_code: str = Field(..., max_length=32, description="Unique depot identifier code")
    name: str = Field(..., max_length=128, description="Facility or depot name")
    market_id: str = Field(..., max_length=32, description="Operational market code (e.g. mumbai, kolkata)")
    address: str = Field(..., max_length=255, description="Physical location address")
    locality: Optional[str] = Field(None, max_length=64)
    contact_person: Optional[str] = Field(None, max_length=64)
    contact_phone: Optional[str] = Field(None, max_length=32)
    is_active: bool = True


class WarehouseCreate(WarehouseBase):
    pass


class WarehouseUpdate(BaseModel):
    name: Optional[str] = None
    market_id: Optional[str] = None
    address: Optional[str] = None
    locality: Optional[str] = None
    contact_person: Optional[str] = None
    contact_phone: Optional[str] = None
    is_active: Optional[bool] = None


class WarehouseResponse(WarehouseBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Warehouse Stock Schemas
# ---------------------------------------------------------------------------

class WarehouseStockResponse(BaseModel):
    id: int
    warehouse_id: int
    catalog_id: int
    quantity_on_hand: int
    quantity_reserved: int
    quantity_available: int
    min_threshold: int
    max_threshold: int
    created_at: datetime
    updated_at: datetime
    catalog: Optional[EquipmentCatalogResponse] = None
    warehouse: Optional[WarehouseResponse] = None

    class Config:
        from_attributes = True


class StockReceiptRequest(BaseModel):
    warehouse_id: int
    catalog_id: int
    quantity: int = Field(..., gt=0, description="Quantity to receive into inventory")
    serial_numbers: Optional[List[str]] = Field(default=None, description="Optional list of serial numbers for serialized devices")
    mac_addresses: Optional[List[str]] = Field(default=None, description="Optional corresponding MAC addresses")
    notes: Optional[str] = None


class StockTransferRequest(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    catalog_id: int
    quantity: int = Field(..., gt=0, description="Quantity to transfer between warehouses")
    serial_numbers: Optional[List[str]] = Field(default=None, description="Optional list of serial numbers being transferred")
    notes: Optional[str] = None


class StockAdjustmentRequest(BaseModel):
    warehouse_id: int
    catalog_id: int
    quantity_delta: int = Field(..., description="Quantity delta (positive or negative adjustment)")
    notes: str = Field(..., min_length=3, description="Mandatory audit justification for manual adjustment")


# ---------------------------------------------------------------------------
# Inventory Device Schemas
# ---------------------------------------------------------------------------

class InventoryDeviceCreate(BaseModel):
    serial_number: str = Field(..., max_length=64, description="Unique hardware serial number")
    mac_address: Optional[str] = Field(None, max_length=32, description="Unique MAC address")
    catalog_id: int
    warehouse_id: int
    market_id: str = Field(..., max_length=32)
    warranty_expires_at: Optional[datetime] = None
    notes: Optional[str] = None


class InventoryDeviceUpdate(BaseModel):
    warehouse_id: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class InventoryDeviceResponse(BaseModel):
    id: int
    serial_number: str
    mac_address: Optional[str] = None
    catalog_id: int
    warehouse_id: Optional[int] = None
    market_id: str
    status: str
    assigned_order_id: Optional[int] = None
    assigned_customer_id: Optional[int] = None
    installed_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    warranty_expires_at: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    catalog: Optional[EquipmentCatalogResponse] = None
    warehouse: Optional[WarehouseResponse] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Stock Movement Schemas
# ---------------------------------------------------------------------------

class StockMovementResponse(BaseModel):
    id: int
    movement_code: str
    movement_type: str
    catalog_id: int
    from_warehouse_id: Optional[int] = None
    to_warehouse_id: Optional[int] = None
    quantity: int
    serial_numbers: Optional[List[str]] = None
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    notes: Optional[str] = None
    actor_id: Optional[int] = None
    actor_name: Optional[str] = None
    created_at: datetime
    catalog: Optional[EquipmentCatalogResponse] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Operations: Return & Replacement Schemas
# ---------------------------------------------------------------------------

class DeviceReturnRequest(BaseModel):
    serial_number: str = Field(..., description="Serial number of the returning device")
    warehouse_id: int = Field(..., description="Depot receiving the returned device")
    condition: str = Field("WORKING", description="Condition: WORKING (returns to stock) or DAMAGED (moves to quarantine/damaged)")
    notes: Optional[str] = None


class DeviceReplacementRequest(BaseModel):
    old_serial_number: str = Field(..., description="Serial number of the faulty device being replaced")
    new_serial_number: str = Field(..., description="Serial number of the replacement device from stock")
    ticket_id: Optional[int] = Field(None, description="Optional associated CRM / Support Ticket ID")
    order_id: Optional[int] = Field(None, description="Optional associated Provisioning Order ID")
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Inventory Summary & Dashboard Stats
# ---------------------------------------------------------------------------

class InventoryStatsResponse(BaseModel):
    total_devices: int
    devices_in_stock: int
    devices_installed: int
    devices_reserved: int
    devices_damaged: int
    devices_returned: int
    total_warehouses: int
    low_stock_items_count: int
