"""
app/services/inventory_service.py

SentinelOS Carrier Inventory & Asset Management Service.
Handles:
1. Equipment catalog lifecycle and warehouse management.
2. Multi-market warehouse stock tracking with non-negative constraints.
3. Serialized device tracking with uniqueness validation (Serial number & MAC).
4. Provisioning workflow hooks: equipment reservation, cancellation release, and installation consumption.
5. RMA / Returns and fault replacement workflows.
6. Immutable stock movement audit ledger.
"""

import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from fastapi import HTTPException, status

from app.models_inventory import (
    EquipmentCatalog, Warehouse, WarehouseStock, InventoryDevice, StockMovement
)
from app.models import User, Customer
from app.models_provisioning import ServiceActivationOrder


def generate_movement_code(prefix: str = "MOV") -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand_suffix = uuid.uuid4().hex[:6].upper()
    return f"{prefix}-{timestamp}-{rand_suffix}"


def validate_market_access(
    caller_market: Optional[str],
    target_market: Optional[str],
    actor: Optional[User] = None
) -> None:
    """
    Verifies caller has authority to operate in target_market.
    Super Admin users bypass market boundary checks.
    If caller_market is provided and does not match target_market, raises 403 Forbidden.
    """
    if actor and hasattr(actor, "role") and actor.role == "Super Admin":
        return
    if not caller_market or not target_market:
        return
    if caller_market.lower().strip() != target_market.lower().strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Operator market '{caller_market}' cannot modify inventory in market '{target_market}'"
        )


# ---------------------------------------------------------------------------
# Catalog & Warehouse Management
# ---------------------------------------------------------------------------

def list_catalogs(
    db: Session,
    equipment_type: Optional[str] = None,
    status: Optional[str] = None
) -> List[EquipmentCatalog]:
    query = db.query(EquipmentCatalog)
    if equipment_type:
        query = query.filter(EquipmentCatalog.equipment_type == equipment_type)
    if status:
        query = query.filter(EquipmentCatalog.status == status)
    return query.order_by(EquipmentCatalog.name).all()


def get_catalog(db: Session, catalog_id: int) -> EquipmentCatalog:
    catalog = db.query(EquipmentCatalog).filter(EquipmentCatalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Equipment catalog item with ID {catalog_id} not found"
        )
    return catalog


def create_catalog(db: Session, catalog_in: Any) -> EquipmentCatalog:
    existing = db.query(EquipmentCatalog).filter(EquipmentCatalog.sku == catalog_in.sku).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Equipment catalog SKU '{catalog_in.sku}' already exists"
        )
    catalog = EquipmentCatalog(
        sku=catalog_in.sku.strip(),
        name=catalog_in.name.strip(),
        equipment_type=catalog_in.equipment_type.upper().strip(),
        model=catalog_in.model.strip(),
        manufacturer=catalog_in.manufacturer.strip(),
        is_serialized=catalog_in.is_serialized,
        hardware_specs=catalog_in.hardware_specs or {},
        warranty_months=catalog_in.warranty_months,
        unit_cost_inr=catalog_in.unit_cost_inr,
        status=catalog_in.status
    )
    db.add(catalog)
    db.commit()
    db.refresh(catalog)
    return catalog


def update_catalog(db: Session, catalog_id: int, catalog_in: Any) -> EquipmentCatalog:
    catalog = get_catalog(db, catalog_id)
    update_data = catalog_in.model_dump(exclude_unset=True) if hasattr(catalog_in, 'model_dump') else catalog_in.dict(exclude_unset=True)
    for field, val in update_data.items():
        if val is not None:
            setattr(catalog, field, val)
    catalog.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(catalog)
    return catalog


def list_warehouses(
    db: Session,
    market_id: Optional[str] = None,
    is_active: Optional[bool] = None
) -> List[Warehouse]:
    query = db.query(Warehouse)
    if market_id:
        query = query.filter(Warehouse.market_id == market_id)
    if is_active is not None:
        query = query.filter(Warehouse.is_active == is_active)
    return query.order_by(Warehouse.warehouse_code).all()


def get_warehouse(db: Session, warehouse_id: int) -> Warehouse:
    warehouse = db.query(Warehouse).filter(Warehouse.id == warehouse_id).first()
    if not warehouse:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Warehouse with ID {warehouse_id} not found"
        )
    return warehouse


