"""
app/routers/inventory.py

SentinelOS Carrier Inventory & Asset Management REST API Router.

Endpoints:
  GET    /api/inventory/catalogs             -> List equipment catalog items
  POST   /api/inventory/catalogs             -> Create equipment catalog item (Admin/NOC/Super Admin)
  GET    /api/inventory/catalogs/{id}        -> Get equipment catalog item detail
  PUT    /api/inventory/catalogs/{id}        -> Update equipment catalog item (Admin/NOC/Super Admin)

  GET    /api/inventory/warehouses           -> List warehouses (filtered by market)
  POST   /api/inventory/warehouses           -> Create warehouse (Admin/Super Admin)
  GET    /api/inventory/warehouses/{id}      -> Get warehouse details
  PUT    /api/inventory/warehouses/{id}      -> Update warehouse (Admin/Super Admin)

  GET    /api/inventory/stocks               -> List stock aggregates across warehouses
  POST   /api/inventory/stocks/receipt       -> Receive stock batch (Admin/NOC/Super Admin)
  POST   /api/inventory/stocks/transfer      -> Transfer stock between depots (Admin/NOC/Super Admin)
  POST   /api/inventory/stocks/adjust        -> Manual stock adjustment with audit note (Admin/Super Admin)

  GET    /api/inventory/devices              -> Search/list serialized devices
  POST   /api/inventory/devices              -> Register serialized device (Admin/NOC/Super Admin)
  GET    /api/inventory/devices/{serial}     -> Get device details by serial number

  POST   /api/inventory/operations/return    -> Process device return/RMA
  POST   /api/inventory/operations/replace   -> Process device replacement for support ticket

  GET    /api/inventory/movements            -> Audit ledger of inventory movements
  GET    /api/inventory/stats                -> Inventory KPI dashboard metrics
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user, require_roles
from app.markets import get_current_market
from app.schemas_inventory import (
    EquipmentCatalogCreate, EquipmentCatalogUpdate, EquipmentCatalogResponse,
    WarehouseCreate, WarehouseUpdate, WarehouseResponse,
    WarehouseStockResponse, StockReceiptRequest, StockTransferRequest, StockAdjustmentRequest,
    InventoryDeviceCreate, InventoryDeviceResponse,
    StockMovementResponse, DeviceReturnRequest, DeviceReplacementRequest,
    InventoryStatsResponse
)
from app.services import inventory_service

router = APIRouter(prefix="/inventory", tags=["Carrier Inventory & Asset Management"])


# ---------------------------------------------------------------------------
# Equipment Catalogs
# ---------------------------------------------------------------------------

@router.get("/catalogs", response_model=List[EquipmentCatalogResponse])
def get_catalogs(
    equipment_type: Optional[str] = Query(None, description="Filter by type: ONT, ROUTER, SWITCH, SPLITTER, etc."),
    status: Optional[str] = Query(None, description="Filter by status: ACTIVE, DEPRECATED, END_OF_LIFE"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return inventory_service.list_catalogs(db, equipment_type=equipment_type, status=status)


@router.post("/catalogs", response_model=EquipmentCatalogResponse, status_code=status.HTTP_201_CREATED)
def create_catalog(
    catalog_in: EquipmentCatalogCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC"]))
):
    return inventory_service.create_catalog(db, catalog_in)


@router.get("/catalogs/{catalog_id}", response_model=EquipmentCatalogResponse)
def get_catalog_detail(
    catalog_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return inventory_service.get_catalog(db, catalog_id)


@router.put("/catalogs/{catalog_id}", response_model=EquipmentCatalogResponse)
def update_catalog(
    catalog_id: int,
    catalog_in: EquipmentCatalogUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC"]))
):
    return inventory_service.update_catalog(db, catalog_id, catalog_in)


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------

@router.get("/warehouses", response_model=List[WarehouseResponse])
def get_warehouses(
    market: Optional[str] = Query(None, description="Market filter (defaults to tenant market if set)"),
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    market_filter = market or current_market
    return inventory_service.list_warehouses(db, market_id=market_filter, is_active=is_active)


@router.post("/warehouses", response_model=WarehouseResponse, status_code=status.HTTP_201_CREATED)
def create_warehouse(
    warehouse_in: WarehouseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"]))
):
    return inventory_service.create_warehouse(db, warehouse_in)


@router.get("/warehouses/{warehouse_id}", response_model=WarehouseResponse)
def get_warehouse_detail(
    warehouse_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return inventory_service.get_warehouse(db, warehouse_id)


@router.put("/warehouses/{warehouse_id}", response_model=WarehouseResponse)
def update_warehouse(
    warehouse_id: int,
    warehouse_in: WarehouseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"]))
):
    return inventory_service.update_warehouse(db, warehouse_id, warehouse_in)


# ---------------------------------------------------------------------------
# Warehouse Stocks & Aggregates
# ---------------------------------------------------------------------------

@router.get("/stocks", response_model=List[WarehouseStockResponse])
def get_warehouse_stocks(
    warehouse_id: Optional[int] = Query(None),
    catalog_id: Optional[int] = Query(None),
    low_stock_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    return inventory_service.list_warehouse_stock(
        db,
        warehouse_id=warehouse_id,
        catalog_id=catalog_id,
        market_id=current_market,
        low_stock_only=low_stock_only
    )


@router.post("/stocks/receipt", response_model=WarehouseStockResponse)
def receive_stock_batch(
    receipt_in: StockReceiptRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC"])),
    current_market: str = Depends(get_current_market)
):
    _, stock = inventory_service.receive_stock(
        db=db,
        warehouse_id=receipt_in.warehouse_id,
        catalog_id=receipt_in.catalog_id,
        quantity=receipt_in.quantity,
        serial_numbers=receipt_in.serial_numbers,
        mac_addresses=receipt_in.mac_addresses,
        notes=receipt_in.notes,
        actor=current_user,
        caller_market=current_market
    )
    return stock


@router.post("/stocks/transfer")
def transfer_stock_batch(
    transfer_in: StockTransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC"])),
    current_market: str = Depends(get_current_market)
):
    movement, src_stock, dst_stock = inventory_service.transfer_stock(
        db=db,
        from_warehouse_id=transfer_in.from_warehouse_id,
        to_warehouse_id=transfer_in.to_warehouse_id,
        catalog_id=transfer_in.catalog_id,
        quantity=transfer_in.quantity,
        serial_numbers=transfer_in.serial_numbers,
        notes=transfer_in.notes,
        actor=current_user,
        caller_market=current_market
    )
    return {
        "success": True,
        "movement_code": movement.movement_code,
        "from_warehouse_id": transfer_in.from_warehouse_id,
        "to_warehouse_id": transfer_in.to_warehouse_id,
        "transferred_quantity": transfer_in.quantity
    }


@router.post("/stocks/adjust", response_model=WarehouseStockResponse)
def adjust_stock(
    adjust_in: StockAdjustmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"])),
    current_market: str = Depends(get_current_market)
):
    _, stock = inventory_service.adjust_stock(
        db=db,
        warehouse_id=adjust_in.warehouse_id,
        catalog_id=adjust_in.catalog_id,
        quantity_delta=adjust_in.quantity_delta,
        notes=adjust_in.notes,
        actor=current_user,
        caller_market=current_market
    )
    return stock


# ---------------------------------------------------------------------------
# Individual Serialized Devices
# ---------------------------------------------------------------------------

@router.get("/devices", response_model=List[InventoryDeviceResponse])
def get_devices(
    warehouse_id: Optional[int] = Query(None),
    catalog_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None, description="IN_STOCK, RESERVED, INSTALLED, RETURNED, DAMAGED, RETIRED"),
    search: Optional[str] = Query(None, description="Search serial number or MAC"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    return inventory_service.list_devices(
        db,
        warehouse_id=warehouse_id,
        catalog_id=catalog_id,
        market_id=current_market,
        status=status,
        search=search,
        skip=skip,
        limit=limit
    )


@router.post("/devices", response_model=InventoryDeviceResponse, status_code=status.HTTP_201_CREATED)
def create_device(
    device_in: InventoryDeviceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC"])),
    current_market: str = Depends(get_current_market)
):
    return inventory_service.create_device(db, device_in, actor=current_user, caller_market=current_market)


@router.get("/devices/{serial_number}", response_model=InventoryDeviceResponse)
def get_device(
    serial_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return inventory_service.get_device_by_serial(db, serial_number)


# ---------------------------------------------------------------------------
# Returns & Replacements
# ---------------------------------------------------------------------------

@router.post("/operations/return")
def process_return(
    return_in: DeviceReturnRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC", "Care"])),
    current_market: str = Depends(get_current_market)
):
    movement, device = inventory_service.process_device_return(
        db=db,
        serial_number=return_in.serial_number,
        warehouse_id=return_in.warehouse_id,
        condition=return_in.condition,
        notes=return_in.notes,
        actor=current_user,
        caller_market=current_market
    )
    return {
        "success": True,
        "serial_number": device.serial_number,
        "new_status": device.status,
        "movement_code": movement.movement_code
    }


@router.post("/operations/replace")
def process_replacement(
    replace_in: DeviceReplacementRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "NOC", "Care"])),
    current_market: str = Depends(get_current_market)
):
    return inventory_service.replace_device_for_ticket(
        db=db,
        old_serial_number=replace_in.old_serial_number,
        new_serial_number=replace_in.new_serial_number,
        ticket_id=replace_in.ticket_id,
        order_id=replace_in.order_id,
        notes=replace_in.notes,
        actor=current_user,
        caller_market=current_market
    )


# ---------------------------------------------------------------------------
# Movements & Dashboard Stats
# ---------------------------------------------------------------------------

@router.get("/movements", response_model=List[StockMovementResponse])
def get_movements(
    catalog_id: Optional[int] = Query(None),
    warehouse_id: Optional[int] = Query(None),
    movement_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return inventory_service.list_movements(
        db,
        catalog_id=catalog_id,
        warehouse_id=warehouse_id,
        movement_type=movement_type,
        skip=skip,
        limit=limit
    )


@router.get("/stats", response_model=InventoryStatsResponse)
def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    return inventory_service.get_inventory_stats(db, market_id=current_market)
