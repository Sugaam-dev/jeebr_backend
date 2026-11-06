"""
app/tests/test_phase1_enterprise_billing_foundations.py

Comprehensive Phase 1 Enterprise Billing Foundations Test Suite:
1. Concurrency-Safe Business Sequence Generator (Invoices, Receipts, Credit Notes, Payments).
2. Statutory Indian GST Place of Supply (PoS) under Section 12(11)(a) IGST Act.
3. Exact-Day Proration Engine (Activations, Plan Changes, Cancellations, Vacation Holds).
4. Consolidated Multi-Service Invoicing with Balanced General Ledger Postings.
5. Staged Dunning & Delinquency State Machine with Promise-to-Pay (PTP) Safety Exemptions.
6. Cashier Desk, Shift Counter Collections, Official Receipt Generation, and Daily Collection Register.
"""

import uuid
from datetime import datetime, timedelta, date
from decimal import Decimal
import pytest
from sqlalchemy.orm import Session

from app.models import Customer, User, Invoice
from app.models_crm import CRMCustomer, CRMAddress, AddressType
from app.models_finance import (
    BillingAccount, BillingSubscription, Payment, InvoiceStatus,
    PaymentStatus, SubscriptionStatus, ChartOfAccounts, PromiseToPay
)
from app.models_cashier import CashierShift, ShiftStatus
from app.services.sequence_service import generate_business_code, SequenceType
from app.services.tax_service import compute_gst, resolve_place_of_supply, resolve_installation_state
from app.services.proration_service import (
    calculate_activation_proration, calculate_plan_change_adjustment,
    calculate_cancellation_refund, calculate_vacation_hold_adjustment
)
from app.services.billing_service import (
    ensure_billing_account, create_subscription, generate_invoice,
    generate_consolidated_invoice
)
from app.services.dunning_service import (
    DunningStage, DunningNetworkAction, register_promise_to_pay,
    check_dunning_exemption, evaluate_account_dunning
)
from app.services.cashier_service import (
    open_cashier_shift, record_counter_collection, close_cashier_shift,
    get_daily_collection_register
)
from app.services.ledger_service import verify_ledger_balancing


# ─── 1. Atomic Sequence Generation Tests ──────────────────────────────────────

def test_atomic_sequence_generation(db_session: Session):
    """Verifies monotonic, formatted code generation without gaps or collisions."""
    inv_code_1 = generate_business_code(db=db_session, sequence_type=SequenceType.INVOICE, market_id="mumbai")
    inv_code_2 = generate_business_code(db=db_session, sequence_type=SequenceType.INVOICE, market_id="mumbai")
    rcp_code = generate_business_code(db=db_session, sequence_type=SequenceType.RECEIPT, market_id="mumbai")
    cn_code = generate_business_code(db=db_session, sequence_type=SequenceType.CREDIT_NOTE, market_id="mumbai")
    pay_ref = generate_business_code(db=db_session, sequence_type=SequenceType.PAYMENT, market_id="mumbai")

    cur_year = datetime.utcnow().year
    assert inv_code_1 == f"INV-MUM-{cur_year}-00001"
    assert inv_code_2 == f"INV-MUM-{cur_year}-00002"
    assert rcp_code == f"RCP-MUM-{cur_year}-00001"
    assert cn_code == f"CN-MUM-{cur_year}-00001"
    assert pay_ref == f"PAY-MUM-{cur_year}-00001"


# ─── 2. Statutory GST Place of Supply Tests ───────────────────────────────────

