"""
app/services/lifecycle_service.py

SentinelOS Complex Telecom Lifecycle & Equipment Inventory Engine.
Implements:
1. Vacation Holds & Temporary Suspensions (Safe custody line retention & proration adjustments).
2. Geographic Relocations (Premises shift, fiber feasibility, and Section 12(11)(a) PoS tax recalibration).
3. CPE & Hardware Asset Lifecycle (Rental billing, refundable security deposits, and damage deductions).
4. Tariff Plan Upgrades & Downgrades (Immediate proration vs next-cycle scheduled transitions).
5. Automated SLA Breach Outage Evaluation & Credit Note Issuance.
"""

import uuid
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_

from app.models import Customer, Invoice
from app.models_crm import CRMCustomer, CRMAddress, AddressType
from app.models_finance import (
    BillingAccount, BillingSubscription, CreditNote, InvoiceLineItem,
    BillingAccountStatus, BillingType, SubscriptionStatus, InvoiceStatus,
    ChartOfAccounts
)
from app.models_inventory import InventoryDevice
from app.models_lifecycle import (
    VacationHold, PremisesRelocation, CPEAssetAssignment, ScheduledPlanChange,
    SLAIncidentRecord, VacationHoldStatus, RelocationStatus, CPEOwnershipType,
    CPEAssignmentStatus, PlanChangeMode, PlanChangeStatus, SLAIncidentStatus
)
from app.models_cpq import SLATier
from app.services.proration_service import (
    calculate_plan_change_adjustment, calculate_vacation_hold_adjustment, calculate_daily_rate
)
from app.services.billing_service import _format_money, issue_credit_note
from app.services.tax_service import compute_gst, resolve_place_of_supply, resolve_installation_state
from app.services.ledger_service import quantize_money, post_journal_entry
from app.services.sequence_service import generate_business_code, SequenceType
from app.services.provisioning_catalog import get_service_plan_by_id

logger = logging.getLogger(__name__)


# ─── 1. Vacation Holds & Temporary Suspensions ───────────────────────────────

