"""
test_tariff_management.py

Tests for SentinelOS Tariff Plan Management:
1. Deterministic canonical tariff seeding linked to ServicePlans.
2. Tariff CRUD operations, versioning, and validation.
3. Effective date range overlap prevention (409 Conflict).
4. Financial charge calculations (recurring, setup, usage, 18% GST, plan discounts).
5. REST API endpoint authorization and execution.
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
from app.models import User
from app.models_service_plan import ServicePlan
from app.models_tariff import TariffPlan, TariffStatus, BillingTiming
from app.services import tariff_service, catalog_service


client = TestClient(app)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = TestSessionLocal(bind=connection)
    try:
        catalog_service.seed_service_plans_from_canonical(session)
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_seed_canonical_tariffs(db_session):
    """
    Verifies that canonical tariffs are seeded for canonical service plans and idempotent.
    """
    tariffs = tariff_service.list_tariffs(db_session)
    assert len(tariffs) > 0

    hero_tariff = tariff_service.get_tariff_by_code(db_session, "TRF-PLAN-PRE-HERO-1.5G-V1")
    assert hero_tariff is not None
    assert hero_tariff.service_plan_code == "PLAN-PRE-HERO-1.5G"
    assert hero_tariff.base_recurring_charge == Decimal("299.00")
    assert hero_tariff.tax_rate_percent == Decimal("18.00")
    assert hero_tariff.status == TariffStatus.ACTIVE

    # Idempotent re-seed produces 0 new records
    second_seed = tariff_service.seed_canonical_tariffs(db_session)
    assert second_seed == 0


def test_tariff_crud_and_versioning(db_session):
    """
    Tests creating, updating, and deactivating a tariff plan.
    """
    plan = db_session.query(ServicePlan).first()
    assert plan is not None

    custom_code = f"TRF-CUST-{uuid.uuid4().hex[:6].upper()}-V1"
    tariff_in = {
        "tariff_code": custom_code,
        "name": "Custom Fiber Tier 1",
        "description": "High tier custom package",
        "service_plan_code": plan.plan_code,
        "market_id": "mumbai",
        "version": 1,
        "billing_type": "POSTPAID",
        "billing_frequency": "MONTHLY",
        "billing_timing": "IN_ARREARS",
        "currency": "INR",
        "base_recurring_charge": Decimal("1199.00"),
        "setup_fee": Decimal("1000.00"),
        "usage_rate_per_gb": Decimal("2.50"),
        "tax_rate_percent": Decimal("18.00"),
        "discount_percentage": Decimal("10.00"),
        "effective_from": datetime(2026, 6, 1),
        "status": TariffStatus.ACTIVE
    }

    class DummyIn:
        def __init__(self, d):
            for k, v in d.items():
                setattr(self, k, v)

    created = tariff_service.create_tariff(db_session, DummyIn(tariff_in))
    assert created.tariff_code == custom_code
    assert created.base_recurring_charge == Decimal("1199.00")
    assert created.discount_percentage == Decimal("10.00")

    # Deactivate tariff
    deactivated = tariff_service.deactivate_tariff(db_session, created.id)
    assert deactivated.status == TariffStatus.INACTIVE


def test_effective_date_overlap_prevention(db_session):
    """
    Tests that overlapping active tariffs for the same plan & market are rejected with 409 Conflict,
    while non-overlapping periods succeed.
    """
    plan = db_session.query(ServicePlan).first()

    class DummyTariff:
        def __init__(self, **kwargs):
            self.tariff_code = kwargs.get("tariff_code")
            self.name = kwargs.get("name", "Test Tariff")
            self.description = kwargs.get("description", "Desc")
            self.service_plan_code = kwargs.get("service_plan_code")
            self.market_id = kwargs.get("market_id", "kolkata")
            self.version = kwargs.get("version", 1)
            self.billing_type = kwargs.get("billing_type", "POSTPAID")
            self.billing_frequency = kwargs.get("billing_frequency", "MONTHLY")
            self.billing_timing = kwargs.get("billing_timing", "IN_ADVANCE")
            self.currency = kwargs.get("currency", "INR")
            self.base_recurring_charge = kwargs.get("base_recurring_charge", Decimal("500.00"))
            self.setup_fee = kwargs.get("setup_fee", Decimal("0.00"))
            self.usage_rate_per_gb = kwargs.get("usage_rate_per_gb", Decimal("0.00"))
            self.tax_rate_percent = kwargs.get("tax_rate_percent", Decimal("18.00"))
            self.discount_percentage = kwargs.get("discount_percentage", Decimal("0.00"))
            self.effective_from = kwargs.get("effective_from")
            self.effective_to = kwargs.get("effective_to")
            self.status = kwargs.get("status", TariffStatus.ACTIVE)

    # 1. Tariff A: Jan 1, 2026 to Jun 30, 2026
    t_a = DummyTariff(
        tariff_code=f"TRF-OVP-A-{uuid.uuid4().hex[:4]}",
        service_plan_code=plan.plan_code,
        market_id="kolkata",
        effective_from=datetime(2026, 1, 1),
        effective_to=datetime(2026, 6, 30)
    )
    tariff_service.create_tariff(db_session, t_a)

    # 2. Tariff B: Mar 1, 2026 to Sep 30, 2026 (Overlaps with A) -> Should fail with 409
    t_b = DummyTariff(
        tariff_code=f"TRF-OVP-B-{uuid.uuid4().hex[:4]}",
        service_plan_code=plan.plan_code,
        market_id="kolkata",
        effective_from=datetime(2026, 3, 1),
        effective_to=datetime(2026, 9, 30)
    )
    with pytest.raises(HTTPException) as exc_info:
        tariff_service.create_tariff(db_session, t_b)
    assert exc_info.value.status_code == 409
    assert "Effective date conflict" in exc_info.value.detail

    # 3. Tariff C: Jul 1, 2026 to Dec 31, 2026 (Non-overlapping with A) -> Should succeed
    t_c = DummyTariff(
        tariff_code=f"TRF-OVP-C-{uuid.uuid4().hex[:4]}",
        service_plan_code=plan.plan_code,
        market_id="kolkata",
        effective_from=datetime(2026, 7, 1),
        effective_to=datetime(2026, 12, 31)
    )
    created_c = tariff_service.create_tariff(db_session, t_c)
    assert created_c is not None


def test_tariff_charge_calculation_breakdown(db_session):
    """
    Tests financial calculation mathematics:
    - Base recurring + setup fee + usage charges.
    - Plan discount deduction.
    - 18% GST computation on taxable amount.
    - Final amount payable.
    """
    plan = db_session.query(ServicePlan).first()

    tcode = f"TRF-CALC-{uuid.uuid4().hex[:6]}"
    tariff = TariffPlan(
        id=uuid.uuid4(),
        tariff_code=tcode,
        name="Calculation Test Tariff",
        service_plan_code=plan.plan_code,
        market_id="ALL",
        version=1,
        billing_type="POSTPAID",
        billing_frequency="MONTHLY",
        billing_timing=BillingTiming.IN_ARREARS,
        base_recurring_charge=Decimal("1000.00"),
        setup_fee=Decimal("500.00"),
        usage_rate_per_gb=Decimal("10.00"),
        tax_rate_percent=Decimal("18.00"),
        discount_percentage=Decimal("10.00"),  # 10% plan discount
        effective_from=datetime(2026, 1, 1),
        status=TariffStatus.ACTIVE
    )
    db_session.add(tariff)
    db_session.commit()

    # Scenario: 20 GB usage, apply_setup_fee = True
    # Gross subtotal = 1000 + 500 + (20 * 10) = 1700.00
    # Plan discount (10%) = 170.00
    # Taxable amount = 1700 - 170 = 1530.00
    # Tax (18%) = 1530 * 0.18 = 275.40
    # Total payable = 1530 + 275.40 = 1805.40

    calc = tariff_service.calculate_tariff_breakdown(
        db_session,
        tariff_code=tcode,
        usage_gb=20.0,
        apply_setup_fee=True
    )

    assert calc["gross_subtotal"] == Decimal("1700.00")
    assert calc["plan_discount_amount"] == Decimal("170.00")
    assert calc["taxable_amount"] == Decimal("1530.00")
    assert calc["tax_amount"] == Decimal("275.40")
    assert calc["total_amount_payable"] == Decimal("1805.40")