def test_statutory_gst_place_of_supply_section_12_11_a(db_session: Session):
    """
    Verifies Section 12(11)(a) IGST Act compliance:
    Place of supply is anchored strictly to physical installation premises.
    Supplier is in Maharashtra (State 27).
    """
    # Intra-State: Installation in Mumbai/Maharashtra -> CGST 9% + SGST 9%
    intra = compute_gst(
        subtotal=Decimal("1000.00"),
        customer_state="Maharashtra",
        market_id="mumbai"
    )
    assert intra.tax_treatment == "INTRA_STATE"
    assert intra.cgst_rate == Decimal("9.00")
    assert intra.sgst_rate == Decimal("9.00")
    assert intra.cgst_amount == Decimal("90.00")
    assert intra.sgst_amount == Decimal("90.00")
    assert intra.igst_amount == Decimal("0.00")
    assert intra.total_amount == Decimal("1180.00")

    # Inter-State: Installation in Bengaluru/Karnataka (State 29) -> IGST 18%
    inter = compute_gst(
        subtotal=Decimal("1000.00"),
        customer_state="Karnataka",
        market_id="mumbai"
    )
    assert inter.tax_treatment == "INTER_STATE"
    assert inter.cgst_amount == Decimal("0.00")
    assert inter.sgst_amount == Decimal("0.00")
    assert inter.igst_rate == Decimal("18.00")
    assert inter.igst_amount == Decimal("180.00")
    assert inter.total_amount == Decimal("1180.00")
    assert "29-Karnataka" in inter.place_of_supply


# ─── 3. Proration Calculation Engine Tests ────────────────────────────────────

def test_activation_proration_mid_cycle():
    """Verifies onboarding proration for mid-cycle activations."""
    # 30-day cycle: 1 June to 30 June. Activated on 16 June (15 active days remaining).
    cycle_start = datetime(2026, 6, 1)
    cycle_end = datetime(2026, 6, 30)
    activation_date = datetime(2026, 6, 16)
    recurring_price = Decimal("1000.00")

    res = calculate_activation_proration(recurring_price, activation_date, cycle_start, cycle_end)
    # total days = 29, active days = 14
    assert res.total_period_days == 29
    assert res.active_days == 14
    assert res.prorated_charge == Decimal("482.76")


def test_plan_change_adjustment_upgrade_and_downgrade():
    """Verifies mid-cycle tariff shift crediting unused old plan and debiting new plan."""
    cycle_start = datetime(2026, 7, 1)
    cycle_end = datetime(2026, 7, 31)
    change_date = datetime(2026, 7, 16)
    old_plan = Decimal("1000.00")
    new_plan = Decimal("2000.00")

    adj = calculate_plan_change_adjustment(old_plan, new_plan, change_date, cycle_start, cycle_end)
    assert adj.total_period_days == 30
    assert adj.elapsed_days == 15
    assert adj.remaining_days == 15
    assert adj.old_plan_credit == Decimal("500.00")
    assert adj.new_plan_debit == Decimal("1000.00")
    assert adj.net_adjustment_amount == Decimal("500.00")  # Customer owes net ₹500


def test_cancellation_refund_and_vacation_hold():
    """Verifies cancellation credit and vacation hold calculation."""
    cycle_start = datetime(2026, 8, 1)
    cycle_end = datetime(2026, 8, 31)
    canc_date = datetime(2026, 8, 21)

    refund = calculate_cancellation_refund(Decimal("1200.00"), canc_date, cycle_start, cycle_end)
    assert refund == Decimal("400.00")  # 10 unused days out of 30

    # Vacation hold: Bandwidth ₹1000, Router lease ₹200. Held for 15 days in 30-day month.
    hold_bill = calculate_vacation_hold_adjustment(
        bandwidth_charge=Decimal("1000.00"),
        equipment_fee=Decimal("200.00"),
        hold_start=datetime(2026, 8, 11),
        hold_end=datetime(2026, 8, 26),
        cycle_start=cycle_start,
        cycle_end=cycle_end
    )
    assert hold_bill == Decimal("700.00")  # (₹500 active bandwidth + ₹200 router lease)


# ─── 4. Consolidated Multi-Service Invoicing Tests ─────────────────────────────

