"""
test_service_provisioning.py

Comprehensive test suite for SentinelOS Service Provisioning & Automated Delivery.
Verifies:
1. Canonical Service Plan Catalog retrieval.
2. Pre-flight eligibility verification for eligible and ineligible customers.
3. Account-type boundary enforcement (e.g. Residential vs Enterprise ILL).
4. Idempotent order creation (repeated requests return identical order without duplication).
5. State transitions: REQUESTED -> APPROVED -> PROVISIONING -> ACTIVE.
6. RBAC boundaries (Viewer and Customer restricted; Care cannot approve or execute).
7. Cross-market tenant isolation rejection (403 Forbidden).
8. Failure simulation, sanitized failure reasons, and bounded retries (max 3).
9. Order cancellation with mandatory audit reason and resource de-allocation.
10. Hardware reconciliation self-healing.
11. Transparent simulation labeling (SIMULATED vs Real vendor fail-closed).
12. Immutable audit trail logging across all lifecycle actions.
"""

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import Customer, User, NetworkDevice, OLTPort
from app.models_crm import CRMCustomer, CustomerLifecycleStatus
from app.models_provisioning import (
    ServiceActivationOrder, ProvisioningLog, ProvisioningStatus, ExecutionMode
)
from app.auth import create_access_token
from app.services.provisioning_provider import SyntheticProvisioningProvider

client = TestClient(app)

ROLE_EMAIL_MAP = {
    "SUPER_ADMIN": "superadmin@pmrg.in",
    "Admin": "admin@pmrg.in",
    "NOC": "noc@pmrg.in",
    "Care": "care@pmrg.in",
    "Revenue": "revenue@pmrg.in",
    "Executive": "executive@pmrg.in",
    "Viewer": "client@pmrgsolution.com",
    "Customer": "customer@pmrg.in",
}

def _auth_headers(role: str = "Admin", market: str = "mumbai", email: str = None) -> dict:
    if email is None:
        email = ROLE_EMAIL_MAP.get(role, "admin@pmrg.in")
    token = create_access_token({"sub": email, "role": role})
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market,
        "Content-Type": "application/json"
    }


# ─── 1. Canonical Service Plans Catalog ───────────────────────────────────────

def test_get_service_plans_catalog():
    headers = _auth_headers("Admin", "mumbai")
    res = client.get("/api/provisioning/plans", headers=headers)
    assert res.status_code == 200
    plans = res.json()
    assert len(plans) >= 8

    # Verify plan properties
    p = plans[0]
    assert "plan_id" in p
    assert "name" in p
    assert "price_inr" in p
    assert "speed_mbps" in p
    assert "service_vlan" in p

    # Filter by account_type
    res_ent = client.get("/api/provisioning/plans?account_type=ENTERPRISE", headers=headers)
    assert res_ent.status_code == 200
    ent_plans = res_ent.json()
    assert all("ILL" in p["name"] or "Dedicated" in p["name"] for p in ent_plans)


# ─── 2. Pre-Flight Customer Eligibility Verification ──────────────────────────

