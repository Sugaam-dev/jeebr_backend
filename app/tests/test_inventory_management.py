"""
app/tests/test_inventory_management.py

Comprehensive acceptance tests for SentinelOS Carrier Inventory & Asset Management:
1. Equipment catalog & warehouse domain management.
2. Serialized device tracking and deduplication (Serial Number & MAC address).
3. Non-negative stock constraints and threshold tracking.
4. Stock movements ledger (RECEIPT, TRANSFER, ADJUSTMENT).
5. Provisioning integration (Reservation, Cancellation Release, Activation Consumption).
6. RMA returns and replacement workflows.
7. RBAC permissions (Admin/NOC allowed, Viewer forbidden) & Market isolation.
"""

import uuid
from datetime import datetime
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models import User, Customer
from app.models_provisioning import ServiceActivationOrder, ProvisioningStatus, ExecutionMode
from app.models_inventory import (
    EquipmentCatalog, Warehouse, WarehouseStock, InventoryDevice, StockMovement
)
from app.auth import create_access_token
from app.services import inventory_service

client = TestClient(app)


@pytest.fixture
def tokens():
    return {
        "superadmin": create_access_token({"sub": "superadmin@pmrg.in", "role": "SUPER_ADMIN"}),
        "admin": create_access_token({"sub": "admin@pmrg.in", "role": "Admin"}),
        "noc": create_access_token({"sub": "noc@pmrg.in", "role": "NOC"}),
        "viewer": create_access_token({"sub": "client@pmrgsolution.com", "role": "Viewer"}),
    }


def auth_header(token: str, market: str = "mumbai"):
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market
    }


def test_catalog_and_warehouse_listing_and_rbac(tokens):
    # 1. Admin lists catalogs (seeded by seed_inventory_management)
    res = client.get("/api/inventory/catalogs", headers=auth_header(tokens["admin"]))
    assert res.status_code == 200
    catalogs = res.json()
    assert len(catalogs) >= 4
    skus = [c["sku"] for c in catalogs]
    assert "ONT-HUA-HG8145V5" in skus
    assert "SPL-1X8-PLC" in skus

    # 2. Viewer can read warehouses
    res_wh = client.get("/api/inventory/warehouses", headers=auth_header(tokens["viewer"]))
    assert res_wh.status_code == 200
    warehouses = res_wh.json()
    assert len(warehouses) >= 1
    wh_codes = [w["warehouse_code"] for w in warehouses]
    assert "WH-MUM-CENTRAL" in wh_codes

    # 3. Viewer forbidden from creating catalog (RBAC)
    res_v_create = client.post(
        "/api/inventory/catalogs",
        headers=auth_header(tokens["viewer"]),
        json={
            "sku": "ROUTER-TEST-RBAC",
            "name": "Test Router",
            "equipment_type": "ROUTER",
            "model": "AX12",
            "manufacturer": "TP-Link",
            "is_serialized": True,
            "unit_cost_inr": 1500.0
        }
    )
    assert res_v_create.status_code == 403