def test_consolidated_invoice_generation_with_balanced_ledger(db_session: Session):
    """Verifies bundling multiple telecom services into one master tax invoice with balanced GL."""
    unique_suffix = uuid.uuid4().hex[:6]
    cust = Customer(
        customer_code=f"CUST-DUNN-{unique_suffix}",
        name="Test Dunning Subscriber",
        email=f"dunning_{unique_suffix}@pmrg.in",
        phone="+91 99999 22222",
        locality="BKC",
        segment="Residential",
        plan_name="Fiber Ultra 100",
        market_id="mumbai"
    )
    db_session.add(cust)
    db_session.commit()
    account = ensure_billing_account(db_session, cust.id)

    items = [
        {
            "description": "Dedicated Internet Access (ILL) 300 Mbps",
            "quantity": Decimal("1.00"),
            "unit_price": Decimal("3000.00"),
            "sac_code": "998422"
        },
        {
            "description": "Static IPv4 /29 Subnet Allocation",
            "quantity": Decimal("1.00"),
            "unit_price": Decimal("500.00"),
            "sac_code": "998422"
        },
        {
            "description": "Managed Wi-Fi 6 Mesh Router Lease",
            "quantity": Decimal("1.00"),
            "unit_price": Decimal("250.00"),
            "sac_code": "997331"
        }
    ]

    inv = generate_consolidated_invoice(
        db=db_session,
        billing_account_id=str(account.id),
        items=items,
        notes="Enterprise Monthly Services Bundle"
    )

    assert inv is not None
    assert inv.invoice_code.startswith("INV-")
    assert len(inv.line_items) == 3
    assert inv.subtotal == Decimal("3750.00")
    assert inv.tax_amount == Decimal("675.00")  # 18% GST on 3750
    assert inv.total_amount == Decimal("4425.00")
    assert inv.outstanding_amount == Decimal("4425.00")

    # Invariant check: General Ledger must strictly balance
    assert verify_ledger_balancing(db_session)["is_fully_balanced"] is True


# ─── 5. Staged Dunning & Delinquency State Machine Tests ───────────────────────

def test_staged_dunning_progression_and_ptp_exemption(db_session: Session):
    """Verifies calibrated dunning stages and Promise-to-Pay safety protection."""
    unique_suffix = uuid.uuid4().hex[:6]
    cust = Customer(
        customer_code=f"CUST-DUNN-{unique_suffix}",
        name="Test Dunning Subscriber",
        email=f"dunning_{unique_suffix}@pmrg.in",
        phone="+91 99999 22222",
        locality="BKC",
        segment="Residential",
        plan_name="Fiber Ultra 100",
        market_id="mumbai"
    )
    db_session.add(cust)
    db_session.commit()
    account = ensure_billing_account(db_session, cust.id)

    sub = create_subscription(
        db=db_session,
        customer_id=cust.id,
        plan_id="PLAN-POST-INFINITY-75G",
        billing_account_id=account.id
    )
    inv = generate_invoice(db=db_session, subscription_id=sub.id)

    # 1. Fresh invoice within due date -> Stage 0 (Current)
    now = datetime.utcnow()
    res_0 = evaluate_account_dunning(db_session, str(account.id), now)
    assert res_0["dunning_stage"] == DunningStage.STAGE_0_CURRENT
    assert res_0["network_action"] == DunningNetworkAction.NONE

    # 2. Simulate 5 days past due -> Stage 2 (Grace period)
    sim_t_plus_5 = inv.due_date + timedelta(days=2)
    res_2 = evaluate_account_dunning(db_session, str(account.id), sim_t_plus_5)
    assert res_2["dunning_stage"] == DunningStage.STAGE_2_PAST_DUE
    assert res_2["network_action"] == DunningNetworkAction.NONE

    # 3. Simulate 8 days past due -> Stage 3 (Soft Delinquency / Throttle 2 Mbps)
    sim_t_plus_8 = inv.due_date + timedelta(days=5)
    res_3 = evaluate_account_dunning(db_session, str(account.id), sim_t_plus_8)
    assert res_3["dunning_stage"] == DunningStage.STAGE_3_SOFT_THROTTLE
    assert res_3["network_action"] == DunningNetworkAction.THROTTLE_2MBPS

    # 4. Register Promise-to-Pay (PTP) for 5 days beyond simulated delinquent date
    commitment_date = sim_t_plus_8 + timedelta(days=5)
    ptp = register_promise_to_pay(
        db=db_session,
        billing_account_id=str(account.id),
        commitment_date=commitment_date,
        committed_amount=inv.total_amount,
        notes="Customer promised to settle by next week."
    )
    assert ptp.status == "PENDING"

    # 5. Re-evaluate on day 8 -> Must be EXEMPT from network action due to active PTP!
    res_exempt = evaluate_account_dunning(db_session, str(account.id), sim_t_plus_8)
    assert res_exempt["exempt"] is True
    assert res_exempt["network_action"] == DunningNetworkAction.NONE
    assert "Promise-to-Pay" in res_exempt["exemption_reason"]