def create_warehouse(db: Session, warehouse_in: Any) -> Warehouse:
    existing = db.query(Warehouse).filter(Warehouse.warehouse_code == warehouse_in.warehouse_code).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Warehouse code '{warehouse_in.warehouse_code}' already exists"
        )
    warehouse = Warehouse(
        warehouse_code=warehouse_in.warehouse_code.strip(),
        name=warehouse_in.name.strip(),
        market_id=warehouse_in.market_id.lower().strip(),
        address=warehouse_in.address.strip(),
        locality=warehouse_in.locality,
        contact_person=warehouse_in.contact_person,
        contact_phone=warehouse_in.contact_phone,
        is_active=warehouse_in.is_active
    )
    db.add(warehouse)
    db.commit()
    db.refresh(warehouse)
    return warehouse


def update_warehouse(db: Session, warehouse_id: int, warehouse_in: Any) -> Warehouse:
    warehouse = get_warehouse(db, warehouse_id)
    update_data = warehouse_in.model_dump(exclude_unset=True) if hasattr(warehouse_in, 'model_dump') else warehouse_in.dict(exclude_unset=True)
    for field, val in update_data.items():
        if val is not None:
            setattr(warehouse, field, val)
    warehouse.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(warehouse)
    return warehouse


# ---------------------------------------------------------------------------
# Warehouse Stock Aggregates & Ledger Movements
# ---------------------------------------------------------------------------

def get_or_create_warehouse_stock(db: Session, warehouse_id: int, catalog_id: int) -> WarehouseStock:
    stock = db.query(WarehouseStock).filter(
        WarehouseStock.warehouse_id == warehouse_id,
        WarehouseStock.catalog_id == catalog_id
    ).first()
    if not stock:
        # Verify warehouse and catalog exist
        get_warehouse(db, warehouse_id)
        get_catalog(db, catalog_id)
        stock = WarehouseStock(
            warehouse_id=warehouse_id,
            catalog_id=catalog_id,
            quantity_on_hand=0,
            quantity_reserved=0,
            min_threshold=10,
            max_threshold=500
        )
        db.add(stock)
        db.flush()
    return stock


def list_warehouse_stock(
    db: Session,
    warehouse_id: Optional[int] = None,
    catalog_id: Optional[int] = None,
    market_id: Optional[str] = None,
    low_stock_only: bool = False
) -> List[WarehouseStock]:
    query = db.query(WarehouseStock).join(Warehouse)
    if warehouse_id:
        query = query.filter(WarehouseStock.warehouse_id == warehouse_id)
    if catalog_id:
        query = query.filter(WarehouseStock.catalog_id == catalog_id)
    if market_id:
        query = query.filter(Warehouse.market_id == market_id)
    if low_stock_only:
        query = query.filter(WarehouseStock.quantity_on_hand <= WarehouseStock.min_threshold)
    return query.all()


