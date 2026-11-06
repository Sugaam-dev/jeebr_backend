"""
app/services/autopay_service.py

SentinelOS UPI AutoPay & NACH e-Mandate Automation Engine.
Complies with RBI Master Directions on Recurring Transactions & NPCI AutoPay specifications:
1. Mandate lifecycle management (Registration, Activation, Execution, Revocation).
2. Statutory Pre-Debit Notification (issued 24-48 hours before debit with opt-out link).
3. Automated debit execution against open invoices with amount-cap verification.
4. Balanced double-entry General Ledger settlement (Dr Cash Clearing 1100, Cr Accounts Receivable 1200).
"""

import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from app.models_autopay import PaymentMandate, PreDebitNotification, MandateType, MandateStatus, NotificationChannel
from app.models_finance import BillingAccount, Payment, PaymentStatus, PaymentMethod, PaymentAllocation, ChartOfAccounts
from app.models import Invoice, Customer
from app.services.sequence_service import generate_sequential_number, SequenceType
from app.services.billing_service import post_journal_entry


def register_mandate(
    db: Session,
    billing_account_id: uuid.UUID,
    mandate_type: str = MandateType.UPI_AUTOPAY,
    max_amount: Decimal = Decimal('15000.00'),
    vpa: Optional[str] = None,
    bank_name: Optional[str] = None,
    account_number_masked: Optional[str] = None
) -> PaymentMandate:
    """
    Registers a new recurring payment mandate for an ISP subscriber's billing account.
    Generates an NPCI-compliant UMRN (Unique Mandate Reference Number) and enables auto-pay.
    """
    ba = db.query(BillingAccount).filter(BillingAccount.id == billing_account_id).first()
    if not ba:
        raise ValueError(f"BillingAccount {billing_account_id} not found.")

    mkt = ba.market_id or "mumbai"
    mandate_count = db.query(PaymentMandate).count() + 1
    mandate_code = f"MAND-{mkt[:3].upper()}-{datetime.utcnow().year}-{mandate_count:04d}"
    umrn = f"NPCI{mkt[:3].upper()}{datetime.utcnow().strftime('%Y%m%d')}{mandate_count:06d}"

    mandate = PaymentMandate(
        mandate_code=mandate_code,
        billing_account_id=ba.id,
        customer_id=ba.customer_id if ba.customer_id else 1,
        market_id=mkt,
        mandate_type=mandate_type,
        umrn=umrn,
        max_amount=Decimal(str(max_amount)),
        frequency="MONTHLY",
        status=MandateStatus.ACTIVE,
        vpa=vpa or "subscriber@okaxis",
        bank_name=bank_name or "HDFC Bank Ltd",
        account_number_masked=account_number_masked or "XXXXXXXX1234",
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(days=365 * 5)
    )
    db.add(mandate)

    ba.auto_pay_enabled = True
    db.commit()
    db.refresh(mandate)
    return mandate


def issue_pre_debit_notification(
    db: Session,
    mandate_id: uuid.UUID,
    invoice_id: int,
    advance_days: int = 2,
    channel: str = NotificationChannel.SMS
) -> PreDebitNotification:
    """
    Dispatches a statutory Pre-Debit Notification (PDN) conforming to RBI guidelines.
    Must precede the actual debit transaction by at least 24 hours.
    """
    mandate = db.query(PaymentMandate).filter(PaymentMandate.id == mandate_id).first()
    if not mandate:
        raise ValueError(f"Mandate {mandate_id} not found.")
    if mandate.status != MandateStatus.ACTIVE:
        raise ValueError(f"Cannot dispatch pre-debit notice for inactive mandate (status: {mandate.status}).")

    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise ValueError(f"Invoice {invoice_id} not found.")

    outstanding = Decimal(str(invoice.outstanding_amount))
    if outstanding <= Decimal('0.00'):
        raise ValueError(f"Invoice {invoice.invoice_code} has no outstanding balance to debit.")

    # Check mandate limit
    if outstanding > mandate.max_amount:
        raise ValueError(f"Invoice amount INR {outstanding} exceeds mandate cap INR {mandate.max_amount}.")

    now = datetime.utcnow()
    scheduled_dt = max(invoice.due_date, now + timedelta(days=advance_days))
    ref = f"PDN-{invoice.invoice_code}-{int(now.timestamp())}"

    pdn = PreDebitNotification(
        mandate_id=mandate.id,
        invoice_id=invoice.id,
        notification_reference=ref,
        scheduled_debit_date=scheduled_dt,
        debit_amount=outstanding,
        channel=channel,
        sent_at=now,
        status="SENT",
        opt_out_url=f"https://pay.sentinelos.com/mandate/{mandate.umrn}/opt-out?ref={ref}"
    )
    db.add(pdn)
    db.commit()
    db.refresh(pdn)
    return pdn


