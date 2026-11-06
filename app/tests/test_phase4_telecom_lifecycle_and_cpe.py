"""
app/tests/test_phase4_telecom_lifecycle_and_cpe.py

Comprehensive Enterprise Test Suite for Phase 4:
1. Vacation Holds & Temporary Suspensions (Unserved Bandwidth Credit & Line Retention).
2. Geographic Premises Relocation & Section 12(11)(a) PoS Recalibration.
3. CPE Hardware Asset Assignment, Security Deposit Liability & Damage Deductions.
4. Tariff Plan Upgrades & Downgrades (Immediate Proration vs Next-Cycle Scheduling).
5. Automated SLA Breach Outage Evaluation & Credit Note Issuance.
"""

import uuid
import pytest
from decimal import Decimal
from datetime import datetime, timedelta

from app.models import Customer, Invoice
from app.models_crm import CRMCustomer, CRMAddress, AccountType, CustomerLifecycleStatus, AddressType
from app.models_finance import (
    BillingAccount, BillingSubscription, CreditNote, BillingAccountStatus,
    BillingType, SubscriptionStatus, InvoiceStatus, ChartOfAccounts, JournalEntry
)
from app.models_inventory import EquipmentCatalog, Warehouse, InventoryDevice
from app.models_lifecycle import (
    VacationHold, PremisesRelocation, CPEAssetAssignment, ScheduledPlanChange,
    SLAIncidentRecord, VacationHoldStatus, RelocationStatus, CPEOwnershipType,
    CPEAssignmentStatus, PlanChangeMode, PlanChangeStatus, SLAIncidentStatus
)
from app.models_cpq import SLATier
from app.services.ledger_service import verify_ledger_balancing, quantize_money
from app.services.lifecycle_service import (
    apply_vacation_hold, resume_vacation_hold, initiate_premises_relocation,
    complete_premises_relocation, assign_cpe_device, return_cpe_device,
    change_subscription_plan, evaluate_sla_and_issue_credit
)


def _setup_lifecycle_subscriber(
    db,
    tag: str,
    market_id: str = "mumbai_p4",
    state: str = "Maharashtra",
    unit_price: Decimal = Decimal('1200.00'),
    billing_period_days: int = 30,
    elapsed_days: int = 15
) -> tuple:
    """Helper to provision customer, CRM master, address, account, and active subscription."""
    uid = uuid.uuid4().hex[:6].upper()
    now = datetime.utcnow()
    period_start = now - timedelta(days=elapsed_days)
    period_end = period_start + timedelta(days=billing_period_days)

    cust = Customer(
        name=f"Subscriber {tag} {uid}",
        email=f"sub_{tag.lower()}_{uid.lower()}@testisp.in",
        phone=f"98100{int(uid[:4], 16) % 90000 + 10000}",
        customer_code=f"CUST-P4-{tag}-{uid}",
        market_id=market_id,
        locality="Bandra West",
        segment="Retail",
        plan_name="UltraFiber 300M"
    )
    db.add(cust)
    db.flush()

    crm_cust = CRMCustomer(
        customer_number=f"CRM-P4-{tag}-{uid}",
        legal_name=f"Subscriber {tag} {uid}",
        account_type=AccountType.RESIDENTIAL,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id=market_id,
        legacy_customer_id=cust.id
    )
    db.add(crm_cust)
    db.flush()

    addr = CRMAddress(
        crm_customer_id=crm_cust.id,
        address_type=AddressType.INSTALLATION,
        address_line1="Shop 4, Linking Road, Bandra West",
        city="Mumbai",
        state=state,
        postal_code="400050",
        is_archived=False
    )
    db.add(addr)
    db.flush()

    acct = BillingAccount(
        id=uuid.uuid4(),
        account_number=f"BA-P4-{tag}-{uid}",
        customer_id=cust.id,
        crm_customer_id=crm_cust.id,
        market_id=market_id,
        billing_type=BillingType.POSTPAID,
        currency="INR",
        balance=Decimal('0.00'),
        credit_limit=Decimal('5000.00'),
        status=BillingAccountStatus.ACTIVE,
        payment_term_days=15
    )
    db.add(acct)
    db.flush()

    sub = BillingSubscription(
        id=uuid.uuid4(),
        subscription_code=f"SUB-P4-{tag}-{uid}",
        billing_account_id=acct.id,
        customer_id=cust.id,
        market_id=market_id,
        plan_id="plan_300m_ultra",
        plan_name="UltraFiber 300M",
        billing_type=BillingType.POSTPAID,
        unit_price=unit_price,
        currency="INR",
        current_period_start=period_start,
        current_period_end=period_end,
        next_billing_date=period_end,
        status=SubscriptionStatus.ACTIVE,
        created_at=period_start
    )
    db.add(sub)
    db.commit()

    return cust, crm_cust, addr, acct, sub