def receive_stock(
    db: Session,
    warehouse_id: int,
    catalog_id: int,
    quantity: int,
    serial_numbers: Optional[List[str]] = None,
    mac_addresses: Optional[List[str]] = None,
    notes: Optional[str] = None,
    actor: Optional[User] = None,
    caller_market: Optional[str] = None
) -> Tuple[StockMovement, WarehouseStock]:
    if quantity <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stock receipt quantity must be greater than zero"
        )
    warehouse = get_warehouse(db, warehouse_id)
    catalog = get_catalog(db, catalog_id)
    validate_market_access(caller_market, warehouse.market_id, actor)

    # If serialized, validate serials provided
    clean_serials: List[str] = []
    if catalog.is_serialized:
        if not serial_numbers or len(serial_numbers) != quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product '{catalog.sku}' is serialized; exactly {quantity} serial number(s) must be provided"
            )
        # Check uniqueness of serial numbers
        clean_serials = [s.strip() for s in serial_numbers if s.strip()]
        if len(clean_serials) != len(set(clean_serials)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate serial numbers provided in receipt payload"
            )
        existing_serials = db.query(InventoryDevice.serial_number).filter(
            InventoryDevice.serial_number.in_(clean_serials)
        ).all()
        if existing_serials:
            dups = [s[0] for s in existing_serials]
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Serial numbers already registered: {', '.join(dups)}"
            )

        # Check MAC addresses if provided
        clean_macs: List[Optional[str]] = []
        if mac_addresses:
            if len(mac_addresses) != quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"If MAC addresses are provided, count ({len(mac_addresses)}) must match quantity ({quantity})"
                )
            clean_macs = [m.strip().upper() if m else None for m in mac_addresses]
            non_null_macs = [m for m in clean_macs if m]
            if len(non_null_macs) != len(set(non_null_macs)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Duplicate MAC addresses provided in receipt payload"
                )
            existing_macs = db.query(InventoryDevice.mac_address).filter(
                InventoryDevice.mac_address.in_(non_null_macs)
            ).all()
            if existing_macs:
                dups = [m[0] for m in existing_macs]
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"MAC addresses already registered: {', '.join(dups)}"
                )
        else:
            clean_macs = [None] * quantity

        # Create InventoryDevice records
        for i, sn in enumerate(clean_serials):
            mac = clean_macs[i] if i < len(clean_macs) else None
            device = InventoryDevice(
                serial_number=sn,
                mac_address=mac,
                catalog_id=catalog_id,
                warehouse_id=warehouse_id,
                market_id=warehouse.market_id,
                status="IN_STOCK",
                notes=f"Received via batch receipt. {notes or ''}".strip()
            )
            db.add(device)

    # Update WarehouseStock
    stock = get_or_create_warehouse_stock(db, warehouse_id, catalog_id)
    stock.quantity_on_hand += quantity
    stock.updated_at = datetime.utcnow()

    # Record Movement
    movement = StockMovement(
        movement_code=generate_movement_code("REC"),
        movement_type="RECEIPT",
        catalog_id=catalog_id,
        to_warehouse_id=warehouse_id,
        quantity=quantity,
        serial_numbers=clean_serials if clean_serials else None,
        reference_type="PURCHASE_RECEIPT",
        notes=notes,
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "System"
    )
    db.add(movement)
    db.commit()
    db.refresh(stock)
    db.refresh(movement)
    return movement, stock


def transfer_stock(
    db: Session,
    from_warehouse_id: int,
    to_warehouse_id: int,
    catalog_id: int,
    quantity: int,
    serial_numbers: Optional[List[str]] = None,
    notes: Optional[str] = None,
    actor: Optional[User] = None,
    caller_market: Optional[str] = None
) -> Tuple[StockMovement, WarehouseStock, WarehouseStock]:
    if from_warehouse_id == to_warehouse_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and destination warehouses cannot be the same"
        )
    if quantity <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transfer quantity must be greater than zero"
        )

    from_wh = get_warehouse(db, from_warehouse_id)
    to_wh = get_warehouse(db, to_warehouse_id)
    validate_market_access(caller_market, from_wh.market_id, actor)
    catalog = get_catalog(db, catalog_id)
    source_stock = get_or_create_warehouse_stock(db, from_warehouse_id, catalog_id)
    dest_stock = get_or_create_warehouse_stock(db, to_warehouse_id, catalog_id)

    if source_stock.quantity_available < quantity:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient available stock at {from_wh.warehouse_code}. Available: {source_stock.quantity_available}, Requested: {quantity}"
        )

    clean_serials: List[str] = []
    if catalog.is_serialized:
        if not serial_numbers or len(serial_numbers) != quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product '{catalog.sku}' is serialized; exactly {quantity} serial number(s) must be specified for transfer"
            )
        clean_serials = [s.strip() for s in serial_numbers if s.strip()]
        # Check all devices belong to from_warehouse and are IN_STOCK
        devices = db.query(InventoryDevice).filter(
            InventoryDevice.serial_number.in_(clean_serials),
            InventoryDevice.catalog_id == catalog_id,
            InventoryDevice.warehouse_id == from_warehouse_id,
            InventoryDevice.status == "IN_STOCK"
        ).all()
        if len(devices) != quantity:
            found_sns = {d.serial_number for d in devices}
            missing = set(clean_serials) - found_sns
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The following devices are not available IN_STOCK in warehouse {from_wh.warehouse_code}: {', '.join(missing)}"
            )
        # Update devices to target warehouse and target market
        for d in devices:
            d.warehouse_id = to_warehouse_id
            d.market_id = to_wh.market_id
            d.updated_at = datetime.utcnow()

    # Decrement source, increment destination
    source_stock.quantity_on_hand -= quantity
    dest_stock.quantity_on_hand += quantity
    source_stock.updated_at = datetime.utcnow()
    dest_stock.updated_at = datetime.utcnow()

    # Record Movement
    movement = StockMovement(
        movement_code=generate_movement_code("TRF"),
        movement_type="TRANSFER",
        catalog_id=catalog_id,
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        quantity=quantity,
        serial_numbers=clean_serials if clean_serials else None,
        reference_type="INTER_WAREHOUSE_TRANSFER",
        notes=notes,
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "System"
    )
    db.add(movement)
    db.commit()
    db.refresh(source_stock)
    db.refresh(dest_stock)
    db.refresh(movement)
    return movement, source_stock, dest_stock


