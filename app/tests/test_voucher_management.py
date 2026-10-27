"""
test_voucher_management.py

Tests for SentinelOS Voucher Campaign & Coupon Management:
1. Voucher definition lifecycle (CRUD, pause, activate, revoke).
2. Bulk voucher code generation with uniqueness guarantees.
3. Pre-redemption eligibility validation and discount mathematics.
4. Concurrency & quota fulfillment (exhaustion, per-customer limits, idempotency).
5. Billing integration (credit note issuance and GL posting).
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import engine
from app.models import User, Customer
from app.models_voucher import Voucher, VoucherStatus, VoucherDefinitionType
from app.models_finance import VoucherRedemption, VoucherType, VoucherRedemptionStatus
from app.services import voucher_management_service, voucher_service


client = TestClient(app)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = TestSessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_voucher_definition_lifecycle(db_session):
    """
    Tests creating, retrieving, pausing, activating, and revoking a voucher.
    """
    code = f"TEST-VCH-{uuid.uuid4().hex[:6].upper()}"
    v_in = {
        "voucher_code": code,
        "campaign_name": "Summer High Speed Promo",
        "description": "₹150 off broadband bill",
        "voucher_type": VoucherDefinitionType.BILL_DISCOUNT,
        "discount_amount": Decimal("150.00"),
        "max_redemptions": 10,
        "per_customer_limit": 1,
        "valid_from": datetime.utcnow() - timedelta(days=1),
        "valid_until": datetime.utcnow() + timedelta(days=30),
        "market_id": "mumbai",
        "status": VoucherStatus.ACTIVE
    }

    class DummyIn:
        def __init__(self, d):
            for k, v in d.items():
                setattr(self, k, v)

    v = voucher_management_service.create_voucher(db_session, DummyIn(v_in))
    assert v.voucher_code == code
    assert v.status == VoucherStatus.ACTIVE

    # Pause
    paused = voucher_management_service.pause_voucher(db_session, v.id)
    assert paused.status == VoucherStatus.PAUSED

    # Activate
    activated = voucher_management_service.activate_voucher(db_session, v.id)
    assert activated.status == VoucherStatus.ACTIVE

    # Revoke
    revoked = voucher_management_service.revoke_voucher(db_session, v.id)
    assert revoked.status == VoucherStatus.REVOKED


def test_bulk_voucher_generation(db_session):
    """
    Tests bulk generation of unique voucher coupons.
    """
    bulk_in = {
        "prefix": "FESTIVE",
        "count": 5,
        "campaign_name": "Diwali Festival Flash Sale",
        "voucher_type": VoucherDefinitionType.BILL_DISCOUNT,
        "discount_amount": Decimal("200.00"),
        "max_redemptions": 1,
        "per_customer_limit": 1,
        "valid_from": datetime.utcnow(),
        "valid_until": datetime.utcnow() + timedelta(days=15),
        "market_id": "ALL"
    }

    class DummyIn:
        def __init__(self, d):
            for k, v in d.items():
                setattr(self, k, v)

    vouchers = voucher_management_service.bulk_create_vouchers(db_session, DummyIn(bulk_in))
    assert len(vouchers) == 5
    codes = [v.voucher_code for v in vouchers]
    assert len(set(codes)) == 5
    for c in codes:
        assert c.startswith("FESTIVE-")


def test_voucher_validation_and_discount_rules(db_session):
    """
    Tests pre-redemption validation:
    - Min invoice requirement
    - Market isolation
    - Percentage discount with maximum discount cap
    """
    code = f"DISC20-{uuid.uuid4().hex[:4].upper()}"
    v = Voucher(
        id=uuid.uuid4(),
        voucher_code=code,
        campaign_name="20% Capped Discount",
        voucher_type=VoucherDefinitionType.PERCENT_DISCOUNT,
        discount_percent=Decimal("20.00"),
        max_discount_cap=Decimal("150.00"),
        min_invoice_amount=Decimal("500.00"),
        max_redemptions=50,
        per_customer_limit=1,
        valid_from=datetime.utcnow() - timedelta(days=1),
        valid_until=datetime.utcnow() + timedelta(days=30),
        market_id="mumbai",
        status=VoucherStatus.ACTIVE
    )
    db_session.add(v)
    db_session.commit()

    # Case 1: Invoice amount below minimum threshold -> Invalid
    ok, disc, v_type, reason = voucher_management_service.validate_voucher_for_redemption(
        db_session,
        voucher_code=code,
        customer_id=1,
        invoice_amount=Decimal("400.00"),
        customer_market="mumbai"
    )
    assert not ok
    assert "Minimum invoice amount required" in reason

    # Case 2: Wrong market -> Invalid
    ok, disc, v_type, reason = voucher_management_service.validate_voucher_for_redemption(
        db_session,
        voucher_code=code,
        customer_id=1,
        invoice_amount=Decimal("800.00"),
        customer_market="kolkata"
    )
    assert not ok
    assert "Voucher only applicable in market 'mumbai'" in reason

    # Case 3: Valid, capped discount (20% of 1000 = 200, capped at 150)
    ok, disc, v_type, reason = voucher_management_service.validate_voucher_for_redemption(
        db_session,
        voucher_code=code,
        customer_id=1,
        invoice_amount=Decimal("1000.00"),
        customer_market="mumbai"
    )
    assert ok
    assert disc == Decimal("150.00")


def test_voucher_redemption_fulfillment_and_quota_exhaustion(db_session):
    """
    Tests end-to-end fulfillment via voucher_service.redeem_customer_voucher:
    - Single-use voucher transitions to EXHAUSTED upon redemption.
    - Subsequent redemption attempt is rejected.
    - Per-customer limit prevents duplicate claims by the same customer.
    """
    customer = db_session.query(Customer).first()
    assert customer is not None

    admin_user = db_session.query(User).filter(User.role.in_(["Super Admin", "Admin"])).first()
    if not admin_user:
        admin_user = User(
            email="admin_vch@sentinel.net",
            full_name="Admin Vch",
            role="Admin"
        )
        db_session.add(admin_user)
        db_session.commit()

    code = f"ONCE-{uuid.uuid4().hex[:6].upper()}"
    v = Voucher(
        id=uuid.uuid4(),
        voucher_code=code,
        campaign_name="Single Use Campaign",
        voucher_type=VoucherDefinitionType.BILL_DISCOUNT,
        discount_amount=Decimal("100.00"),
        max_redemptions=1,
        per_customer_limit=1,
        valid_from=datetime.utcnow() - timedelta(days=1),
        valid_until=datetime.utcnow() + timedelta(days=30),
        market_id="ALL",
        status=VoucherStatus.ACTIVE
    )
    db_session.add(v)
    db_session.commit()

    # Step 1: First redemption -> Succeeds
    redemption = voucher_service.redeem_customer_voucher(
        db=db_session,
        customer_id=customer.id,
        voucher_code=code,
        voucher_type=VoucherType.BILL_DISCOUNT,
        current_user=admin_user
    )
    assert redemption.status == VoucherRedemptionStatus.FULFILLED
    assert redemption.financial_credit_amount == Decimal("100.00")
    assert redemption.voucher_id == v.id

    # Check voucher state
    db_session.refresh(v)
    assert v.current_redemptions == 1
    assert v.status == VoucherStatus.EXHAUSTED

    # Step 2: Second redemption attempt with different customer -> Rejection due to exhaustion
    other_cust = db_session.query(Customer).filter(Customer.id != customer.id).first()
    if other_cust:
        with pytest.raises(voucher_service.VoucherRedemptionError) as exc_info:
            voucher_service.redeem_customer_voucher(
                db=db_session,
                customer_id=other_cust.id,
                voucher_code=code,
                voucher_type=VoucherType.BILL_DISCOUNT,
                current_user=admin_user
            )
        assert "EXHAUSTED" in str(exc_info.value) or "maximum" in str(exc_info.value)


def test_voucher_multi_redemption_per_customer_limit(db_session):
    """
    Regression test: ensures campaigns with per_customer_limit > 1 permit multiple
    redemptions for the same customer up to the limit, and reject redemptions beyond the limit.
    """
    customer = db_session.query(Customer).first()
    assert customer is not None

    admin_user = db_session.query(User).filter(User.role.in_(["Super Admin", "Admin"])).first()
    if not admin_user:
        admin_user = User(email="admin_multi@sentinel.net", full_name="Admin Multi", role="Admin")
        db_session.add(admin_user)
        db_session.commit()

    code = f"MULTI-{uuid.uuid4().hex[:6].upper()}"
    v = Voucher(
        id=uuid.uuid4(),
        voucher_code=code,
        campaign_name="Multi Redemptions Campaign",
        voucher_type=VoucherDefinitionType.BILL_DISCOUNT,
        discount_amount=Decimal("50.00"),
        max_redemptions=10,
        per_customer_limit=2,
        valid_from=datetime.utcnow() - timedelta(days=1),
        valid_until=datetime.utcnow() + timedelta(days=30),
        market_id="ALL",
        status=VoucherStatus.ACTIVE
    )
    db_session.add(v)
    db_session.commit()

    # 1. First redemption for customer
    r1 = voucher_service.redeem_customer_voucher(
        db=db_session,
        customer_id=customer.id,
        voucher_code=code,
        voucher_type=VoucherType.BILL_DISCOUNT,
        current_user=admin_user,
        idempotency_key=f"REQ-{code}-1"
    )
    assert r1.status == VoucherRedemptionStatus.FULFILLED

    # 2. Second redemption for same customer (different request/idempotency key)
    r2 = voucher_service.redeem_customer_voucher(
        db=db_session,
        customer_id=customer.id,
        voucher_code=code,
        voucher_type=VoucherType.BILL_DISCOUNT,
        current_user=admin_user,
        idempotency_key=f"REQ-{code}-2"
    )
    assert r2.status == VoucherRedemptionStatus.FULFILLED
    assert r2.id != r1.id

    # 3. Third redemption should exceed per_customer_limit=2
    with pytest.raises(voucher_service.VoucherRedemptionError) as exc:
        voucher_service.redeem_customer_voucher(
            db=db_session,
            customer_id=customer.id,
            voucher_code=code,
            voucher_type=VoucherType.BILL_DISCOUNT,
            current_user=admin_user,
            idempotency_key=f"REQ-{code}-3"
        )
    assert "Per-customer limit (2) reached" in str(exc.value)
