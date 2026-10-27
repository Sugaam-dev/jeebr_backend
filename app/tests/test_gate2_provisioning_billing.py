"""
test_gate2_provisioning_billing.py

Gate 2 Comprehensive Verification Test Suite: Provisioning-to-Billing Workflow Integrity
Covers:
1. Successful provisioning creates expected BillingAccount and BillingSubscription records.
2. Next billing date and subscription validity are derived directly from canonical plan rules.
3. Repeated completion events and retries are strictly idempotent (no duplicate accounts/subscriptions).
4. Missing or invalid subscriber/plan data fails safely without orphan records.
5. Cross-market or cross-tenant data cannot be linked incorrectly (strict market isolation).
6. A failed billing write rolls back cleanly and leaves no untracked partial state.
7. Reconciliation self-healing ensures billing initialization.
8. No premature invoices or payment charges are generated during activation.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models import Customer, User, NetworkDevice
from app.models_crm import CRMCustomer
from app.models_provisioning import (
    ServiceActivationOrder, ProvisioningLog, ProvisioningStatus, ExecutionMode
)
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingAccountStatus,
    SubscriptionStatus, BillingType
)
from app.auth import create_access_token
from app.services.billing_service import (
    initialize_billing_for_order, ensure_billing_account, create_subscription
)
from app.services.provisioning_service import execute_provisioning, reconcile_activation_order
from app.services.provisioning_catalog import get_service_plan_by_id

client = TestClient(app)

ROLE_EMAIL_MAP = {
    "SUPER_ADMIN": "superadmin@pmrg.in",
    "Admin": "admin@pmrg.in",
    "NOC": "noc@pmrg.in",
    "Care": "care@pmrg.in",
}

def _auth_headers(role: str = "Admin", market: str = "mumbai") -> dict:
    email = ROLE_EMAIL_MAP.get(role, "admin@pmrg.in")
    token = create_access_token({"sub": email, "role": role})
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market,
        "Content-Type": "application/json"
    }


# ─── 1. Successful Provisioning Creates Expected Billing Records ─────────────

def test_provisioning_success_initializes_billing_records(db_session: Session):
    """
    Verify that an end-to-end provisioning workflow transitions to ACTIVE
    and automatically creates an authoritative BillingAccount and BillingSubscription.
    """
    headers_care = _auth_headers("Care", "mumbai")
    headers_admin = _auth_headers("Admin", "mumbai")

    # Use customer 2 (Mumbai residential customer)
    cust = db_session.query(Customer).filter(Customer.id == 2).first()
    assert cust is not None

    plan_id = "PLAN-PRE-SUPER-5G"
    plan_meta = get_service_plan_by_id(plan_id)
    assert plan_meta is not None

    # 1. Create activation order
    idem_key = f"IDEM-GATE2-{uuid.uuid4().hex[:12]}"
    create_res = client.post("/api/provisioning/orders", json={
        "customer_id": cust.id,
        "plan_id": plan_id,
        "idempotency_key": idem_key
    }, headers=headers_care)
    assert create_res.status_code == 201
    order_data = create_res.json()
    order_code = order_data["order_code"]
    order_id = order_data["id"]

    # 2. Approve order (Admin)
    app_res = client.post(f"/api/provisioning/orders/{order_code}/approve", json={"notes": "Approved for Gate 2 verification"}, headers=headers_admin)
    assert app_res.status_code == 200
    assert app_res.json()["status"] == "APPROVED"

    # 3. Execute provisioning (Admin)
    prov_res = client.post(f"/api/provisioning/orders/{order_code}/provision", headers=headers_admin)
    assert prov_res.status_code == 200
    active_order = prov_res.json()
    assert active_order["status"] == "ACTIVE"
    assert active_order["subscription_status"] == "ACTIVE"

    # 4. Verify BillingAccount exists and matches customer & market
    acct = db_session.query(BillingAccount).filter(BillingAccount.customer_id == cust.id).first()
    assert acct is not None
    assert acct.market_id.lower() == cust.market_id.lower()
    assert acct.status == BillingAccountStatus.ACTIVE
    assert acct.account_number.startswith(f"BA-{cust.market_id[:3].upper()}")

    # 5. Verify BillingSubscription exists and links to provisioning_order_id
    sub = db_session.query(BillingSubscription).filter(
        BillingSubscription.provisioning_order_id == order_id
    ).first()
    assert sub is not None
    assert sub.billing_account_id == acct.id
    assert sub.customer_id == cust.id
    assert sub.market_id.lower() == cust.market_id.lower()
    assert sub.plan_id == plan_id
    assert sub.plan_name == plan_meta["name"]
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.unit_price == Decimal(str(plan_meta["price_inr"]))

    # 6. Verify plan rules derivation (validity days and next billing date)
    expected_validity = plan_meta.get("validity_days", 28)
    delta = sub.current_period_end - sub.current_period_start
    # Delta days should match plan validity days
    assert delta.days == expected_validity
    assert sub.next_billing_date == sub.current_period_end

    # 7. Verify response exposes billing identifiers
    detail_res = client.get(f"/api/provisioning/orders/{order_code}", headers=headers_admin)
    assert detail_res.status_code == 200
    det = detail_res.json()
    assert det["billing_account_number"] == acct.account_number
    assert det["billing_subscription_code"] == sub.subscription_code


# ─── 2. Idempotency: Repeated Completion Does Not Duplicate Records ───────────

def test_provisioning_completion_idempotency_no_duplicates(db_session: Session):
    """
    Replaying initialize_billing_for_order or repeated completion calls
    must return the existing billing entities without creating duplicates.
    """
    order = db_session.query(ServiceActivationOrder).filter(
        ServiceActivationOrder.status == ProvisioningStatus.ACTIVE
    ).first()
    assert order is not None

    # Count existing subscriptions for this customer and order
    subs_before = db_session.query(BillingSubscription).filter(
        BillingSubscription.provisioning_order_id == order.id
    ).all()
    assert len(subs_before) == 1

    accounts_before = db_session.query(BillingAccount).filter(
        BillingAccount.customer_id == order.customer_id
    ).all()
    assert len(accounts_before) == 1

    # Replay initialize_billing_for_order directly
    acct, sub = initialize_billing_for_order(order=order, db=db_session, auto_commit=True)
    assert acct.id == accounts_before[0].id
    assert sub.id == subs_before[0].id

    # Verify counts have NOT increased
    subs_after = db_session.query(BillingSubscription).filter(
        BillingSubscription.provisioning_order_id == order.id
    ).all()
    assert len(subs_after) == 1

    accounts_after = db_session.query(BillingAccount).filter(
        BillingAccount.customer_id == order.customer_id
    ).all()
    assert len(accounts_after) == 1


# ─── 3. Missing or Invalid Subscriber / Plan Data Fails Safely ────────────────

def test_invalid_subscriber_fails_safely(db_session: Session):
    """Attempting billing initialization for a nonexistent subscriber raises ValueError."""
    fake_order = ServiceActivationOrder(
        id=999999,
        order_code="ACT-FAKE-0001",
        idempotency_key="IDEM-FAKE-0001",
        market_id="mumbai",
        customer_id=9876543,  # Nonexistent
        customer_name="Fake User",
        customer_code="CUST-FAKE",
        plan_id="PLAN-PRE-HERO-1.5G",
        plan_name="Hero Plan",
        plan_price=299.0,
        status=ProvisioningStatus.ACTIVE,
        created_at=datetime.utcnow()
    )

    with pytest.raises(ValueError) as exc:
        initialize_billing_for_order(order=fake_order, db=db_session, auto_commit=False)
    assert "Customer #9876543 does not exist" in str(exc.value)


def test_invalid_plan_fails_safely(db_session: Session):
    """Attempting billing initialization for an invalid plan raises ValueError."""
    cust = db_session.query(Customer).first()
    assert cust is not None

    fake_order = ServiceActivationOrder(
        id=999998,
        order_code="ACT-FAKE-0002",
        idempotency_key="IDEM-FAKE-0002",
        market_id=cust.market_id,
        customer_id=cust.id,
        customer_name=cust.name,
        customer_code=cust.customer_code,
        plan_id="PLAN-INVALID-NONEXISTENT",
        plan_name="Invalid Plan",
        plan_price=999.0,
        status=ProvisioningStatus.ACTIVE,
        created_at=datetime.utcnow()
    )

    with pytest.raises(ValueError) as exc:
        initialize_billing_for_order(order=fake_order, db=db_session, auto_commit=False)
    assert "does not exist in the approved tariff catalog" in str(exc.value)


# ─── 4. Cross-Market Data Isolation Rejection ─────────────────────────────────

def test_cross_market_billing_linkage_strictly_rejected(db_session: Session):
    """Order with mismatched market relative to customer is strictly rejected."""
    # Customer 1 belongs to Mumbai
    cust = db_session.query(Customer).filter(Customer.id == 1).first()
    assert cust is not None
    assert cust.market_id.lower() == "mumbai"

    mismatched_order = ServiceActivationOrder(
        id=999997,
        order_code="ACT-KOL-9999",
        idempotency_key="IDEM-MISMATCH-0001",
        market_id="kolkata",  # Mismatched!
        customer_id=cust.id,
        customer_name=cust.name,
        customer_code=cust.customer_code,
        plan_id="PLAN-PRE-HERO-1.5G",
        plan_name="Hero Plan",
        plan_price=299.0,
        status=ProvisioningStatus.ACTIVE,
        created_at=datetime.utcnow()
    )

    with pytest.raises(ValueError) as exc:
        initialize_billing_for_order(order=mismatched_order, db=db_session, auto_commit=False)
    assert "Cross-market billing violation" in str(exc.value)


# ─── 5. Failed Billing Write Rolls Back Cleanly ───────────────────────────────

def test_failed_billing_write_does_not_leave_untracked_state(monkeypatch, db_session: Session):
    """
    If billing initialization fails during execute_provisioning, the transaction
    rolls back, the order transitions to FAILED with an actionable failure code,
    and no partial subscription record is left in the database.
    """
    admin_user = db_session.query(User).filter(User.role.in_(["Admin", "SUPER_ADMIN"])).first()
    cust = db_session.query(Customer).filter(Customer.id == 5).first()
    assert cust is not None

    # Create an order in APPROVED state
    order = ServiceActivationOrder(
        order_code=f"ACT-MUM-ERR-{uuid.uuid4().hex[:6].upper()}",
        idempotency_key=f"IDEM-ERR-{uuid.uuid4().hex[:8]}",
        market_id="mumbai",
        customer_id=cust.id,
        customer_name=cust.name,
        customer_code=cust.customer_code,
        locality=cust.locality or "Bandra West",
        plan_id="PLAN-PRE-HERO-1.5G",
        plan_name="Hero Plan",
        plan_price=299.0,
        status=ProvisioningStatus.APPROVED,
        created_at=datetime.utcnow()
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    # Monkeypatch initialize_billing_for_order to simulate an unexpected ledger/billing failure
    def mock_failing_billing(*args, **kwargs):
        raise RuntimeError("Simulated transient database error in double-entry ledger allocation.")

    monkeypatch.setattr(
        "app.services.provisioning_service.initialize_billing_for_order",
        mock_failing_billing
    )

    # Execute provisioning
    result_order, exec_res = execute_provisioning(
        order_id_or_code=order.order_code,
        current_user=admin_user,
        db=db_session
    )

    # Assert fail-closed behavior
    assert result_order.status == ProvisioningStatus.FAILED
    assert result_order.failure_code == "BILLING_INITIALIZATION_ERROR"
    assert "Simulated transient database error" in result_order.failure_reason
    assert not exec_res.success

    # Assert no partial subscription was committed
    partial_sub = db_session.query(BillingSubscription).filter(
        BillingSubscription.provisioning_order_id == order.id
    ).first()
    assert partial_sub is None


# ─── 6. Reconciliation Self-Healing Ensures Billing ───────────────────────────

def test_reconciliation_heals_and_initializes_billing(db_session: Session):
    """
    Hardware reconciliation that recovers an order to ACTIVE status
    also ensures the billing account and subscription are initialized.
    """
    admin_user = db_session.query(User).filter(User.role.in_(["Admin", "SUPER_ADMIN"])).first()
    cust = db_session.query(Customer).filter(Customer.id == 6).first()
    assert cust is not None

    order = ServiceActivationOrder(
        order_code=f"ACT-MUM-REC-{uuid.uuid4().hex[:6].upper()}",
        idempotency_key=f"IDEM-REC-{uuid.uuid4().hex[:8]}",
        market_id="mumbai",
        customer_id=cust.id,
        customer_name=cust.name,
        customer_code=cust.customer_code,
        locality=cust.locality or "Bandra West",
        plan_id="PLAN-POST-INFINITY-75G",
        plan_name="Infinity 75G",
        plan_price=799.0,
        status=ProvisioningStatus.PROVISIONING,  # In-flight
        created_at=datetime.utcnow()
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    # Reconcile activation order
    reconciled_order, res = reconcile_activation_order(
        order_id_or_code=order.order_code,
        current_user=admin_user,
        db=db_session
    )

    assert reconciled_order.status == ProvisioningStatus.ACTIVE
    # Billing subscription must be initialized
    sub = db_session.query(BillingSubscription).filter(
        BillingSubscription.provisioning_order_id == order.id
    ).first()
    assert sub is not None
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.plan_id == "PLAN-POST-INFINITY-75G"


# ─── 7. No Premature Invoices or Payments Generated ───────────────────────────

def test_no_side_effect_invoices_or_payments_on_activation(db_session: Session):
    """
    Completing provisioning must initialize accounts and subscriptions,
    but must NOT prematurely generate invoices or charge payments.
    """
    order = db_session.query(ServiceActivationOrder).filter(
        ServiceActivationOrder.status == ProvisioningStatus.ACTIVE
    ).first()
    assert order is not None

    sub = db_session.query(BillingSubscription).filter(
        BillingSubscription.provisioning_order_id == order.id
    ).first()
    assert sub is not None

    # Verify no line items or invoices were auto-generated as a side effect
    assert len(sub.line_items) == 0