def adjust_stock(
    db: Session,
    warehouse_id: int,
    catalog_id: int,
    quantity_delta: int,
    notes: str,
    actor: Optional[User] = None,
    caller_market: Optional[str] = None
) -> Tuple[StockMovement, WarehouseStock]:
    if quantity_delta == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stock adjustment quantity delta cannot be zero"
        )
    if not notes or len(notes.strip()) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A detailed justification note is required for stock adjustments"
        )

    wh = get_warehouse(db, warehouse_id)
    validate_market_access(caller_market, wh.market_id, actor)
    get_catalog(db, catalog_id)
    stock = get_or_create_warehouse_stock(db, warehouse_id, catalog_id)

    new_on_hand = stock.quantity_on_hand + quantity_delta
    if new_on_hand < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Adjustment would result in negative stock ({new_on_hand}). Current on-hand: {stock.quantity_on_hand}"
        )
    if new_on_hand < stock.quantity_reserved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Adjustment would reduce stock below currently reserved quantity ({stock.quantity_reserved})"
        )

    stock.quantity_on_hand = new_on_hand
    stock.updated_at = datetime.utcnow()

    movement = StockMovement(
        movement_code=generate_movement_code("ADJ"),
        movement_type="ADJUSTMENT",
        catalog_id=catalog_id,
        from_warehouse_id=warehouse_id if quantity_delta < 0 else None,
        to_warehouse_id=warehouse_id if quantity_delta > 0 else None,
        quantity=abs(quantity_delta),
        reference_type="MANUAL_ADJUSTMENT",
        notes=notes.strip(),
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "System"
    )
    db.add(movement)
    db.commit()
    db.refresh(stock)
    db.refresh(movement)
    return movement, stock


# ---------------------------------------------------------------------------
# Individual Serialized Device Operations
# ---------------------------------------------------------------------------

def list_devices(
    db: Session,
    warehouse_id: Optional[int] = None,
    catalog_id: Optional[int] = None,
    market_id: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100
) -> List[InventoryDevice]:
    query = db.query(InventoryDevice)
    if warehouse_id:
        query = query.filter(InventoryDevice.warehouse_id == warehouse_id)
    if catalog_id:
        query = query.filter(InventoryDevice.catalog_id == catalog_id)
    if market_id:
        query = query.filter(InventoryDevice.market_id == market_id)
    if status:
        query = query.filter(InventoryDevice.status == status)
    if search:
        s = f"%{search.strip()}%"
        query = query.filter(
            or_(
                InventoryDevice.serial_number.ilike(s),
                InventoryDevice.mac_address.ilike(s)
            )
        )
    return query.order_by(InventoryDevice.id.desc()).offset(skip).limit(limit).all()


def get_device_by_serial(db: Session, serial_number: str) -> InventoryDevice:
    device = db.query(InventoryDevice).filter(
        InventoryDevice.serial_number == serial_number.strip()
    ).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with serial number '{serial_number}' not found"
        )
    return device