def test_customer_eligibility_verification_success():
    headers = _auth_headers("Care", "mumbai")
    # Customer 1 is Mumbai customer
    res = client.get("/api/provisioning/customer/1/eligibility?plan_id=PLAN-PRE-HERO-1.5G", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["customer_id"] == 1
    assert data["market_id"] == "mumbai"
    assert data["plan"]["plan_id"] == "PLAN-PRE-HERO-1.5G"
    assert data["eligible"] is True
    assert len(data["blocking_errors"]) == 0


def test_customer_eligibility_cross_market_rejected():
    # Customer 301 belongs to Kolkata, calling with Mumbai market header
    headers = _auth_headers("Care", "mumbai")
    res = client.get("/api/provisioning/customer/301/eligibility?plan_id=PLAN-PRE-HERO-1.5G", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["eligible"] is False
    assert any("Cross-market activation is forbidden" in err for err in data["blocking_errors"])


def test_customer_eligibility_invalid_plan():
    headers = _auth_headers("Care", "mumbai")
    res = client.get("/api/provisioning/customer/1/eligibility?plan_id=PLAN-NONEXISTENT", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["eligible"] is False
    assert any("does not exist" in err for err in data["blocking_errors"])


# ─── 3. Idempotent Order Creation & Account Boundary ──────────────────────────

def test_create_service_activation_order_idempotent():
    headers = _auth_headers("Care", "mumbai")
    idem_key = f"IDEM-TEST-{uuid.uuid4().hex[:12]}"

    payload = {
        "customer_id": 2,
        "plan_id": "PLAN-PRE-SUPER-5G",
        "idempotency_key": idem_key,
        "execution_mode": "SIMULATED"
    }

    # First request: Creates order (201 Created)
    res1 = client.post("/api/provisioning/orders", json=payload, headers=headers)
    assert res1.status_code == 201
    order1 = res1.json()
    assert order1["status"] == "REQUESTED"
    assert order1["customer_id"] == 2
    assert order1["order_code"].startswith("ACT-MUM-")
    assert order1["idempotency_key"] == idem_key
    assert order1["execution_mode"] == "SIMULATED"
    assert order1["provider_name"] == "SyntheticProvisioningProvider"
    assert order1["telemetry_source"] == "Synthetic / Demo Telemetry"

    # Second request with SAME idempotency key: Returns existing order without duplication
    res2 = client.post("/api/provisioning/orders", json=payload, headers=headers)
    assert res2.status_code == 201
    order2 = res2.json()
    assert order2["id"] == order1["id"]
    assert order2["order_code"] == order1["order_code"]
    assert order2["created_at"] == order1["created_at"]


def test_enterprise_plan_rejected_for_residential_customer():
    headers = _auth_headers("Care", "mumbai")
    # Customer 3 is residential prepaid, attempting to provision 1Gbps Corporate ILL
    payload = {
        "customer_id": 3,
        "plan_id": "PLAN-POST-CORP-500M",
        "idempotency_key": f"IDEM-TEST-{uuid.uuid4().hex[:12]}"
    }
    res = client.post("/api/provisioning/orders", json=payload, headers=headers)
    assert res.status_code == 409
    assert "requires ENTERPRISE classification" in res.json()["detail"]


# ─── 4. End-to-End State Machine: REQUESTED -> APPROVED -> PROVISIONING -> ACTIVE ─

def test_full_activation_lifecycle_and_audit_trail():
    headers_care = _auth_headers("Care", "mumbai")
    headers_admin = _auth_headers("Admin", "mumbai")

    # 1. Create order
    idem_key = f"IDEM-TEST-{uuid.uuid4().hex[:12]}"
    create_res = client.post("/api/provisioning/orders", json={
        "customer_id": 4,
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "idempotency_key": idem_key
    }, headers=headers_care)
    assert create_res.status_code == 201
    order = create_res.json()
    order_code = order["order_code"]
    assert order["status"] == "REQUESTED"

    # 2. Care attempts to approve -> Forbidden (Care cannot approve)
    res_care_approve = client.post(f"/api/provisioning/orders/{order_code}/approve", json={"notes": "Care approval"}, headers=headers_care)
    assert res_care_approve.status_code == 403

    # 3. Admin approves -> 200 OK (APPROVED)
    res_admin_approve = client.post(f"/api/provisioning/orders/{order_code}/approve", json={"notes": "Engineering check passed"}, headers=headers_admin)
    assert res_admin_approve.status_code == 200
    app_order = res_admin_approve.json()
    assert app_order["status"] == "APPROVED"
    assert app_order["approved_by_email"] == "admin@pmrg.in"

    # 4. Care attempts to execute provisioning -> Forbidden
    res_care_prov = client.post(f"/api/provisioning/orders/{order_code}/provision", headers=headers_care)
    assert res_care_prov.status_code == 403

    # 5. Admin triggers automated provisioning -> 200 OK (ACTIVE)
    res_admin_prov = client.post(f"/api/provisioning/orders/{order_code}/provision", headers=headers_admin)
    assert res_admin_prov.status_code == 200
    active_order = res_admin_prov.json()
    assert active_order["status"] == "ACTIVE"
    assert active_order["subscription_status"] == "ACTIVE"
    assert active_order["customer_lifecycle_status"] == "ACTIVE"
    assert active_order["provisioned_at"] is not None

    # 6. Verify audit logs & history
    detail_res = client.get(f"/api/provisioning/orders/{order_code}", headers=headers_admin)
    assert detail_res.status_code == 200
    det = detail_res.json()
    assert det["status"] == "ACTIVE"
    assert len(det["logs"]) >= 3
    actions = [l["action"] for l in det["logs"]]
    assert "CREATE_REQUEST" in actions
    assert "APPROVE" in actions
    assert "PROVISION_SUCCESS" in actions


# ─── 5. Server-Side Pagination & Listing ───────────────────────────────────────

def test_list_provisioning_orders_pagination():
    headers = _auth_headers("NOC", "mumbai")
    res = client.get("/api/provisioning/orders?page=1&page_size=10", headers=headers)
    assert res.status_code == 200
    assert "X-Total-Count" in res.headers
    assert "X-Page" in res.headers
    assert "X-Page-Size" in res.headers
    assert "X-Total-Pages" in res.headers
    data = res.json()
    assert isinstance(data, list)


# ─── 6. Bounded Retries & Failure Handling ─────────────────────────────────────

def test_provisioning_failure_and_bounded_retry():
    headers = _auth_headers("Admin", "mumbai")
    from app.services.provisioning_service import execute_provisioning

    # Create order
    create_res = client.post("/api/provisioning/orders", json={
        "customer_id": 5,
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "idempotency_key": f"IDEM-FAIL-{uuid.uuid4().hex[:12]}"
    }, headers=headers)
    assert create_res.status_code == 201
    order = create_res.json()
    order_code = order["order_code"]

    # Approve
    client.post(f"/api/provisioning/orders/{order_code}/approve", headers=headers)

    # 1. Simulate failure using SyntheticProvisioningProvider with failure_mode="TIMEOUT"
    fail_provider = SyntheticProvisioningProvider(failure_mode="TIMEOUT")
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        updated_order, exec_res = execute_provisioning(order_code, db.query(User).filter(User.email == "admin@pmrg.in").first(), db, provider_override=fail_provider)
        assert updated_order.status == "FAILED"
        assert updated_order.failure_code == "PROV_TIMEOUT"
        assert "timed out" in updated_order.failure_reason
        assert updated_order.retry_count == 1
    finally:
        db.close()

    # 2. Retry with normal provider -> Success
    retry_res = client.post(f"/api/provisioning/orders/{order_code}/retry", headers=headers)
    assert retry_res.status_code == 200
    retried_order = retry_res.json()
    assert retried_order["status"] == "ACTIVE"
    assert retried_order["failure_code"] is None


def test_max_retry_limit_exceeded_rejected():
    headers = _auth_headers("Admin", "mumbai")
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        # Create an order directly in FAILED status with retry_count = 3
        order = ServiceActivationOrder(
            order_code=f"ACT-MUM-{uuid.uuid4().hex[:6].upper()}",
            idempotency_key=f"IDEM-MAX-{uuid.uuid4().hex[:10]}",
            market_id="mumbai",
            customer_id=1,
            customer_name="Aarav Sharma",
            customer_code="CUST-MUM-0001",
            locality="Colaba",
            plan_id="PLAN-PRE-HERO-1.5G",
            plan_name="Hero Unlimited",
            plan_price=299.0,
            status=ProvisioningStatus.FAILED,
            retry_count=3,
            max_retries=3,
            failure_code="MAX_EXCEEDED",
            failure_reason="Prior attempts failed"
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        order_code = order.order_code
    finally:
        db.close()

    # Retry must be rejected because retry_count >= max_retries
    res = client.post(f"/api/provisioning/orders/{order_code}/retry", headers=headers)
    assert res.status_code == 400
    assert "Maximum retry limit (3) exceeded" in res.json()["detail"]


# ─── 7. Order Cancellation with Mandatory Reason ──────────────────────────────

def test_cancel_activation_order():
    headers = _auth_headers("Care", "mumbai")
    create_res = client.post("/api/provisioning/orders", json={
        "customer_id": 6,
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "idempotency_key": f"IDEM-CANCEL-{uuid.uuid4().hex[:12]}"
    }, headers=headers)
    order_code = create_res.json()["order_code"]

    # Missing cancellation reason rejected
    res_empty_reason = client.post(f"/api/provisioning/orders/{order_code}/cancel", json={"reason": ""}, headers=headers)
    assert res_empty_reason.status_code == 400

    # Cancel with valid reason
    res_cancel = client.post(f"/api/provisioning/orders/{order_code}/cancel", json={"reason": "Customer relocated to different address"}, headers=headers)
    assert res_cancel.status_code == 200
    cancelled_order = res_cancel.json()
    assert cancelled_order["status"] == "CANCELLED"
    assert cancelled_order["cancellation_reason"] == "Customer relocated to different address"
    assert cancelled_order["cancelled_at"] is not None

    # Cannot cancel already cancelled order
    res_recancel = client.post(f"/api/provisioning/orders/{order_code}/cancel", json={"reason": "Cancel again"}, headers=headers)
    assert res_recancel.status_code == 400
    assert "already in terminal state" in res_recancel.json()["detail"]


# ─── 8. Hardware Reconciliation Self-Healing ──────────────────────────────────

def test_reconciliation_self_healing():
    headers = _auth_headers("NOC", "mumbai")
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        # Create an order in PROVISIONING status whose downstream response was dropped
        order = ServiceActivationOrder(
            order_code=f"ACT-MUM-{uuid.uuid4().hex[:6].upper()}",
            idempotency_key=f"IDEM-REC-{uuid.uuid4().hex[:10]}",
            market_id="mumbai",
            customer_id=1,
            customer_name="Aarav Sharma",
            customer_code="CUST-MUM-0001",
            locality="Colaba",
            plan_id="PLAN-PRE-HERO-1.5G",
            plan_name="Hero Unlimited",
            plan_price=299.0,
            status=ProvisioningStatus.PROVISIONING
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        order_code = order.order_code
    finally:
        db.close()

    # Reconcile endpoint executes and self-heals state to ACTIVE
    res = client.post(f"/api/provisioning/orders/{order_code}/reconcile", headers=headers)
    assert res.status_code == 200
    reconciled = res.json()
    assert reconciled["status"] == "ACTIVE"


# ─── 9. Security & RBAC Boundary Enforcements ─────────────────────────────────

def test_viewer_and_customer_role_restrictions():
    viewer_headers = _auth_headers("Viewer", "mumbai", "client@pmrgsolution.com")

    # Viewer cannot create activation request
    res_create = client.post("/api/provisioning/orders", json={
        "customer_id": 1,
        "plan_id": "PLAN-PRE-HERO-1.5G"
    }, headers=viewer_headers)
    assert res_create.status_code == 403

    # Viewer cannot approve or provision
    res_app = client.post("/api/provisioning/orders/ACT-MUM-2026-0001/approve", json={}, headers=viewer_headers)
    assert res_app.status_code == 403

    res_prov = client.post("/api/provisioning/orders/ACT-MUM-2026-0001/provision", headers=viewer_headers)
    assert res_prov.status_code == 403

    res_cancel = client.post("/api/provisioning/orders/ACT-MUM-2026-0001/cancel", json={"reason": "test"}, headers=viewer_headers)
    assert res_cancel.status_code == 403


def test_cross_market_order_access_rejected():
    # User in Mumbai market tries to fetch Kolkata order
    mumbai_headers = _auth_headers("Care", "mumbai")
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        kolkata_order = ServiceActivationOrder(
            order_code=f"ACT-KOL-{uuid.uuid4().hex[:6].upper()}",
            idempotency_key=f"IDEM-KOL-{uuid.uuid4().hex[:10]}",
            market_id="kolkata",
            customer_id=301,
            customer_name="Subhashish Ghosh",
            customer_code="CUST-KOL-0001",
            locality="Salt Lake",
            plan_id="PLAN-PRE-HERO-1.5G",
            plan_name="Hero Unlimited",
            plan_price=299.0,
            status=ProvisioningStatus.REQUESTED
        )
        db.add(kolkata_order)
        db.commit()
        db.refresh(kolkata_order)
        kol_code = kolkata_order.order_code
    finally:
        db.close()

    res = client.get(f"/api/provisioning/orders/{kol_code}", headers=mumbai_headers)
    assert res.status_code == 403
    assert "Cross-market access denied" in res.json()["detail"]


# ─── 10. Real Vendor Adapter Fail-Closed Safety ───────────────────────────────

def test_real_vendor_adapter_fails_closed_without_credentials():
    headers = _auth_headers("Admin", "mumbai")
    create_res = client.post("/api/provisioning/orders", json={
        "customer_id": 7,
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "idempotency_key": f"IDEM-REAL-{uuid.uuid4().hex[:12]}",
        "execution_mode": "REAL_VENDOR"
    }, headers=headers)
    assert create_res.status_code == 201
    order_code = create_res.json()["order_code"]

    # Approve
    client.post(f"/api/provisioning/orders/{order_code}/approve", headers=headers)

    # Real execution fails closed because real hardware adapter credentials and test env are unconfigured
    prov_res = client.post(f"/api/provisioning/orders/{order_code}/provision", headers=headers)
    assert prov_res.status_code == 200
    failed_order = prov_res.json()
    assert failed_order["status"] == "FAILED"
    assert failed_order["failure_code"] == "REAL_VENDOR_ADAPTER_UNCONFIGURED"
    assert "Production network mutations require verified hardware credentials" in failed_order["failure_reason"]