def execute_mandate_auto_debit(
    db: Session,
    mandate_id: uuid.UUID,
    invoice_id: int
) -> Dict[str, Any]:
    """
    Executes an automated debit under an active mandate.
    Verifies amount cap, allocates payment, updates invoice state,
    and posts double-entry balanced General Ledger transactions.
    """
    mandate = db.query(PaymentMandate).filter(PaymentMandate.id == mandate_id).first()
    if not mandate:
        raise ValueError(f"Mandate {mandate_id} not found.")
    if mandate.status != MandateStatus.ACTIVE:
        raise ValueError(f"Mandate {mandate.mandate_code} is not active ({mandate.status}).")

    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise ValueError(f"Invoice {invoice_id} not found.")

    outstanding = Decimal(str(invoice.outstanding_amount))
    if outstanding <= Decimal('0.00'):
        raise ValueError(f"Invoice {invoice.invoice_code} has zero outstanding balance.")

    if outstanding > mandate.max_amount:
        raise ValueError(f"Debit amount INR {outstanding} exceeds mandate threshold INR {mandate.max_amount}.")

    mkt = mandate.market_id or "mumbai"
    pay_ref = generate_sequential_number(db, SequenceType.PAYMENT, mkt)
    now = datetime.utcnow()

    # Record Payment
    payment = Payment(
        payment_reference=pay_ref,
        billing_account_id=mandate.billing_account_id,
        customer_id=mandate.customer_id,
        market_id=mkt,
        amount=outstanding,
        currency="INR",
        payment_method=PaymentMethod.AUTOPAY,
        provider_name="UPI_AUTOPAY_GATEWAY",
        provider_transaction_id=f"TXN-{mandate.umrn}-{int(now.timestamp())}",
        idempotency_key=f"AUTOPAY-EXEC-{invoice.id}-{int(now.timestamp())}",
        status=PaymentStatus.COMPLETED,
        allocated_amount=outstanding,
        unallocated_amount=Decimal('0.00'),
        is_simulated=True,
        payment_date=now
    )
    db.add(payment)
    db.flush()

    # Create Allocation
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        allocated_amount=outstanding,
        allocated_at=now
    )
    db.add(allocation)

    # Update Invoice Status
    invoice.paid_amount = Decimal(str(invoice.paid_amount)) + outstanding
    invoice.outstanding_amount = Decimal('0.00')
    invoice.status = "Paid"
    invoice.paid_date = now

    # Post balanced General Ledger entries:
    # Debit: Cash Clearing (1100) = outstanding
    # Credit: Accounts Receivable (1200) = outstanding
    journal_lines = [
        {
            "account_code": ChartOfAccounts.CASH_CLEARING,
            "debit": outstanding,
            "credit": Decimal('0.00'),
            "description": f"AutoPay Settlement - Mandate {mandate.mandate_code} on Invoice {invoice.invoice_code}"
        },
        {
            "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
            "debit": Decimal('0.00'),
            "credit": outstanding,
            "description": f"Receivable Cleared - Invoice {invoice.invoice_code}"
        }
    ]

    post_journal_entry(
        db=db,
        market_id=mkt,
        reference_type="PAYMENT",
        reference_id=str(payment.id),
        description=f"AutoPay Execution Receipt {pay_ref} for Invoice {invoice.invoice_code}",
        lines=journal_lines
    )

    db.commit()
    db.refresh(invoice)
    db.refresh(payment)

    return {
        "status": "SUCCESS",
        "payment_reference": pay_ref,
        "mandate_code": mandate.mandate_code,
        "umrn": mandate.umrn,
        "invoice_code": invoice.invoice_code,
        "debited_amount": float(outstanding),
        "invoice_status": invoice.status,
        "remaining_outstanding": float(invoice.outstanding_amount)
    }


def revoke_mandate(
    db: Session,
    mandate_id: uuid.UUID,
    reason: str = "Customer request"
) -> PaymentMandate:
    """Revokes an active mandate and adjusts billing account auto-pay flag if no other active mandates remain."""
    mandate = db.query(PaymentMandate).filter(PaymentMandate.id == mandate_id).first()
    if not mandate:
        raise ValueError(f"Mandate {mandate_id} not found.")

    mandate.status = MandateStatus.REVOKED
    ba = mandate.billing_account

    if ba:
        # Check if other active mandates exist
        active_remaining = db.query(PaymentMandate).filter(
            PaymentMandate.billing_account_id == ba.id,
            PaymentMandate.id != mandate.id,
            PaymentMandate.status == MandateStatus.ACTIVE
        ).count()
        if active_remaining == 0:
            ba.auto_pay_enabled = False

    db.commit()
    db.refresh(mandate)
    return mandate