def create_device(
    db: Session,
    device_in: Any,
    actor: Optional[User] = None,
    caller_market: Optional[str] = None
) -> InventoryDevice:
    existing_sn = db.query(InventoryDevice).filter(
        InventoryDevice.serial_number == device_in.serial_number.strip()
    ).first()
    if existing_sn:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device with serial number '{device_in.serial_number}' already exists"
        )
    if device_in.mac_address:
        existing_mac = db.query(InventoryDevice).filter(
            InventoryDevice.mac_address == device_in.mac_address.strip().upper()
        ).first()
        if existing_mac:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Device with MAC address '{device_in.mac_address}' already exists"
            )

    wh = get_warehouse(db, device_in.warehouse_id)
    validate_market_access(caller_market, wh.market_id, actor)
    if device_in.market_id:
        validate_market_access(caller_market, device_in.market_id, actor)
    get_catalog(db, device_in.catalog_id)

    device = InventoryDevice(
        serial_number=device_in.serial_number.strip(),
        mac_address=device_in.mac_address.strip().upper() if device_in.mac_address else None,
        catalog_id=device_in.catalog_id,
        warehouse_id=device_in.warehouse_id,
        market_id=device_in.market_id,
        status="IN_STOCK",
        warranty_expires_at=device_in.warranty_expires_at,
        notes=device_in.notes
    )
    db.add(device)

    # Increment stock
    stock = get_or_create_warehouse_stock(db, device_in.warehouse_id, device_in.catalog_id)
    stock.quantity_on_hand += 1
    stock.updated_at = datetime.utcnow()

    # Ledger movement
    movement = StockMovement(
        movement_code=generate_movement_code("REC"),
        movement_type="RECEIPT",
        catalog_id=device_in.catalog_id,
        to_warehouse_id=device_in.warehouse_id,
        quantity=1,
        serial_numbers=[device.serial_number],
        reference_type="INDIVIDUAL_DEVICE_REGISTRATION",
        notes=device_in.notes,
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "System"
    )
    db.add(movement)
    db.commit()
    db.refresh(device)
    return device


# ---------------------------------------------------------------------------
# Provisioning Workflow Integration Hooks
# ---------------------------------------------------------------------------

def reserve_equipment_for_order(
    db: Session,
    order: ServiceActivationOrder,
    catalog_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    serial_number: Optional[str] = None,
    actor: Optional[User] = None
) -> Optional[InventoryDevice]:
    """
    Called upon provisioning order creation.
    Reserves an available device for the activation order and customer.
    Increments quantity_reserved on the corresponding WarehouseStock atomically with
    oversubscription protection.
    """
    # Check if a device is already reserved or installed for this order
    existing = db.query(InventoryDevice).filter(
        InventoryDevice.assigned_order_id == order.id,
        InventoryDevice.status.in_(["RESERVED", "INSTALLED"])
    ).first()
    if existing:
        return existing

    candidate: Optional[InventoryDevice] = None

    if serial_number:
        candidate = db.query(InventoryDevice).filter(
            InventoryDevice.serial_number == serial_number.strip(),
            InventoryDevice.status == "IN_STOCK"
        ).first()
        if not candidate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Requested device serial '{serial_number}' is not available IN_STOCK"
            )
    else:
        # Search for available device by catalog and/or market
        query = db.query(InventoryDevice).filter(
            InventoryDevice.status == "IN_STOCK"
        )
        if catalog_id:
            query = query.filter(InventoryDevice.catalog_id == catalog_id)
        if warehouse_id:
            query = query.filter(InventoryDevice.warehouse_id == warehouse_id)
        elif order.market_id:
            query = query.filter(InventoryDevice.market_id == order.market_id)

        candidate = query.order_by(InventoryDevice.id.asc()).first()

    if not candidate:
        return None

    # Oversubscription protection: atomically increment quantity_reserved ONLY if available stock >= 1
    stock: Optional[WarehouseStock] = None
    if candidate.warehouse_id:
        stock = get_or_create_warehouse_stock(db, candidate.warehouse_id, candidate.catalog_id)
        stock_updated = db.query(WarehouseStock).filter(
            WarehouseStock.id == stock.id,
            (WarehouseStock.quantity_on_hand - WarehouseStock.quantity_reserved) >= 1
        ).update(
            {
                "quantity_reserved": WarehouseStock.quantity_reserved + 1,
                "updated_at": datetime.utcnow()
            },
            synchronize_session=False
        )
        if stock_updated == 0:
            return None

    # Atomically claim device
    claimed = db.query(InventoryDevice).filter(
        InventoryDevice.id == candidate.id,
        InventoryDevice.status == "IN_STOCK"
    ).update(
        {
            "status": "RESERVED",
            "assigned_order_id": order.id,
            "assigned_customer_id": order.customer_id,
            "updated_at": datetime.utcnow()
        },
        synchronize_session=False
    )
    if claimed == 0:
        # Concurrently claimed by another worker! Roll back stock reserved increment
        if candidate.warehouse_id and stock:
            db.query(WarehouseStock).filter(
                WarehouseStock.id == stock.id,
                WarehouseStock.quantity_reserved > 0
            ).update(
                {
                    "quantity_reserved": WarehouseStock.quantity_reserved - 1,
                    "updated_at": datetime.utcnow()
                },
                synchronize_session=False
            )
            db.commit()
        return None

    db.refresh(candidate)

    # Record Movement
    movement = StockMovement(
        movement_code=generate_movement_code("RES"),
        movement_type="RESERVATION",
        catalog_id=candidate.catalog_id,
        from_warehouse_id=candidate.warehouse_id,
        quantity=1,
        serial_numbers=[candidate.serial_number],
        reference_type="PROVISIONING_ORDER",
        reference_id=str(order.order_code if hasattr(order, 'order_code') else order.id),
        notes=f"Reserved for order {order.id}, customer {order.customer_id}",
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "Provisioning System"
    )
    db.add(movement)
    db.commit()
    db.refresh(candidate)
    return candidate