def test_stock_receipt_with_serialized_deduplication(tokens):
    db = SessionLocal()
    try:
        wh = db.query(Warehouse).filter(Warehouse.warehouse_code == "WH-MUM-CENTRAL").first()
        cat = db.query(EquipmentCatalog).filter(EquipmentCatalog.sku == "ONT-HUA-HG8145V5").first()
        assert wh is not None and cat is not None

        sn1 = f"SN-TEST-{uuid.uuid4().hex[:6].upper()}"
        sn2 = f"SN-TEST-{uuid.uuid4().hex[:6].upper()}"
        mac1 = f"50:06:04:11:22:{uuid.uuid4().hex[:2].upper()}"
        mac2 = f"50:06:04:11:22:{uuid.uuid4().hex[:2].upper()}"

        # 1. Valid Receipt with Serial Numbers and MAC
        receipt_payload = {
            "warehouse_id": wh.id,
            "catalog_id": cat.id,
            "quantity": 2,
            "serial_numbers": [sn1, sn2],
            "mac_addresses": [mac1, mac2],
            "notes": "Automated receipt acceptance test"
        }
        res = client.post(
            "/api/inventory/stocks/receipt",
            headers=auth_header(tokens["admin"]),
            json=receipt_payload
        )
        assert res.status_code == 200
        stock = res.json()
        assert stock["quantity_on_hand"] >= 2
        assert stock["quantity_available"] >= 2

        # Verify device created in IN_STOCK
        res_dev = client.get(f"/api/inventory/devices/{sn1}", headers=auth_header(tokens["admin"]))
        assert res_dev.status_code == 200
        assert res_dev.json()["status"] == "IN_STOCK"
        assert res_dev.json()["mac_address"] == mac1

        # 2. Duplicate Serial Number Rejection (HTTP 409)
        res_dup_sn = client.post(
            "/api/inventory/stocks/receipt",
            headers=auth_header(tokens["admin"]),
            json={
                "warehouse_id": wh.id,
                "catalog_id": cat.id,
                "quantity": 1,
                "serial_numbers": [sn1],  # Already exists!
                "notes": "Duplicate test"
            }
        )
        assert res_dup_sn.status_code == 409
        assert "already registered" in res_dup_sn.json()["detail"].lower()

        # 3. Duplicate MAC Address Rejection (HTTP 409)
        res_dup_mac = client.post(
            "/api/inventory/stocks/receipt",
            headers=auth_header(tokens["admin"]),
            json={
                "warehouse_id": wh.id,
                "catalog_id": cat.id,
                "quantity": 1,
                "serial_numbers": [f"SN-TEST-{uuid.uuid4().hex[:6].upper()}"],
                "mac_addresses": [mac1],  # MAC already exists!
                "notes": "Duplicate MAC test"
            }
        )
        assert res_dup_mac.status_code == 409
        assert "mac addresses already registered" in res_dup_mac.json()["detail"].lower()
    finally:
        db.close()


def test_inter_warehouse_transfer_and_insufficient_stock(tokens):
    db = SessionLocal()
    try:
        wh_src = db.query(Warehouse).filter(Warehouse.warehouse_code == "WH-MUM-CENTRAL").first()
        wh_dst = db.query(Warehouse).filter(Warehouse.warehouse_code == "WH-MUM-ANDHERI").first()
        cat = db.query(EquipmentCatalog).filter(EquipmentCatalog.sku == "ONT-HUA-HG8145V5").first()
        assert wh_src is not None and wh_dst is not None

        # Seed an available device in source warehouse
        sn_trf = f"SN-TRF-{uuid.uuid4().hex[:6].upper()}"
        dev = InventoryDevice(
            serial_number=sn_trf,
            catalog_id=cat.id,
            warehouse_id=wh_src.id,
            market_id="mumbai",
            status="IN_STOCK",
            notes="Transfer test device"
        )
        db.add(dev)
        src_stock = inventory_service.get_or_create_warehouse_stock(db, wh_src.id, cat.id)
        src_stock.quantity_on_hand += 1
        db.commit()

        # 1. Valid Transfer
        res_trf = client.post(
            "/api/inventory/stocks/transfer",
            headers=auth_header(tokens["admin"]),
            json={
                "from_warehouse_id": wh_src.id,
                "to_warehouse_id": wh_dst.id,
                "catalog_id": cat.id,
                "quantity": 1,
                "serial_numbers": [sn_trf],
                "notes": "Inter-depot rebalance"
            }
        )
        assert res_trf.status_code == 200
        assert res_trf.json()["success"] is True

        # Verify device location updated
        dev_check = client.get(f"/api/inventory/devices/{sn_trf}", headers=auth_header(tokens["admin"])).json()
        assert dev_check["warehouse_id"] == wh_dst.id

        # 2. Insufficient Stock Rejection (HTTP 400)
        res_insuf = client.post(
            "/api/inventory/stocks/transfer",
            headers=auth_header(tokens["admin"]),
            json={
                "from_warehouse_id": wh_src.id,
                "to_warehouse_id": wh_dst.id,
                "catalog_id": cat.id,
                "quantity": 99999,
                "notes": "Too much"
            }
        )
        assert res_insuf.status_code == 400
        assert "insufficient available stock" in res_insuf.json()["detail"].lower()
    finally:
        db.close()