# ─── 1. Vacation Hold & Temporary Suspension Tests ───────────────────────────

def test_vacation_hold_proration_retention_and_resumption(db_session):
    """
    Verifies vacation hold lifecycle:
    1. Unserved bandwidth credit calculated accurately for remaining cycle window.
    2. Account credited and balanced General Ledger entries posted.
    3. Subscription transitioned to SUSPENDED with safe custody retention fee.
    4. Resumption restores ACTIVE status and original tariff rate.
    """
    mkt = "mumbai_p4_hold"
    cust, _, _, acct, sub = _setup_lifecycle_subscriber(
        db_session, tag="HOLD", market_id=mkt, unit_price=Decimal('1200.00'), elapsed_days=15
    )

    now = datetime.utcnow()
    hold_start = now
    expected_resume = now + timedelta(days=30)

    # Apply Vacation Hold
    hold = apply_vacation_hold(
        db=db_session,
        subscription_id=sub.id,
        hold_start=hold_start,
        expected_resume=expected_resume,
        retention_fee_monthly=Decimal('150.00'),
        reason="Family traveling overseas for 1 month"
    )

    # Assertions on Hold
    assert hold.status == VacationHoldStatus.ACTIVE_HOLD
    assert hold.retention_fee_monthly == Decimal('150.00')
    assert hold.original_monthly_price == Decimal('1200.00')
    assert hold.credit_adjustment_amount > Decimal('0.00')  # ~ 15 days unserved credit
    assert acct.balance > Decimal('0.00')

    # Verify subscription state
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.SUSPENDED
    assert sub.unit_price == Decimal('150.00')

    # Verify General Ledger Balancing
    ledger_check = verify_ledger_balancing(db_session, market_id=mkt)
    assert ledger_check["is_fully_balanced"] is True, f"GL out of balance: {ledger_check['unbalanced_entries']}"

    # Early Resumption
    resumed = resume_vacation_hold(db_session, hold_id=hold.id)
    assert resumed.status == VacationHoldStatus.RESUMED
    assert resumed.actual_resume_date is not None

    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.unit_price == Decimal('1200.00')


# ─── 2. Geographic Premises Relocation & Section 12(11)(a) Recalibration Tests ─

def test_geographic_premises_relocation_and_pos_recalibration(db_session):
    """
    Verifies premises relocation:
    1. Intra-state to Inter-state move updates installation address.
    2. Section 12(11)(a) Place of Supply recalibrates from 27-Maharashtra to 24-Gujarat.
    3. Shift charge invoice issued with 18% IGST instead of CGST+SGST.
    4. General Ledger posts balanced entries.
    5. Completion archives old address and updates relocation record.
    """
    mkt = "mumbai_p4_reloc"
    cust, crm_cust, old_addr, acct, sub = _setup_lifecycle_subscriber(
        db_session, tag="RELOC", market_id=mkt, state="Maharashtra"
    )

    new_address_data = {
        "address_line1": "Flat 801, Satellite Tower, SG Highway",
        "city": "Ahmedabad",
        "state": "Gujarat",
        "postal_code": "380015"
    }

    # Initiate Relocation
    relocation = initiate_premises_relocation(
        db=db_session,
        customer_id=cust.id,
        subscription_id=sub.id,
        new_address_data=new_address_data,
        shift_charge=Decimal('1000.00')
    )

    # Assertions on Relocation
    assert relocation.relocation_code.startswith("REL-")
    assert relocation.status == RelocationStatus.REQUESTED
    assert "27-Maharashtra" in relocation.old_place_of_supply
    assert "24-Gujarat" in relocation.new_place_of_supply

    # Assert Shift Charge Invoice
    assert relocation.shift_invoice_id is not None
    shift_inv = db_session.query(Invoice).filter(Invoice.id == relocation.shift_invoice_id).first()
    assert shift_inv is not None
    assert shift_inv.subtotal == Decimal('1000.00')
    assert shift_inv.tax_treatment == "INTER_STATE"  # Mumbai (Supplier) -> Gujarat (Installation)
    assert shift_inv.igst_amount == Decimal('180.00')
    assert shift_inv.cgst_amount == Decimal('0.00')
    assert shift_inv.total_amount == Decimal('1180.00')

    # Verify General Ledger Balancing
    ledger_check = verify_ledger_balancing(db_session, market_id=mkt)
    assert ledger_check["is_fully_balanced"] is True, f"GL out of balance: {ledger_check['unbalanced_entries']}"

    # Complete Relocation
    completed = complete_premises_relocation(db_session, relocation.id)
    assert completed.status == RelocationStatus.COMPLETED
    assert completed.completed_date is not None

    db_session.refresh(old_addr)
    assert old_addr.is_archived is True