def release_reservation_for_order(
    db: Session,
    order: ServiceActivationOrder,
    actor: Optional[User] = None
) -> List[InventoryDevice]:
    """
    Called upon provisioning order cancellation or failure.
    Releases all reserved devices back to IN_STOCK and decrements quantity_reserved.
    """
    devices = db.query(InventoryDevice).filter(
        InventoryDevice.assigned_order_id == order.id,
        InventoryDevice.status == "RESERVED"
    ).all()

    released: List[InventoryDevice] = []
    for device in devices:
        updated = db.query(InventoryDevice).filter(
            InventoryDevice.id == device.id,
            InventoryDevice.status == "RESERVED"
        ).update(
            {
                "status": "IN_STOCK",
                "assigned_order_id": None,
                "assigned_customer_id": None,
                "updated_at": datetime.utcnow()
            },
            synchronize_session=False
        )
        if updated > 0:
            if device.warehouse_id:
                db.query(WarehouseStock).filter(
                    WarehouseStock.warehouse_id == device.warehouse_id,
                    WarehouseStock.catalog_id == device.catalog_id,
                    WarehouseStock.quantity_reserved > 0
                ).update(
                    {
                        "quantity_reserved": WarehouseStock.quantity_reserved - 1,
                        "updated_at": datetime.utcnow()
                    },
                    synchronize_session=False
                )

            movement = StockMovement(
                movement_code=generate_movement_code("REL"),
                movement_type="RELEASE",
                catalog_id=device.catalog_id,
                to_warehouse_id=device.warehouse_id,
                quantity=1,
                serial_numbers=[device.serial_number],
                reference_type="PROVISIONING_ORDER",
                reference_id=str(order.order_code if hasattr(order, 'order_code') else order.id),
                notes=f"Released reservation from cancelled order {order.id}",
                actor_id=actor.id if actor else None,
                actor_name=actor.full_name if actor else "Provisioning System"
            )
            db.add(movement)
            db.refresh(device)
            released.append(device)

    db.commit()
    return released


def consume_and_install_device_for_order(
    db: Session,
    order: ServiceActivationOrder,
    actor: Optional[User] = None
) -> Optional[InventoryDevice]:
    """
    Called upon successful provisioning activation.
    Transitions device status to INSTALLED, decrements both quantity_on_hand and quantity_reserved.
    """
    device = db.query(InventoryDevice).filter(
        InventoryDevice.assigned_order_id == order.id,
        InventoryDevice.status.in_(["RESERVED", "IN_STOCK"])
    ).first()

    if not device:
        return None

    was_reserved = (device.status == "RESERVED")
    updated = db.query(InventoryDevice).filter(
        InventoryDevice.id == device.id,
        InventoryDevice.status.in_(["RESERVED", "IN_STOCK"])
    ).update(
        {
            "status": "INSTALLED",
            "installed_at": datetime.utcnow(),
            "assigned_customer_id": order.customer_id,
            "updated_at": datetime.utcnow()
        },
        synchronize_session=False
    )
    if updated == 0:
        return None

    if device.warehouse_id:
        if was_reserved:
            db.query(WarehouseStock).filter(
                WarehouseStock.warehouse_id == device.warehouse_id,
                WarehouseStock.catalog_id == device.catalog_id,
                WarehouseStock.quantity_reserved > 0,
                WarehouseStock.quantity_on_hand > 0
            ).update(
                {
                    "quantity_reserved": WarehouseStock.quantity_reserved - 1,
                    "quantity_on_hand": WarehouseStock.quantity_on_hand - 1,
                    "updated_at": datetime.utcnow()
                },
                synchronize_session=False
            )
        else:
            db.query(WarehouseStock).filter(
                WarehouseStock.warehouse_id == device.warehouse_id,
                WarehouseStock.catalog_id == device.catalog_id,
                WarehouseStock.quantity_on_hand > 0
            ).update(
                {
                    "quantity_on_hand": WarehouseStock.quantity_on_hand - 1,
                    "updated_at": datetime.utcnow()
                },
                synchronize_session=False
            )

    movement = StockMovement(
        movement_code=generate_movement_code("CON"),
        movement_type="CONSUMPTION",
        catalog_id=device.catalog_id,
        from_warehouse_id=device.warehouse_id,
        quantity=1,
        serial_numbers=[device.serial_number],
        reference_type="PROVISIONING_ORDER",
        reference_id=str(order.order_code if hasattr(order, 'order_code') else order.id),
        notes=f"Installed and active for customer {order.customer_id}",
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "Provisioning System"
    )
    db.add(movement)
    db.commit()
    db.refresh(device)
    return device