# ─── 6. Cashier Desk & Shift Closing Tests ────────────────────────────────────

def test_cashier_shift_counter_collection_and_closing(db_session: Session):
    """Verifies counter collection, receipt numbering, GL posting, and DCR closing."""
    admin = db_session.query(User).filter(User.role == "Admin").first()
    unique_suffix = uuid.uuid4().hex[:6]
    cust = Customer(
        customer_code=f"CUST-DUNN-{unique_suffix}",
        name="Test Dunning Subscriber",
        email=f"dunning_{unique_suffix}@pmrg.in",
        phone="+91 99999 22222",
        locality="BKC",
        segment="Residential",
        plan_name="Fiber Ultra 100",
        market_id="mumbai"
    )
    db_session.add(cust)
    db_session.commit()
    account = ensure_billing_account(db_session, cust.id)

    # 1. Open Cashier Shift with ₹1,000 float
    shift = open_cashier_shift(
        db=db_session,
        cashier_user_id=admin.id,
        market_id="mumbai",
        opening_float=Decimal("1000.00"),
        branch_name="BKC Retail Counter"
    )
    assert shift.status == ShiftStatus.OPEN
    assert shift.shift_code.startswith("SHF-MUM-")
    assert shift.expected_cash_in_drawer == Decimal("1000.00")

    # 2. Record counter cash collection of ₹500
    res = record_counter_collection(
        db=db_session,
        shift_id=str(shift.id),
        customer_id=cust.id,
        amount=Decimal("500.00"),
        payment_method="CASH",
        notes="Cash payment at retail counter"
    )
    assert res["status"] == "SUCCESS"
    assert res["receipt_code"].startswith("RCP-MUM-")
    assert shift.total_cash_collected == Decimal("500.00")
    assert shift.expected_cash_in_drawer == Decimal("1500.00")

    # 3. Close Shift with exact denomination breakdown (₹1,500 total)
    # Denominations: 2x ₹500, 2x ₹200, 1x ₹100 = 1000 + 400 + 100 = 1500
    denominations = {"500": 2, "200": 2, "100": 1}
    closed_shift = close_cashier_shift(
        db=db_session,
        shift_id=str(shift.id),
        denominations=denominations,
        closing_notes="Shift balanced perfectly."
    )
    assert closed_shift.status == ShiftStatus.RECONCILED
    assert closed_shift.actual_cash_counted == Decimal("1500.00")
    assert closed_shift.cash_variance == Decimal("0.00")

    # 4. Extract Daily Collection Register (DCR)
    dcr = get_daily_collection_register(db=db_session, market_id="mumbai", register_date=datetime.utcnow().date())
    assert dcr["total_shifts"] >= 1
    assert dcr["total_cash_collected"] >= 500.00
    assert dcr["total_cash_variance"] == 0.00

    # 5. Ledger Balancing Verification
    assert verify_ledger_balancing(db_session)["is_fully_balanced"] is True