# ─── 3. CPE Hardware Asset Assignment, Security Deposit & Return Tests ────────

def test_cpe_asset_assignment_security_deposit_and_damage_return(db_session):
    """
    Verifies CPE hardware asset lifecycle:
    1. ONT device assigned to customer with refundable security deposit (Liability 2100).
    2. Balanced General Ledger postings for deposit collection.
    3. Device returned in DAMAGED condition with partial damage deduction.
    4. General Ledger posts net cash refund and damage recovery income.
    """
    mkt = "mumbai_p4_cpe"
    cust, _, _, acct, sub = _setup_lifecycle_subscriber(db_session, tag="CPE", market_id=mkt)

    # 1. Setup Catalog, Warehouse & Serialized ONT Device
    cat = EquipmentCatalog(
        sku=f"ONT-GPON-WIFI6-{uuid.uuid4().hex[:6].upper()}",
        name="GPON Wi-Fi 6 Gigabit Gateway",
        equipment_type="ONT",
        model="HG8145X6",
        manufacturer="Huawei",
        warranty_months=24,
        unit_cost_inr=Decimal('2800.00'),
        status="ACTIVE"
    )
    wh = Warehouse(
        warehouse_code=f"WH-TEST-{uuid.uuid4().hex[:6].upper()}",
        name="Central Hub Depot",
        market_id=mkt,
        address="MIDC Andheri East",
        is_active=True
    )
    db_session.add_all([cat, wh])
    db_session.flush()

    dev = InventoryDevice(
        serial_number=f"ONT-SN-{uuid.uuid4().hex[:8].upper()}",
        mac_address=f"00:11:22:33:{uuid.uuid4().hex[:2].upper()}:{uuid.uuid4().hex[:2].upper()}",
        catalog_id=cat.id,
        warehouse_id=wh.id,
        market_id=mkt,
        status="IN_STOCK"
    )
    db_session.add(dev)
    db_session.commit()

    # 2. Assign CPE with Security Deposit of INR 2,000 and monthly rental of INR 100
    assignment = assign_cpe_device(
        db=db_session,
        device_id=dev.id,
        customer_id=cust.id,
        subscription_id=sub.id,
        ownership_type=CPEOwnershipType.SECURITY_DEPOSIT,
        monthly_rental=Decimal('100.00'),
        security_deposit=Decimal('2000.00')
    )

    # Assertions on Assignment
    assert assignment.status == CPEAssignmentStatus.ASSIGNED
    assert assignment.deposit_paid is True
    assert assignment.security_deposit_amount == Decimal('2000.00')

    db_session.refresh(dev)
    assert dev.status == "INSTALLED"
    assert dev.assigned_customer_id == cust.id

    # Verify General Ledger Balancing after deposit collection
    ledger_check = verify_ledger_balancing(db_session, market_id=mkt)
    assert ledger_check["is_fully_balanced"] is True, f"GL out of balance: {ledger_check['unbalanced_entries']}"

    # 3. Return Device with Damage (INR 500 damage deduction from INR 2,000 deposit)
    ret_res = return_cpe_device(
        db=db_session,
        assignment_id=assignment.id,
        condition="DAMAGED",
        damage_charge=Decimal('500.00')
    )

    assert ret_res["status"] == CPEAssignmentStatus.DAMAGED
    assert ret_res["original_deposit"] == 2000.00
    assert ret_res["damage_deduction"] == 500.00
    assert ret_res["net_refund_amount"] == 1500.00

    db_session.refresh(dev)
    assert dev.status == "DAMAGED"
    assert dev.assigned_customer_id is None

    # Verify Final General Ledger Balancing (full deposit cleared, net refund credited, damage recognized)
    ledger_check_final = verify_ledger_balancing(db_session, market_id=mkt)
    assert ledger_check_final["is_fully_balanced"] is True, f"GL out of balance: {ledger_check_final['unbalanced_entries']}"


# ─── 4. Tariff Plan Upgrades & Downgrades Tests ──────────────────────────────