def apply_vacation_hold(
    db: Session,
    subscription_id: Any,
    hold_start: datetime,
    expected_resume: datetime,
    retention_fee_monthly: Decimal = Decimal('150.00'),
    reason: Optional[str] = None
) -> VacationHold:
    """
    Suspends subscriber broadband service temporarily while reserving physical optical port.
    Applies safe custody retention fee (e.g. INR 150/month) and credits unserved bandwidth for the period.
    """
    sub = db.query(BillingSubscription).filter(
        (BillingSubscription.id == subscription_id) | (BillingSubscription.subscription_code == str(subscription_id))
    ).first()
    if not sub:
        raise ValueError(f"BillingSubscription '{subscription_id}' not found.")

    account = sub.billing_account
    if not account:
        raise ValueError("BillingAccount associated with subscription not found.")

    if expected_resume <= hold_start:
        raise ValueError("Expected resume date must be strictly after hold start date.")

    hold_duration_days = (expected_resume - hold_start).days
    if hold_duration_days > 90:
        raise ValueError(f"Vacation hold duration ({hold_duration_days} days) exceeds maximum statutory limit of 90 days.")

    original_price = quantize_money(sub.unit_price)

    # Calculate unserved bandwidth credit for the current active billing cycle
    credit_amount = Decimal('0.00')
    if sub.current_period_start and sub.current_period_end:
        cs = sub.current_period_start
        ce = sub.current_period_end
        if hold_start < ce:
            total_cycle_days = max(1, (ce - cs).days)
            unused_days = max(0, min(total_cycle_days, (ce - hold_start).days))
            daily_rate = calculate_daily_rate(original_price, total_cycle_days)
            credit_amount = quantize_money(daily_rate * Decimal(str(unused_days)))

    # Post balanced ledger entry for unserved bandwidth credit
    if credit_amount > Decimal('0.00'):
        account.balance = quantize_money(account.balance) + credit_amount
        post_journal_entry(
            db=db,
            market_id=sub.market_id,
            reference_type="VACATION_HOLD_CREDIT",
            reference_id=f"HLD-{str(sub.id)[:8]}",
            description=f"Vacation hold unserved bandwidth credit for subscription {sub.subscription_code}",
            lines=[
                {
                    "account_code": ChartOfAccounts.VOUCHER_DISCOUNTS,
                    "debit": credit_amount,
                    "credit": Decimal('0.00'),
                    "currency": sub.currency
                },
                {
                    "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
                    "debit": Decimal('0.00'),
                    "credit": credit_amount,
                    "currency": sub.currency
                }
            ],
            allow_idempotent_replay=True
        )

    # Update subscription to suspended with nominal retention fee
    sub.status = SubscriptionStatus.SUSPENDED
    sub.unit_price = quantize_money(retention_fee_monthly)
    sub.updated_at = datetime.utcnow()

    hold = VacationHold(
        id=uuid.uuid4(),
        subscription_id=sub.id,
        billing_account_id=account.id,
        market_id=sub.market_id,
        hold_start_date=hold_start,
        expected_resume_date=expected_resume,
        retention_fee_monthly=quantize_money(retention_fee_monthly),
        original_monthly_price=original_price,
        credit_adjustment_amount=credit_amount,
        status=VacationHoldStatus.ACTIVE_HOLD,
        reason=reason or "Customer requested vacation hold",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(hold)
    db.commit()
    db.refresh(hold)
    return hold


def resume_vacation_hold(
    db: Session,
    hold_id: Any,
    resume_date: Optional[datetime] = None
) -> VacationHold:
    """
    Restores suspended subscriber service back to ACTIVE status and restores normal tariff rate.
    """
    hid = uuid.UUID(str(hold_id)) if not isinstance(hold_id, uuid.UUID) else hold_id
    hold = db.query(VacationHold).filter(VacationHold.id == hid).first()
    if not hold:
        raise ValueError(f"VacationHold '{hold_id}' not found.")

    if hold.status != VacationHoldStatus.ACTIVE_HOLD:
        raise ValueError(f"VacationHold '{hold_id}' is already in status '{hold.status}'.")

    sub = db.query(BillingSubscription).filter(BillingSubscription.id == hold.subscription_id).first()
    if not sub:
        raise ValueError("Subscription associated with vacation hold not found.")

    actual_resume = resume_date or datetime.utcnow()

    # Restore subscription
    sub.status = SubscriptionStatus.ACTIVE
    sub.unit_price = hold.original_monthly_price
    sub.updated_at = datetime.utcnow()

    hold.actual_resume_date = actual_resume
    hold.status = VacationHoldStatus.RESUMED
    hold.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(hold)
    return hold


# ─── 2. Geographic Premises Relocation & Section 12(11)(a) Recalibration ─────

def initiate_premises_relocation(
    db: Session,
    customer_id: int,
    subscription_id: Any,
    new_address_data: Dict[str, Any],
    shift_charge: Decimal = Decimal('1000.00'),
    operator_id: Optional[int] = None
) -> PremisesRelocation:
    """
    Manages subscriber relocation to new physical installation premises:
    1. Creates authoritative CRMAddress installation record.
    2. Recalibrates Section 12(11)(a) Place of Supply (PoS) and interstate vs intrastate GST tax treatment.
    3. Issues shift charge tax invoice with balanced double-entry ledger postings.
    """
    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise ValueError(f"Customer #{customer_id} not found.")

    sub = db.query(BillingSubscription).filter(
        (BillingSubscription.id == subscription_id) | (BillingSubscription.subscription_code == str(subscription_id))
    ).first()
    if not sub:
        raise ValueError(f"BillingSubscription '{subscription_id}' not found.")

    crm_cust = db.query(CRMCustomer).filter(
        (CRMCustomer.legacy_customer_id == cust.id) | (CRMCustomer.customer_number == cust.customer_code)
    ).first()

    account = sub.billing_account

    # 1. Capture old installation address and Place of Supply
    old_pos = resolve_place_of_supply(
        market_id=sub.market_id,
        installation_state=resolve_installation_state(db, cust.id)
    )

    old_addr = None
    if crm_cust:
        old_addr = db.query(CRMAddress).filter(
            CRMAddress.crm_customer_id == crm_cust.id,
            CRMAddress.address_type == AddressType.INSTALLATION,
            CRMAddress.is_archived == False
        ).first()

    # 2. Create new CRMAddress for new installation premises
    new_addr = CRMAddress(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id if crm_cust else uuid.uuid4(),
        address_type=AddressType.INSTALLATION,
        address_line1=new_address_data.get("address_line1", "Flat 402, High-Tech Residency"),
        address_line2=new_address_data.get("address_line2", ""),
        city=new_address_data.get("city", "Mumbai"),
        state=new_address_data.get("state", "Maharashtra"),
        postal_code=new_address_data.get("postal_code", "400051"),
        is_archived=False,
        created_at=datetime.utcnow()
    )
    db.add(new_addr)
    db.flush()

    # 3. Recalibrate Section 12(11)(a) Place of Supply strictly against new installation address
    new_pos = resolve_place_of_supply(
        market_id=sub.market_id,
        installation_state=new_addr.state
    )

    # 4. Generate Relocation Business Code
    rel_code = generate_business_code(
        db=db,
        sequence_type=SequenceType.RELOCATION,
        market_id=sub.market_id
    )

    # 5. Issue Shift Charge Tax Invoice
    dec_shift = quantize_money(shift_charge)
    shift_inv = None
    if dec_shift > Decimal('0.00'):
        tax_calc = compute_gst(
            subtotal=dec_shift,
            discount_amount=Decimal('0.00'),
            market_id=sub.market_id,
            customer_state=new_addr.state,
            currency=sub.currency
        )
        inv_code = generate_business_code(db=db, sequence_type=SequenceType.INVOICE, market_id=sub.market_id)
        due_d = datetime.utcnow() + timedelta(days=account.payment_term_days if account else 15)

        shift_inv = Invoice(
            market_id=sub.market_id,
            invoice_code=inv_code,
            customer_id=cust.id,
            billing_account_id=account.id if account else None,
            subscription_id=sub.id,
            plan_name="Premises Shift Fee",
            transaction_type="Shift Charge",
            payment_method="UPI",
            billed_amount=float(tax_calc.total_amount),
            expected_amount=float(tax_calc.total_amount),
            due_date=due_d,
            status=InvoiceStatus.ISSUED,
            currency=sub.currency,
            subtotal=dec_shift,
            tax_amount=tax_calc.total_tax,
            discount_amount=Decimal('0.00'),
            total_amount=tax_calc.total_amount,
            paid_amount=Decimal('0.00'),
            outstanding_amount=tax_calc.total_amount,
            tax_treatment=tax_calc.tax_treatment,
            cgst_amount=tax_calc.cgst_amount,
            sgst_amount=tax_calc.sgst_amount,
            igst_amount=tax_calc.igst_amount,
            place_of_supply=new_pos,
            supplier_gstin=tax_calc.supplier_gstin,
            customer_gstin=tax_calc.customer_gstin,
            billing_period_start=datetime.utcnow(),
            billing_period_end=datetime.utcnow(),
            idempotency_key=f"shift_{rel_code}",
            notes=f"Premises relocation charge: {rel_code}",
            created_at=datetime.utcnow()
        )
        db.add(shift_inv)
        db.flush()

        # Detailed Line Item
        item = InvoiceLineItem(
            id=uuid.uuid4(),
            invoice_id=shift_inv.id,
            subscription_id=sub.id,
            description="Broadband Line Re-termination & Premises Shift Charge",
            quantity=Decimal('1.00'),
            unit_price=dec_shift,
            amount=dec_shift,
            tax_rate=tax_calc.cgst_rate + tax_calc.sgst_rate if tax_calc.tax_treatment == 'INTRA_STATE' else tax_calc.igst_rate,
            tax_amount=tax_calc.total_tax,
            discount_amount=Decimal('0.00'),
            total_amount=tax_calc.total_amount,
            currency=sub.currency,
            created_at=datetime.utcnow()
        )
        db.add(item)
        db.flush()

        # Post balanced double-entry General Ledger entry
        ledger_lines = [
            {
                "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
                "debit": tax_calc.total_amount,
                "credit": Decimal('0.00'),
                "currency": sub.currency
            },
            {
                "account_code": ChartOfAccounts.EQUIPMENT_REVENUE,
                "debit": Decimal('0.00'),
                "credit": dec_shift,
                "currency": sub.currency
            }
        ]
        if tax_calc.total_tax > Decimal('0.00'):
            ledger_lines.append({
                "account_code": ChartOfAccounts.TAX_PAYABLE_GST,
                "debit": Decimal('0.00'),
                "credit": tax_calc.total_tax,
                "currency": sub.currency
            })

        post_journal_entry(
            db=db,
            market_id=sub.market_id,
            reference_type="RELOCATION_INVOICE",
            reference_id=inv_code,
            description=f"Shift charge invoice {inv_code} for relocation {rel_code}",
            lines=ledger_lines,
            created_by_id=operator_id,
            allow_idempotent_replay=True
        )

    relocation = PremisesRelocation(
        id=uuid.uuid4(),
        relocation_code=rel_code,
        customer_id=cust.id,
        crm_customer_id=crm_cust.id if crm_cust else None,
        subscription_id=sub.id,
        market_id=sub.market_id,
        old_address_id=old_addr.id if old_addr else None,
        new_address_id=new_addr.id,
        old_place_of_supply=old_pos,
        new_place_of_supply=new_pos,
        feasibility_status="FEASIBLE",
        shift_charge=dec_shift,
        shift_invoice_id=shift_inv.id if shift_inv else None,
        status=RelocationStatus.REQUESTED,
        requested_date=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(relocation)
    db.commit()
    db.refresh(relocation)
    return relocation


def complete_premises_relocation(
    db: Session,
    relocation_id: Any
) -> PremisesRelocation:
    """Marks relocation work order completed and archives previous address."""
    rid = uuid.UUID(str(relocation_id)) if not isinstance(relocation_id, uuid.UUID) else relocation_id
    relocation = db.query(PremisesRelocation).filter(PremisesRelocation.id == rid).first()
    if not relocation:
        raise ValueError(f"PremisesRelocation '{relocation_id}' not found.")

    if relocation.old_address_id:
        old_addr = db.query(CRMAddress).filter(CRMAddress.id == relocation.old_address_id).first()
        if old_addr:
            old_addr.is_archived = True

    relocation.status = RelocationStatus.COMPLETED
    relocation.completed_date = datetime.utcnow()
    relocation.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(relocation)
    return relocation


# ─── 3. CPE Hardware Asset Management (Rental & Security Deposit) ────────────

def assign_cpe_device(
    db: Session,
    device_id: int,
    customer_id: int,
    subscription_id: Optional[Any] = None,
    ownership_type: str = CPEOwnershipType.ISP_RENTAL,
    monthly_rental: Decimal = Decimal('0.00'),
    security_deposit: Decimal = Decimal('0.00')
) -> CPEAssetAssignment:
    """
    Authoritative assignment of serialized hardware (ONT, Router) to customer billing.
    Manages security deposit liability tracking under General Ledger Account 2100.
    """
    device = db.query(InventoryDevice).filter(InventoryDevice.id == device_id).first()
    if not device:
        raise ValueError(f"InventoryDevice #{device_id} not found.")

    if device.status not in ("IN_STOCK", "RETURNED"):
        raise ValueError(f"Device #{device_id} is in status '{device.status}' and cannot be assigned.")

    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise ValueError(f"Customer #{customer_id} not found.")

    crm_cust = db.query(CRMCustomer).filter(
        (CRMCustomer.legacy_customer_id == cust.id) | (CRMCustomer.customer_number == cust.customer_code)
    ).first()

    account = db.query(BillingAccount).filter(BillingAccount.customer_id == cust.id).first()
    if not account:
        raise ValueError("BillingAccount not found for customer.")

    sub = None
    if subscription_id:
        sub = db.query(BillingSubscription).filter(
            (BillingSubscription.id == subscription_id) | (BillingSubscription.subscription_code == str(subscription_id))
        ).first()

    dec_deposit = quantize_money(security_deposit)
    dec_rental = quantize_money(monthly_rental)

    # Post balanced ledger entry for refundable security deposit (Liability 2100)
    if ownership_type == CPEOwnershipType.SECURITY_DEPOSIT and dec_deposit > Decimal('0.00'):
        post_journal_entry(
            db=db,
            market_id=account.market_id,
            reference_type="CPE_SECURITY_DEPOSIT",
            reference_id=f"DEP-{device.serial_number}",
            description=f"Security deposit collected for CPE {device.serial_number} from customer #{cust.id}",
            lines=[
                {
                    "account_code": ChartOfAccounts.CASH_CLEARING,
                    "debit": dec_deposit,
                    "credit": Decimal('0.00'),
                    "currency": account.currency
                },
                {
                    "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
                    "debit": Decimal('0.00'),
                    "credit": dec_deposit,
                    "currency": account.currency
                }
            ],
            allow_idempotent_replay=True
        )

    # Update device status
    device.status = "INSTALLED"
    device.assigned_customer_id = cust.id
    device.installed_at = datetime.utcnow()
    device.updated_at = datetime.utcnow()

    assignment = CPEAssetAssignment(
        id=uuid.uuid4(),
        device_id=device.id,
        customer_id=cust.id,
        crm_customer_id=crm_cust.id if crm_cust else None,
        billing_account_id=account.id,
        subscription_id=sub.id if sub else None,
        market_id=account.market_id,
        ownership_type=ownership_type,
        monthly_rental_price=dec_rental,
        security_deposit_amount=dec_deposit,
        deposit_paid=(ownership_type == CPEOwnershipType.SECURITY_DEPOSIT and dec_deposit > Decimal('0.00')),
        deposit_refunded=False,
        damage_deduction=Decimal('0.00'),
        status=CPEAssignmentStatus.ASSIGNED,
        assigned_date=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


def return_cpe_device(
    db: Session,
    assignment_id: Any,
    condition: str = "GOOD",
    damage_charge: Decimal = Decimal('0.00')
) -> Dict[str, Any]:
    """
    Handles CPE hardware asset return, condition audit, damage deduction, and deposit refund.
    Maintains balanced General Ledger entries across Asset, Liability, and Other Income accounts.
    """
    aid = uuid.UUID(str(assignment_id)) if not isinstance(assignment_id, uuid.UUID) else assignment_id
    assignment = db.query(CPEAssetAssignment).filter(CPEAssetAssignment.id == aid).first()
    if not assignment:
        raise ValueError(f"CPEAssetAssignment '{assignment_id}' not found.")

    if assignment.status != CPEAssignmentStatus.ASSIGNED:
        raise ValueError(f"Assignment is already in status '{assignment.status}'.")

    device = db.query(InventoryDevice).filter(InventoryDevice.id == assignment.device_id).first()
    account = assignment.billing_account

    dec_damage = min(quantize_money(damage_charge), assignment.security_deposit_amount)
    net_refund = max(Decimal('0.00'), assignment.security_deposit_amount - dec_damage)

    # Deposit refund and damage recovery General Ledger postings
    if assignment.deposit_paid and assignment.security_deposit_amount > Decimal('0.00'):
        ledger_lines = [
            {
                "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
                "debit": assignment.security_deposit_amount,
                "credit": Decimal('0.00'),
                "currency": account.currency
            }
        ]
        if net_refund > Decimal('0.00'):
            ledger_lines.append({
                "account_code": ChartOfAccounts.CASH_CLEARING,
                "debit": Decimal('0.00'),
                "credit": net_refund,
                "currency": account.currency
            })
        if dec_damage > Decimal('0.00'):
            ledger_lines.append({
                "account_code": ChartOfAccounts.DAMAGE_REVENUE,
                "debit": Decimal('0.00'),
                "credit": dec_damage,
                "currency": account.currency
            })

        post_journal_entry(
            db=db,
            market_id=assignment.market_id,
            reference_type="CPE_DEPOSIT_REFUND",
            reference_id=f"REF-DEP-{str(assignment.id)[:8]}",
            description=f"Security deposit settlement for returned device {device.serial_number if device else ''}",
            lines=ledger_lines,
            allow_idempotent_replay=True
        )

    # Update device and assignment records
    if device:
        device.status = "RETURNED" if condition != "DAMAGED" else "DAMAGED"
        device.returned_at = datetime.utcnow()
        device.assigned_customer_id = None
        device.updated_at = datetime.utcnow()

    assignment.status = CPEAssignmentStatus.RETURNED if condition != "DAMAGED" else CPEAssignmentStatus.DAMAGED
    assignment.condition_on_return = condition
    assignment.returned_date = datetime.utcnow()
    assignment.deposit_refunded = True
    assignment.damage_deduction = dec_damage
    assignment.updated_at = datetime.utcnow()

    db.commit()
    return {
        "status": assignment.status,
        "device_serial": device.serial_number if device else "",
        "condition": condition,
        "original_deposit": float(assignment.security_deposit_amount),
        "damage_deduction": float(dec_damage),
        "net_refund_amount": float(net_refund),
        "returned_date": assignment.returned_date.isoformat()
    }


# ─── 4. Tariff Plan Upgrades & Downgrades ────────────────────────────────────

def change_subscription_plan(
    db: Session,
    subscription_id: Any,
    target_plan_id: str,
    effective_mode: str = PlanChangeMode.IMMEDIATE
) -> Dict[str, Any]:
    """
    Executes rate plan upgrade or downgrade:
    - IMMEDIATE: Prorates unused old plan and debits new plan for remaining cycle days.
      Issues adjustment invoice or applies balance credit.
    - NEXT_CYCLE: Schedules plan change to execute on next cycle start without mid-cycle prorations.
    """
    sub = db.query(BillingSubscription).filter(
        (BillingSubscription.id == subscription_id) | (BillingSubscription.subscription_code == str(subscription_id))
    ).first()
    if not sub:
        raise ValueError(f"BillingSubscription '{subscription_id}' not found.")

    account = sub.billing_account
    new_plan = get_service_plan_by_id(target_plan_id)
    if not new_plan:
        raise ValueError(f"Target tariff plan '{target_plan_id}' not found in catalog.")

    new_plan_price = quantize_money(new_plan.get("price_inr") or new_plan.get("price", 0))
    old_plan_price = quantize_money(sub.unit_price)

    change_type = "UPGRADE" if new_plan_price >= old_plan_price else "DOWNGRADE"
    now = datetime.utcnow()

    if effective_mode == PlanChangeMode.IMMEDIATE:
        # Calculate exact-day mid-cycle adjustment
        adj = calculate_plan_change_adjustment(
            old_plan_price=old_plan_price,
            new_plan_price=new_plan_price,
            change_date=now,
            cycle_start=sub.current_period_start,
            cycle_end=sub.current_period_end
        )

        sub.plan_id = new_plan.get("plan_id") or new_plan.get("id")
        sub.plan_name = new_plan["name"]
        sub.unit_price = new_plan_price
        sub.updated_at = now

        adj_invoice_id = None
        # If customer owes upgrade adjustment difference
        if adj.net_adjustment_amount > Decimal('0.00'):
            tax_calc = compute_gst(
                subtotal=adj.net_adjustment_amount,
                discount_amount=Decimal('0.00'),
                market_id=sub.market_id,
                customer_state=resolve_installation_state(db, sub.customer_id) or sub.market_id,
                currency=sub.currency
            )
            inv_code = generate_business_code(db=db, sequence_type=SequenceType.INVOICE, market_id=sub.market_id)
            adj_inv = Invoice(
                market_id=sub.market_id,
                invoice_code=inv_code,
                customer_id=sub.customer_id,
                billing_account_id=account.id if account else None,
                subscription_id=sub.id,
                plan_name=f"Upgrade Adjustment: {new_plan['name']}",
                transaction_type="Plan Upgrade",
                payment_method="UPI",
                billed_amount=float(tax_calc.total_amount),
                expected_amount=float(tax_calc.total_amount),
                due_date=now + timedelta(days=account.payment_term_days if account else 15),
                status=InvoiceStatus.ISSUED,
                currency=sub.currency,
                subtotal=adj.net_adjustment_amount,
                tax_amount=tax_calc.total_tax,
                discount_amount=Decimal('0.00'),
                total_amount=tax_calc.total_amount,
                paid_amount=Decimal('0.00'),
                outstanding_amount=tax_calc.total_amount,
                tax_treatment=tax_calc.tax_treatment,
                cgst_amount=tax_calc.cgst_amount,
                sgst_amount=tax_calc.sgst_amount,
                igst_amount=tax_calc.igst_amount,
                place_of_supply=tax_calc.place_of_supply,
                supplier_gstin=tax_calc.supplier_gstin,
                customer_gstin=tax_calc.customer_gstin,
                billing_period_start=now,
                billing_period_end=sub.current_period_end,
                idempotency_key=f"upgrade_{sub.id}_{int(now.timestamp())}",
                notes=f"Prorated upgrade to {new_plan['name']} for {adj.remaining_days} days",
                created_at=now
            )
            db.add(adj_inv)
            db.flush()
            adj_invoice_id = adj_inv.id

            # Post balanced General Ledger entries
            ledger_lines = [
                {
                    "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
                    "debit": tax_calc.total_amount,
                    "credit": Decimal('0.00'),
                    "currency": sub.currency
                },
                {
                    "account_code": ChartOfAccounts.BROADBAND_REVENUE,
                    "debit": Decimal('0.00'),
                    "credit": adj.net_adjustment_amount,
                    "currency": sub.currency
                }
            ]
            if tax_calc.total_tax > Decimal('0.00'):
                ledger_lines.append({
                    "account_code": ChartOfAccounts.TAX_PAYABLE_GST,
                    "debit": Decimal('0.00'),
                    "credit": tax_calc.total_tax,
                    "currency": sub.currency
                })

            post_journal_entry(
                db=db,
                market_id=sub.market_id,
                reference_type="PLAN_UPGRADE_INVOICE",
                reference_id=inv_code,
                description=f"Prorated plan upgrade invoice {inv_code} for subscription {sub.subscription_code}",
                lines=ledger_lines,
                allow_idempotent_replay=True
            )

        elif adj.net_adjustment_amount < Decimal('0.00'):
            # Downgrade credit to customer account
            credit_due = abs(adj.net_adjustment_amount)
            if account:
                account.balance = quantize_money(account.balance) + credit_due
            post_journal_entry(
                db=db,
                market_id=sub.market_id,
                reference_type="PLAN_DOWNGRADE_CREDIT",
                reference_id=f"CR-{str(sub.id)[:8]}",
                description=f"Downgrade credit for subscription {sub.subscription_code}",
                lines=[
                    {
                        "account_code": ChartOfAccounts.VOUCHER_DISCOUNTS,
                        "debit": credit_due,
                        "credit": Decimal('0.00'),
                        "currency": sub.currency
                    },
                    {
                        "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
                        "debit": Decimal('0.00'),
                        "credit": credit_due,
                        "currency": sub.currency
                    }
                ],
                allow_idempotent_replay=True
            )

        rec = ScheduledPlanChange(
            id=uuid.uuid4(),
            subscription_id=sub.id,
            billing_account_id=account.id if account else uuid.uuid4(),
            market_id=sub.market_id,
            current_plan_id=old_plan_price and str(old_plan_price),
            target_plan_id=target_plan_id,
            change_type=change_type,
            effective_mode=PlanChangeMode.IMMEDIATE,
            scheduled_effective_date=now,
            executed_at=now,
            status=PlanChangeStatus.EXECUTED,
            adjustment_amount=adj.net_adjustment_amount,
            adjustment_invoice_id=adj_invoice_id,
            created_at=now,
            updated_at=now
        )
        db.add(rec)
        db.commit()

        return {
            "status": "EXECUTED",
            "change_type": change_type,
            "effective_mode": "IMMEDIATE",
            "new_plan_name": new_plan["name"],
            "new_plan_price": float(new_plan_price),
            "net_adjustment_amount": float(adj.net_adjustment_amount),
            "adjustment_invoice_id": adj_invoice_id,
            "remaining_days": adj.remaining_days
        }

    else:
        # NEXT_CYCLE effective dating
        rec = ScheduledPlanChange(
            id=uuid.uuid4(),
            subscription_id=sub.id,
            billing_account_id=account.id if account else uuid.uuid4(),
            market_id=sub.market_id,
            current_plan_id=sub.plan_id,
            target_plan_id=target_plan_id,
            change_type=change_type,
            effective_mode=PlanChangeMode.NEXT_CYCLE,
            scheduled_effective_date=sub.current_period_end,
            status=PlanChangeStatus.SCHEDULED,
            adjustment_amount=Decimal('0.00'),
            created_at=now,
            updated_at=now
        )
        db.add(rec)
        db.commit()

        return {
            "status": "SCHEDULED",
            "change_type": change_type,
            "effective_mode": "NEXT_CYCLE",
            "effective_date": sub.current_period_end.isoformat(),
            "target_plan_name": new_plan["name"],
            "target_plan_price": float(new_plan_price)
        }


# ─── 5. Automated SLA Breach Outage Evaluation & Credit Notes ────────────────

def evaluate_sla_and_issue_credit(
    db: Session,
    subscription_id: Any,
    downtime_minutes: int,
    billing_period_start: datetime,
    billing_period_end: datetime,
    sla_tier: str = SLATier.STANDARD_99_5,
    root_cause: Optional[str] = None
) -> Dict[str, Any]:
    """
    Evaluates enterprise SLA availability and issues automated credit notes:
    - Tier 1 (99.5%): Max downtime 216 mins/month.
    - Tier 2 (99.9%): Max downtime 43 mins/month.
    - Tier 3 (99.95%): Max downtime 21 mins/month.
    If breached, calculates tiered penalty credit and posts balanced ledger reversal.
    """
    sub = db.query(BillingSubscription).filter(
        (BillingSubscription.id == subscription_id) | (BillingSubscription.subscription_code == str(subscription_id))
    ).first()
    if not sub:
        raise ValueError(f"BillingSubscription '{subscription_id}' not found.")

    account = sub.billing_account
    if not account:
        raise ValueError("BillingAccount associated with subscription not found.")

    total_period_minutes = max(1, int((billing_period_end - billing_period_start).total_seconds() / 60))
    downtime_clamped = min(downtime_minutes, total_period_minutes)
    uptime_minutes = max(0, total_period_minutes - downtime_clamped)

    actual_uptime_pct = quantize_money(Decimal(str(uptime_minutes)) / Decimal(str(total_period_minutes)) * Decimal('100.00'))

    # Determine committed SLA uptime threshold
    threshold_map = {
        SLATier.STANDARD_99_5: Decimal('99.50'),
        SLATier.PREMIUM_99_9: Decimal('99.90'),
        SLATier.MISSION_CRITICAL_99_95: Decimal('99.95')
    }
    committed_pct = threshold_map.get(sla_tier, Decimal('99.50'))

    sla_breached = (actual_uptime_pct < committed_pct)
    penalty_credit_pct = Decimal('0.00')
    credit_amount = Decimal('0.00')
    credit_note = None

    if sla_breached:
        shortfall = committed_pct - actual_uptime_pct
        if shortfall <= Decimal('1.00'):
            penalty_credit_pct = Decimal('10.00')   # 10% credit of monthly billing
        elif shortfall <= Decimal('2.00'):
            penalty_credit_pct = Decimal('25.00')   # 25% credit
        else:
            penalty_credit_pct = Decimal('50.00')   # 50% credit

        monthly_charge = quantize_money(sub.unit_price)
        credit_amount = quantize_money(monthly_charge * penalty_credit_pct / Decimal('100.00'))

        if credit_amount > Decimal('0.00'):
            # Issue authoritative CreditNote
            credit_note = issue_credit_note(
                db=db,
                billing_account_id=str(account.id),
                amount=float(credit_amount),
                reason=f"SLA Breach Credit ({downtime_minutes}m downtime, {actual_uptime_pct}% uptime vs {committed_pct}% SLA)"
            )

    incident_code = generate_business_code(
        db=db,
        sequence_type=SequenceType.SLA_INCIDENT,
        market_id=sub.market_id
    )

    incident = SLAIncidentRecord(
        id=uuid.uuid4(),
        incident_code=incident_code,
        subscription_id=sub.id,
        billing_account_id=account.id,
        customer_id=sub.customer_id,
        market_id=sub.market_id,
        sla_tier=sla_tier,
        committed_uptime_pct=committed_pct,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end,
        total_period_minutes=total_period_minutes,
        total_downtime_minutes=downtime_clamped,
        actual_uptime_pct=actual_uptime_pct,
        sla_breached=sla_breached,
        penalty_credit_pct=penalty_credit_pct,
        credit_note_id=credit_note.id if credit_note else None,
        credit_note_amount=credit_amount,
        status=SLAIncidentStatus.CREDIT_ISSUED if credit_note else SLAIncidentStatus.EVALUATED,
        root_cause=root_cause or "Network outage telemetry threshold breached",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    return {
        "incident_code": incident_code,
        "sla_tier": sla_tier,
        "committed_uptime_pct": float(committed_pct),
        "actual_uptime_pct": float(actual_uptime_pct),
        "downtime_minutes": downtime_clamped,
        "sla_breached": sla_breached,
        "penalty_credit_pct": float(penalty_credit_pct),
        "credit_note_code": credit_note.credit_note_code if credit_note else None,
        "credit_note_amount": float(credit_amount),
        "status": incident.status
    }