# ---------------------------------------------------------------------------
# Returns and Replacements
# ---------------------------------------------------------------------------

def process_device_return(
    db: Session,
    serial_number: str,
    warehouse_id: int,
    condition: str = "WORKING",
    notes: Optional[str] = None,
    actor: Optional[User] = None,
    caller_market: Optional[str] = None
) -> Tuple[StockMovement, InventoryDevice]:
    device = get_device_by_serial(db, serial_number)
    warehouse = get_warehouse(db, warehouse_id)
    validate_market_access(caller_market, warehouse.market_id, actor)
    if device.market_id:
        validate_market_access(caller_market, device.market_id, actor)

    condition_upper = condition.upper().strip()
    new_status = "IN_STOCK" if condition_upper == "WORKING" else "DAMAGED"

    prev_status = device.status
    device.status = new_status
    device.returned_at = datetime.utcnow()
    device.warehouse_id = warehouse_id
    device.market_id = warehouse.market_id
    device.updated_at = datetime.utcnow()

    # If working, return to available on_hand stock
    if new_status == "IN_STOCK":
        stock = get_or_create_warehouse_stock(db, warehouse_id, device.catalog_id)
        stock.quantity_on_hand += 1
        stock.updated_at = datetime.utcnow()

    movement = StockMovement(
        movement_code=generate_movement_code("RET"),
        movement_type="RETURN",
        catalog_id=device.catalog_id,
        to_warehouse_id=warehouse_id,
        quantity=1,
        serial_numbers=[device.serial_number],
        reference_type="DEVICE_RETURN",
        reference_id=device.serial_number,
        notes=f"Condition: {condition_upper}. Prev status: {prev_status}. {notes or ''}".strip(),
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "System"
    )
    db.add(movement)
    db.commit()
    db.refresh(device)
    db.refresh(movement)
    return movement, device


