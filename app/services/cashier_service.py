"""
app/services/cashier_service.py

SentinelOS Cashier Desk, Collection Shift & Daily Collection Register (DCR) Service.
Handles counter payments, official receipt generation, cash drawer opening/closing,
physical denomination verification, and shift closing variance auditing.
"""

import uuid
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app.models import Customer, Invoice, User
from app.models_finance import (
    BillingAccount, Payment, PaymentAllocation, PaymentStatus,
    PaymentMethod, ChartOfAccounts, InvoiceStatus
)
from app.models_cashier import CashierShift, CashierShiftTransaction, ShiftStatus
from app.services.sequence_service import generate_business_code, SequenceType
from app.services.ledger_service import post_journal_entry


DENOMINATION_VALUES = {
    "500": Decimal("500.00"),
    "200": Decimal("200.00"),
    "100": Decimal("100.00"),
    "50": Decimal("50.00"),
    "20": Decimal("20.00"),
    "10": Decimal("10.00"),
    "5": Decimal("5.00"),
    "2": Decimal("2.00"),
    "1": Decimal("1.00"),
}


def open_cashier_shift(
    db: Session,
    cashier_user_id: int,
    market_id: str = "mumbai",
    opening_float: Decimal = Decimal("0.00"),
    branch_name: str = "Main Branch"
) -> CashierShift:
    """
    Opens a new cashier work shift.
    Enforces invariant: A cashier can have at most one active OPEN shift at a time.
    """
    existing_open = (
        db.query(CashierShift)
        .filter(
            CashierShift.cashier_user_id == cashier_user_id,
            CashierShift.status == ShiftStatus.OPEN
        )
        .first()
    )
    if existing_open:
        raise ValueError(f"Cashier already has an active open shift: {existing_open.shift_code}")

    cur_year = datetime.utcnow().year
    shift_code = generate_business_code(
        db=db,
        sequence_type="SHIFT",
        market_id=market_id,
        year=cur_year,
        custom_prefix="SHF"
    )

    float_val = opening_float.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shift = CashierShift(
        id=uuid.uuid4(),
        shift_code=shift_code,
        cashier_user_id=cashier_user_id,
        market_id=market_id,
        branch_name=branch_name,
        opening_time=datetime.utcnow(),
        status=ShiftStatus.OPEN,
        opening_float=float_val,
        expected_cash_in_drawer=float_val,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(shift)
    db.commit()
    db.refresh(shift)
    return shift


def record_counter_collection(
    db: Session,
    shift_id: str,
    customer_id: int,
    amount: Decimal,
    invoice_id: Optional[int] = None,
    payer_name: Optional[str] = None,
    payment_method: str = "CASH",
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Records a counter cash or spot UPI payment received by a cashier.
    Generates an official Cash Receipt (RCP-MUM-2026-00001), updates billing account,
    allocates to invoice if specified, and posts to the General Ledger.
    """
    shift_uuid = uuid.UUID(str(shift_id))
    shift = db.query(CashierShift).filter(CashierShift.id == shift_uuid).with_for_update().first()
    if not shift or shift.status != ShiftStatus.OPEN:
        raise ValueError(f"Active open cashier shift '{shift_id}' not found.")

    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise ValueError(f"Customer #{customer_id} not found.")

    account = db.query(BillingAccount).filter(BillingAccount.customer_id == cust.id).first()
    if not account:
        raise ValueError(f"Billing account for customer #{customer_id} not found.")

    pay_amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cur_year = datetime.utcnow().year
    mkt = cust.market_id or shift.market_id

    pay_ref = generate_business_code(db=db, sequence_type=SequenceType.PAYMENT, market_id=mkt, year=cur_year)
    rcp_code = generate_business_code(db=db, sequence_type=SequenceType.RECEIPT, market_id=mkt, year=cur_year)

    # Create Payment Record
    payment = Payment(
        id=uuid.uuid4(),
        payment_reference=pay_ref,
        billing_account_id=account.id,
        customer_id=cust.id,
        market_id=mkt,
        amount=pay_amount,
        currency=account.currency,
        payment_method=PaymentMethod.CASH if payment_method.upper() == "CASH" else PaymentMethod.UPI,
        provider_name="COUNTER_CASHIER_DESK",
        provider_transaction_id=rcp_code,
        idempotency_key=f"cash_coll_{rcp_code}",
        status=PaymentStatus.COMPLETED,
        allocated_amount=Decimal("0.00"),
        unallocated_amount=pay_amount,
        is_simulated=False,
        payment_date=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(payment)
    db.flush()

    # Allocate to specific invoice or oldest overdue invoice
    target_invoice = None
    if invoice_id:
        target_invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    else:
        target_invoice = (
            db.query(Invoice)
            .filter(Invoice.billing_account_id == account.id, Invoice.outstanding_amount > Decimal("0.00"))
            .order_by(Invoice.due_date.asc())
            .first()
        )

    if target_invoice and target_invoice.outstanding_amount > Decimal("0.00"):
        alloc_amt = min(pay_amount, target_invoice.outstanding_amount)
        allocation = PaymentAllocation(
            id=uuid.uuid4(),
            payment_id=payment.id,
            invoice_id=target_invoice.id,
            allocated_amount=alloc_amt,
            currency=account.currency,
            allocated_at=datetime.utcnow()
        )
        db.add(allocation)
        
        target_invoice.paid_amount += alloc_amt
        target_invoice.outstanding_amount -= alloc_amt
        if target_invoice.outstanding_amount <= Decimal("0.00"):
            target_invoice.status = InvoiceStatus.PAID
            target_invoice.paid_date = datetime.utcnow()
        else:
            target_invoice.status = InvoiceStatus.PARTIALLY_PAID

        payment.allocated_amount = alloc_amt
        payment.unallocated_amount = pay_amount - alloc_amt
        payment.status = PaymentStatus.ALLOCATED if payment.unallocated_amount == 0 else PaymentStatus.PARTIALLY_ALLOCATED

    # Update Shift Totals
    if payment_method.upper() == "CASH":
        shift.total_cash_collected += pay_amount
        shift.expected_cash_in_drawer = shift.opening_float + shift.total_cash_collected - shift.total_refunds_paid
    else:
        shift.total_upi_collected += pay_amount

    # Log Shift Transaction
    shift_tx = CashierShiftTransaction(
        id=uuid.uuid4(),
        shift_id=shift.id,
        receipt_code=rcp_code,
        payment_id=payment.id,
        customer_id=cust.id,
        amount=pay_amount,
        payment_method=payment_method.upper(),
        transaction_time=datetime.utcnow(),
        payer_name=payer_name or cust.name,
        notes=notes
    )
    db.add(shift_tx)

    # Post Balanced General Ledger Journal Entry
    # Debit:  1100 Cash Clearing (pay_amount)
    # Credit: 1200 Accounts Receivable (allocated_amount)
    # Credit: 2100 Customer Credit Deposits (unallocated_amount)
    ledger_lines = [
        {
            "account_code": ChartOfAccounts.CASH_CLEARING,
            "debit": pay_amount,
            "credit": Decimal("0.00"),
            "currency": account.currency
        }
    ]
    if payment.allocated_amount > Decimal("0.00"):
        ledger_lines.append({
            "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
            "debit": Decimal("0.00"),
            "credit": payment.allocated_amount,
            "currency": account.currency
        })
    if payment.unallocated_amount > Decimal("0.00"):
        ledger_lines.append({
            "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
            "debit": Decimal("0.00"),
            "credit": payment.unallocated_amount,
            "currency": account.currency
        })

    post_journal_entry(
        db=db,
        market_id=mkt,
        reference_type="COUNTER_COLLECTION",
        reference_id=rcp_code,
        description=f"Counter collection receipt {rcp_code} from {payer_name or cust.name}",
        lines=ledger_lines
    )

    db.commit()

    return {
        "status": "SUCCESS",
        "receipt_code": rcp_code,
        "payment_reference": pay_ref,
        "amount": pay_amount,
        "payment_method": payment_method.upper(),
        "shift_code": shift.shift_code,
        "allocated_amount": payment.allocated_amount,
        "unallocated_credit": payment.unallocated_amount,
        "invoice_status": target_invoice.status if target_invoice else None
    }


def close_cashier_shift(
    db: Session,
    shift_id: str,
    denominations: Dict[str, int],
    closing_notes: Optional[str] = None,
    supervisor_user_id: Optional[int] = None
) -> CashierShift:
    """
    Closes a cashier shift, verifies physical cash denominations, and computes cash drawer variance.
    """
    shift_uuid = uuid.UUID(str(shift_id))
    shift = db.query(CashierShift).filter(CashierShift.id == shift_uuid).with_for_update().first()
    if not shift or shift.status != ShiftStatus.OPEN:
        raise ValueError(f"Active open cashier shift '{shift_id}' not found.")

    # Calculate actual cash counted from denominations
    actual_cash = Decimal("0.00")
    for note, count in denominations.items():
        val = DENOMINATION_VALUES.get(str(note))
        if val and int(count) > 0:
            actual_cash += val * Decimal(str(count))

    expected_cash = shift.opening_float + shift.total_cash_collected - shift.total_refunds_paid
    variance = actual_cash - expected_cash

    shift.actual_cash_counted = actual_cash
    shift.expected_cash_in_drawer = expected_cash
    shift.cash_variance = variance
    shift.denominations_breakdown = denominations
    shift.closing_notes = closing_notes
    shift.supervisor_user_id = supervisor_user_id
    shift.closing_time = datetime.utcnow()
    shift.status = ShiftStatus.RECONCILED if variance == Decimal("0.00") else ShiftStatus.DISCREPANCY
    shift.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(shift)
    return shift


def get_daily_collection_register(
    db: Session,
    market_id: str = "mumbai",
    register_date: Optional[date] = None
) -> Dict[str, Any]:
    """
    Aggregates the official Daily Collection Register (DCR) for a specific date and market.
    """
    target_d = register_date or datetime.utcnow().date()
    day_start = datetime(target_d.year, target_d.month, target_d.day, 0, 0, 0)
    day_end = datetime(target_d.year, target_d.month, target_d.day, 23, 59, 59)

    shifts = (
        db.query(CashierShift)
        .filter(
            CashierShift.market_id == market_id,
            CashierShift.opening_time >= day_start,
            CashierShift.opening_time <= day_end
        )
        .all()
    )

    total_cash = sum((s.total_cash_collected for s in shifts), Decimal("0.00"))
    total_upi = sum((s.total_upi_collected for s in shifts), Decimal("0.00"))
    total_collected = total_cash + total_upi
    total_variance = sum((s.cash_variance for s in shifts), Decimal("0.00"))

    shift_summaries = []
    for s in shifts:
        shift_summaries.append({
            "shift_code": s.shift_code,
            "cashier_id": s.cashier_user_id,
            "status": s.status,
            "opening_float": float(s.opening_float),
            "cash_collected": float(s.total_cash_collected),
            "upi_collected": float(s.total_upi_collected),
            "actual_cash": float(s.actual_cash_counted),
            "variance": float(s.cash_variance),
            "transactions_count": len(s.transactions)
        })

    return {
        "market_id": market_id,
        "date": target_d.isoformat(),
        "total_shifts": len(shifts),
        "total_cash_collected": float(total_cash),
        "total_upi_collected": float(total_upi),
        "total_collected": float(total_collected),
        "total_cash_variance": float(total_variance),
        "shifts": shift_summaries
    }