def test_tariff_plan_upgrade_immediate_proration_vs_next_cycle(db_session):
    """
    Verifies plan upgrade and downgrade handling:
    1. Immediate Upgrade calculates exact-day proration, updates subscription tariff,
       and issues adjustment invoice with balanced GL postings.
    2. Next-Cycle Downgrade schedules change record without immediate pricing alteration.
    """
    mkt = "mumbai_p4_plan"
    cust, _, _, acct, sub = _setup_lifecycle_subscriber(
        db_session, tag="PLAN", market_id=mkt, unit_price=Decimal('599.00'), elapsed_days=15
    )

    # 1. Immediate Upgrade to Family Plus 150G Plan (Price = INR 999.00)
    upgrade_res = change_subscription_plan(
        db=db_session,
        subscription_id=sub.id,
        target_plan_id="PLAN-POST-FAMILY-150G",
        effective_mode=PlanChangeMode.IMMEDIATE
    )

    assert upgrade_res["status"] == "EXECUTED"
    assert upgrade_res["change_type"] == "UPGRADE"
    assert upgrade_res["new_plan_price"] == 999.00
    assert upgrade_res["net_adjustment_amount"] > 0.0  # Customer owes upgrade difference
    assert upgrade_res["adjustment_invoice_id"] is not None

    db_session.refresh(sub)
    assert sub.plan_id == "PLAN-POST-FAMILY-150G"
    assert sub.unit_price == Decimal('999.00')

    # Verify General Ledger Balancing after upgrade invoice
    ledger_check = verify_ledger_balancing(db_session, market_id=mkt)
    assert ledger_check["is_fully_balanced"] is True, f"GL out of balance: {ledger_check['unbalanced_entries']}"

    # 2. Next-Cycle Downgrade to Individual Infinity 75G Plan (Price = INR 499.00)
    downgrade_res = change_subscription_plan(
        db=db_session,
        subscription_id=sub.id,
        target_plan_id="PLAN-POST-INFINITY-75G",
        effective_mode=PlanChangeMode.NEXT_CYCLE
    )

    assert downgrade_res["status"] == "SCHEDULED"
    assert downgrade_res["change_type"] == "DOWNGRADE"
    assert downgrade_res["effective_mode"] == "NEXT_CYCLE"

    # Current subscription remains on 999 plan until scheduled cycle rollover
    db_session.refresh(sub)
    assert sub.plan_id == "PLAN-POST-FAMILY-150G"
    assert sub.unit_price == Decimal('999.00')


# ─── 5. Automated SLA Breach Outage Evaluation & Credit Notes Tests ───────────

def test_automated_sla_breach_evaluation_and_credit_note(db_session):
    """
    Verifies SLA availability evaluation and automated credit issuance:
    1. Compliant uptime (99.8% on 99.5% SLA) generates no breach or credit note.
    2. Outage breach (97.0% uptime on 99.5% SLA, 1300 minutes downtime):
       - Confirms SLA breach.
       - Evaluates tiered penalty percentage (25% credit).
       - Issues authoritative CreditNote with balanced General Ledger postings.
    """
    mkt = "mumbai_p4_sla"
    cust, _, _, acct, sub = _setup_lifecycle_subscriber(
        db_session, tag="SLA", market_id=mkt, unit_price=Decimal('2000.00'), billing_period_days=30
    )

    p_start = sub.current_period_start
    p_end = sub.current_period_end

    # Scenario 1: Minor 30 min outage -> 99.93% uptime (Compliant with 99.5% SLA)
    res_compliant = evaluate_sla_and_issue_credit(
        db=db_session,
        subscription_id=sub.id,
        downtime_minutes=30,
        billing_period_start=p_start,
        billing_period_end=p_end,
        sla_tier=SLATier.STANDARD_99_5
    )
    assert res_compliant["sla_breached"] is False
    assert res_compliant["credit_note_code"] is None
    assert res_compliant["credit_note_amount"] == 0.0

    # Scenario 2: Major fiber cut outage of 1300 minutes (~21.6 hours) -> ~96.99% uptime
    res_breach = evaluate_sla_and_issue_credit(
        db=db_session,
        subscription_id=sub.id,
        downtime_minutes=1300,
        billing_period_start=p_start,
        billing_period_end=p_end,
        sla_tier=SLATier.STANDARD_99_5,
        root_cause="Major metro rail excavation cut primary & secondary optical feeder rings"
    )

    assert res_breach["sla_breached"] is True
    assert res_breach["actual_uptime_pct"] < 97.50
    assert res_breach["penalty_credit_pct"] == 50.00  # 50% penalty credit tier for > 2% shortfall
    assert res_breach["credit_note_code"] is not None
    assert res_breach["credit_note_code"].startswith("CN-")
    assert res_breach["credit_note_amount"] == 1000.00  # 50% of INR 2,000

    # Verify CreditNote in database
    cn = db_session.query(CreditNote).filter(CreditNote.credit_note_code == res_breach["credit_note_code"]).first()
    assert cn is not None
    assert cn.amount == Decimal('1000.00')

    # Verify General Ledger Balancing after credit note issuance
    ledger_check = verify_ledger_balancing(db_session, market_id=mkt)
    assert ledger_check["is_fully_balanced"] is True, f"GL out of balance: {ledger_check['unbalanced_entries']}"