def replace_device_for_ticket(
    db: Session,
    old_serial_number: str,
    new_serial_number: str,
    ticket_id: Optional[int] = None,
    order_id: Optional[int] = None,
    notes: Optional[str] = None,
    actor: Optional[User] = None,
    caller_market: Optional[str] = None
) -> Dict[str, Any]:
    old_dev = get_device_by_serial(db, old_serial_number)
    new_dev = get_device_by_serial(db, new_serial_number)

    validate_market_access(caller_market, old_dev.market_id, actor)
    validate_market_access(caller_market, new_dev.market_id, actor)

    if old_dev.status not in ["INSTALLED", "ASSIGNED"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot replace device '{old_serial_number}' with status '{old_dev.status}'. Only INSTALLED or ASSIGNED devices can be replaced."
        )

    if new_dev.status != "IN_STOCK":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Replacement device '{new_serial_number}' is not available IN_STOCK (current status: {new_dev.status})"
        )

    customer_id = old_dev.assigned_customer_id

    # 1. Mark old device DAMAGED
    old_dev.status = "DAMAGED"
    old_dev.returned_at = datetime.utcnow()
    old_dev.notes = f"Replaced by {new_serial_number}. {notes or ''}".strip()
    old_dev.updated_at = datetime.utcnow()

    # 2. Mark new device INSTALLED atomically to prevent concurrent replacement collisions
    new_wh_id = new_dev.warehouse_id
    new_catalog_id = new_dev.catalog_id
    assigned_order = order_id if order_id is not None else old_dev.assigned_order_id

    claimed = db.query(InventoryDevice).filter(
        InventoryDevice.id == new_dev.id,
        InventoryDevice.status == "IN_STOCK"
    ).update(
        {
            "status": "INSTALLED",
            "installed_at": datetime.utcnow(),
            "assigned_customer_id": customer_id,
            "assigned_order_id": assigned_order,
            "updated_at": datetime.utcnow()
        },
        synchronize_session=False
    )
    if claimed == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Replacement device '{new_serial_number}' is no longer IN_STOCK (concurrently assigned)."
        )

    # Decrement stock for new device
    if new_wh_id:
        stock = get_or_create_warehouse_stock(db, new_wh_id, new_catalog_id)
        stock.quantity_on_hand = max(0, stock.quantity_on_hand - 1)
        stock.updated_at = datetime.utcnow()

    # Audit movement
    movement = StockMovement(
        movement_code=generate_movement_code("RPL"),
        movement_type="REPLACEMENT",
        catalog_id=new_catalog_id,
        quantity=1,
        serial_numbers=[f"OUT:{old_serial_number}", f"IN:{new_serial_number}"],
        reference_type="TICKET_REPLACEMENT",
        reference_id=str(ticket_id) if ticket_id else str(order_id or ""),
        notes=f"Replaced faulty device {old_serial_number} with {new_serial_number}. {notes or ''}".strip(),
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "Support Technician"
    )
    db.add(movement)
    db.commit()
    db.refresh(old_dev)
    db.refresh(new_dev)

    return {
        "success": True,
        "old_device": old_dev.serial_number,
        "new_device": new_dev.serial_number,
        "customer_id": customer_id,
        "ticket_id": ticket_id,
        "movement_code": movement.movement_code
    }


# ---------------------------------------------------------------------------
# Aggregated Metrics & Stats
# ---------------------------------------------------------------------------

def get_inventory_stats(db: Session, market_id: Optional[str] = None) -> Dict[str, Any]:
    query_dev = db.query(InventoryDevice)
    if market_id:
        query_dev = query_dev.filter(InventoryDevice.market_id == market_id)

    total_devices = query_dev.count()
    devices_in_stock = query_dev.filter(InventoryDevice.status == "IN_STOCK").count()
    devices_installed = query_dev.filter(InventoryDevice.status == "INSTALLED").count()
    devices_reserved = query_dev.filter(InventoryDevice.status == "RESERVED").count()
    devices_damaged = query_dev.filter(InventoryDevice.status == "DAMAGED").count()
    devices_returned = query_dev.filter(InventoryDevice.status == "RETURNED").count()

    query_wh = db.query(Warehouse).filter(Warehouse.is_active == True)
    if market_id:
        query_wh = query_wh.filter(Warehouse.market_id == market_id)
    total_warehouses = query_wh.count()

    query_low = db.query(WarehouseStock).join(Warehouse).filter(
        WarehouseStock.quantity_on_hand <= WarehouseStock.min_threshold
    )
    if market_id:
        query_low = query_low.filter(Warehouse.market_id == market_id)
    low_stock_items_count = query_low.count()

    return {
        "total_devices": total_devices,
        "devices_in_stock": devices_in_stock,
        "devices_installed": devices_installed,
        "devices_reserved": devices_reserved,
        "devices_damaged": devices_damaged,
        "devices_returned": devices_returned,
        "total_warehouses": total_warehouses,
        "low_stock_items_count": low_stock_items_count
    }


def list_movements(
    db: Session,
    catalog_id: Optional[int] = None,
    warehouse_id: Optional[int] = None,
    movement_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 100
) -> List[StockMovement]:
    query = db.query(StockMovement)
    if catalog_id:
        query = query.filter(StockMovement.catalog_id == catalog_id)
    if warehouse_id:
        query = query.filter(
            or_(
                StockMovement.from_warehouse_id == warehouse_id,
                StockMovement.to_warehouse_id == warehouse_id
            )
        )
    if movement_type:
        query = query.filter(StockMovement.movement_type == movement_type)
    return query.order_by(StockMovement.id.desc()).offset(skip).limit(limit).all()