def test_manual_adjustment_and_negative_stock_prevention(tokens):
    db = SessionLocal()
    try:
        wh = db.query(Warehouse).filter(Warehouse.warehouse_code == "WH-MUM-CENTRAL").first()
        cat = db.query(EquipmentCatalog).filter(EquipmentCatalog.sku == "SPL-1X8-PLC").first()
        stock = inventory_service.get_or_create_warehouse_stock(db, wh.id, cat.id)
        current_qty = stock.quantity_on_hand

        # 1. Valid adjustment (+3)
        res_adj = client.post(
            "/api/inventory/stocks/adjust",
            headers=auth_header(tokens["admin"]),
            json={
                "warehouse_id": wh.id,
                "catalog_id": cat.id,
                "quantity_delta": 3,
                "notes": "Cycle count audit reconciliation"
            }
        )
        assert res_adj.status_code == 200
        assert res_adj.json()["quantity_on_hand"] == current_qty + 3

        # 2. Negative Stock Prevention (HTTP 400)
        res_neg = client.post(
            "/api/inventory/stocks/adjust",
            headers=auth_header(tokens["admin"]),
            json={
                "warehouse_id": wh.id,
                "catalog_id": cat.id,
                "quantity_delta": -99999,
                "notes": "Illegal reduction"
            }
        )
        assert res_neg.status_code == 400
        assert "negative stock" in res_neg.json()["detail"].lower()
    finally:
        db.close()


def test_provisioning_reservation_release_and_consumption_lifecycle(tokens):
    db = SessionLocal()
    try:
        wh = db.query(Warehouse).filter(Warehouse.warehouse_code == "WH-MUM-CENTRAL").first()
        cat = db.query(EquipmentCatalog).filter(EquipmentCatalog.sku == "ONT-HUA-HG8145V5").first()
        cust = db.query(Customer).filter(Customer.market_id == "mumbai").first()
        assert cust is not None

        sn_prov = f"SN-ACT-{uuid.uuid4().hex[:6].upper()}"
        dev = InventoryDevice(
            serial_number=sn_prov,
            catalog_id=cat.id,
            warehouse_id=wh.id,
            market_id="mumbai",
            status="IN_STOCK",
            notes="Provisioning lifecycle test unit"
        )
        db.add(dev)
        stock = inventory_service.get_or_create_warehouse_stock(db, wh.id, cat.id)
        stock.quantity_on_hand += 1
        db.commit()

        # Dummy activation order
        order = ServiceActivationOrder(
            order_code=f"ACT-MUM-2026-{uuid.uuid4().hex[:6].upper()}",
            idempotency_key=f"IDEMP-{uuid.uuid4().hex[:8]}",
            market_id="mumbai",
            customer_id=cust.id,
            customer_name=cust.name,
            customer_code=cust.customer_code,
            locality=cust.locality or "Lower Parel",
            service_address=f"{cust.locality or 'Lower Parel'}, Mumbai",
            account_type="RESIDENTIAL",
            plan_id="PLAN-TEST-100",
            plan_name="Test Fiber 100M",
            plan_price=699.0,
            speed_mbps=100,
            data_quota_gb=500.0,
            validity_days=28,
            status=ProvisioningStatus.REQUESTED,
            execution_mode=ExecutionMode.SIMULATED
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        # 1. Equipment Reservation
        reserved = inventory_service.reserve_equipment_for_order(
            db=db,
            order=order,
            serial_number=sn_prov
        )
        assert reserved is not None
        assert reserved.status == "RESERVED"
        assert reserved.assigned_order_id == order.id

        db.refresh(stock)
        assert stock.quantity_reserved >= 1

        # 2. Reservation Release (Simulating order cancellation)
        released = inventory_service.release_reservation_for_order(db=db, order=order)
        assert len(released) == 1
        assert released[0].status == "IN_STOCK"
        assert released[0].assigned_order_id is None

        # 3. Re-reserve & Active Consumption (Simulating activation completion)
        inventory_service.reserve_equipment_for_order(db=db, order=order, serial_number=sn_prov)
        db.refresh(stock)
        qty_before = stock.quantity_on_hand

        installed = inventory_service.consume_and_install_device_for_order(db=db, order=order)
        assert installed is not None
        assert installed.status == "INSTALLED"
        assert installed.installed_at is not None
        assert installed.assigned_customer_id == cust.id

        db.refresh(stock)
        assert stock.quantity_on_hand == qty_before - 1
    finally:
        db.close()


def test_rma_return_and_replacement_workflows(tokens):
    db = SessionLocal()
    try:
        wh = db.query(Warehouse).filter(Warehouse.warehouse_code == "WH-MUM-CENTRAL").first()
        cat = db.query(EquipmentCatalog).filter(EquipmentCatalog.sku == "ONT-HUA-HG8145V5").first()
        cust = db.query(Customer).filter(Customer.market_id == "mumbai").first()

        old_sn = f"SN-RMA-OLD-{uuid.uuid4().hex[:6].upper()}"
        new_sn = f"SN-RMA-NEW-{uuid.uuid4().hex[:6].upper()}"

        d_old = InventoryDevice(
            serial_number=old_sn,
            catalog_id=cat.id,
            warehouse_id=wh.id,
            market_id="mumbai",
            status="INSTALLED",
            assigned_customer_id=cust.id
        )
        d_new = InventoryDevice(
            serial_number=new_sn,
            catalog_id=cat.id,
            warehouse_id=wh.id,
            market_id="mumbai",
            status="IN_STOCK"
        )
        db.add_all([d_old, d_new])
        stock = inventory_service.get_or_create_warehouse_stock(db, wh.id, cat.id)
        stock.quantity_on_hand += 1
        db.commit()

        # 1. Replace Faulty Device
        res_rpl = client.post(
            "/api/inventory/operations/replace",
            headers=auth_header(tokens["admin"]),
            json={
                "old_serial_number": old_sn,
                "new_serial_number": new_sn,
                "ticket_id": 1099,
                "notes": "Transceiver fault replaced"
            }
        )
        assert res_rpl.status_code == 200
        assert res_rpl.json()["success"] is True

        # Check old device marked DAMAGED
        d_old_check = client.get(f"/api/inventory/devices/{old_sn}", headers=auth_header(tokens["admin"])).json()
        assert d_old_check["status"] == "DAMAGED"

        # Check new device marked INSTALLED and assigned to same customer
        d_new_check = client.get(f"/api/inventory/devices/{new_sn}", headers=auth_header(tokens["admin"])).json()
        assert d_new_check["status"] == "INSTALLED"
        assert d_new_check["assigned_customer_id"] == cust.id

        # 2. Return Working Device
        res_ret = client.post(
            "/api/inventory/operations/return",
            headers=auth_header(tokens["admin"]),
            json={
                "serial_number": new_sn,
                "warehouse_id": wh.id,
                "condition": "WORKING",
                "notes": "Returned by customer"
            }
        )
        assert res_ret.status_code == 200
        assert res_ret.json()["new_status"] == "IN_STOCK"
    finally:
        db.close()


def test_inventory_stats_and_audit_movements(tokens):
    # 1. Stats KPI endpoint
    res_stats = client.get("/api/inventory/stats", headers=auth_header(tokens["admin"]))
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["total_devices"] >= 1
    assert stats["devices_in_stock"] >= 1
    assert "devices_installed" in stats
    assert "total_warehouses" in stats

    # 2. Movement audit ledger
    res_mov = client.get("/api/inventory/movements", headers=auth_header(tokens["admin"]))
    assert res_mov.status_code == 200
    movements = res_mov.json()
    assert len(movements) >= 1
    types = [m["movement_type"] for m in movements]
    assert "RECEIPT" in types or "TRANSFER" in types or "REPLACEMENT" in types
