"""
app/services/billing_service.py

SentinelOS ISP Billing & Subscription Engine.
Implements:
1. Billing account creation & subscription lifecycle with canonical tariff plan binding.
2. Invoice generation, calculation with fixed-precision Decimal arithmetic, and line items.
3. Configurable tax (GST 18%) and discount calculations.
4. Payment processing, duplicate detection via idempotency keys, and multi-invoice allocation.
5. Balanced General Ledger postings for all invoices, payments, credit notes, and refunds.
6. Prepaid balance control and postpaid billing run orchestration.
7. Overdue dunning evaluation and aging breakdown.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_

from app.models import Customer, Invoice
from app.models_crm import CRMCustomer
from app.models_finance import (
    BillingAccount, BillingSubscription, InvoiceLineItem, Payment,
    PaymentAllocation, CreditNote, RefundRecord, BillingAccountStatus,
    BillingType, SubscriptionStatus, InvoiceStatus, PaymentStatus,
    PaymentMethod, ChartOfAccounts
)
from app.services.provisioning_catalog import get_service_plan_by_id, CANONICAL_SERVICE_PLANS
from app.services.payment_provider import get_payment_provider, PaymentChargeResult
from app.services.ledger_service import post_journal_entry, quantize_money


# ─── Helper Functions ─────────────────────────────────────────────────────────

def _format_money(val: Any) -> Decimal:
    return quantize_money(val)


# ─── 1. Customer Billing Account Lifecycle ────────────────────────────────────

def ensure_billing_account(
    db: Session,
    customer_id: int,
    market_id: str = "mumbai",
    billing_type: str = BillingType.POSTPAID,
    currency: str = "INR",
    credit_limit: float = 0.0,
    payment_term_days: int = 15
) -> BillingAccount:
    """
    Find or lazily create an authoritative BillingAccount for an operational or CRM customer.
    Ensures 1:1 account mapping per customer.
    """
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise ValueError(f"Customer #{customer_id} does not exist.")

    crm_cust = db.query(CRMCustomer).filter(
        or_(
            CRMCustomer.legacy_customer_id == customer.id,
            CRMCustomer.customer_number == customer.customer_code
        )
    ).first()

    account = db.query(BillingAccount).filter(
        BillingAccount.customer_id == customer.id
    ).first()

    if account:
        return account

    # Generate sequential unique account number e.g. BA-MUM-2026-00001
    count = db.query(BillingAccount).count()
    mkt = (market_id or customer.market_id or "mumbai")[:3].upper()
    acct_num = f"BA-{mkt}-{datetime.utcnow().year}-{count + 1:05d}"

    # Inherit customer type if available
    cust_billing_type = BillingType.PREPAID if customer.customer_type == "Prepaid" else billing_type

    account = BillingAccount(
        id=uuid.uuid4(),
        account_number=acct_num,
        customer_id=customer.id,
        crm_customer_id=crm_cust.id if crm_cust else None,
        market_id=customer.market_id or market_id,
        billing_type=cust_billing_type,
        currency=(crm_cust.billing_currency if crm_cust and crm_cust.billing_currency else currency),
        balance=Decimal('0.00'),
        credit_limit=_format_money(credit_limit),
        status=BillingAccountStatus.ACTIVE,
        payment_term_days=payment_term_days,
        auto_pay_enabled=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


# ─── 2. Subscription Lifecycle ────────────────────────────────────────────────

def create_subscription(
    db: Session,
    customer_id: int,
    plan_id: str,
    billing_account_id: Optional[str] = None,
    billing_frequency: str = "MONTHLY",
    start_date: Optional[datetime] = None,
    provisioning_order_id: Optional[int] = None
) -> BillingSubscription:
    """
    Subscribe a customer to a canonical telecom plan from the tariff catalog.
    Prevents duplicate active subscriptions for the same plan.
    """
    # 1. Validate plan against canonical catalogue
    plan = get_service_plan_by_id(plan_id)
    if not plan:
        raise ValueError(f"Plan '{plan_id}' not found in canonical service plan catalog.")

    # 2. Resolve billing account
    if billing_account_id:
        account = db.query(BillingAccount).filter(BillingAccount.id == billing_account_id).first()
        if not account:
            raise ValueError(f"Billing account '{billing_account_id}' not found.")
    else:
        account = ensure_billing_account(db, customer_id)

    # 3. Check for existing active subscription to the same plan
    existing_sub = db.query(BillingSubscription).filter(
        BillingSubscription.billing_account_id == account.id,
        BillingSubscription.plan_id == plan_id,
        BillingSubscription.status == SubscriptionStatus.ACTIVE
    ).first()

    if existing_sub:
        return existing_sub

    # 4. Compute effective period dates
    now = start_date or datetime.utcnow()
    validity_days = plan.get("validity_days", 30)
    period_end = now + timedelta(days=validity_days)

    count = db.query(BillingSubscription).count()
    sub_code = f"SUB-{account.market_id[:3].upper()}-{now.year}-{count + 1:05d}"

    subscription = BillingSubscription(
        id=uuid.uuid4(),
        subscription_code=sub_code,
        billing_account_id=account.id,
        customer_id=account.customer_id,
        crm_customer_id=account.crm_customer_id,
        market_id=account.market_id,
        plan_id=plan["plan_id"],
        plan_name=plan["name"],
        billing_type=plan.get("billing_type", account.billing_type),
        billing_frequency=billing_frequency,
        unit_price=_format_money(plan["price_inr"]),
        currency=account.currency,
        status=SubscriptionStatus.ACTIVE,
        start_date=now,
        current_period_start=now,
        current_period_end=period_end,
        next_billing_date=period_end,
        provisioning_order_id=provisioning_order_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def transition_subscription_status(
    db: Session,
    subscription_id: str,
    target_status: str,
    reason: Optional[str] = None
) -> BillingSubscription:
    """Transition subscription lifecycle status (ACTIVE, SUSPENDED, CANCELLED)."""
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == subscription_id).first()
    if not sub:
        raise ValueError(f"Subscription '{subscription_id}' not found.")

    action_map = {
        "REACTIVATE": SubscriptionStatus.ACTIVE,
        "SUSPEND": SubscriptionStatus.SUSPENDED,
        "CANCEL": SubscriptionStatus.CANCELLED,
        "ACTIVATE": SubscriptionStatus.ACTIVE
    }
    target_status = action_map.get(target_status.upper(), target_status)

    if target_status not in SubscriptionStatus.ALL:
        raise ValueError(f"Invalid target status '{target_status}'.")

    sub.status = target_status
    sub.updated_at = datetime.utcnow()
    if target_status == SubscriptionStatus.CANCELLED:
        sub.cancelled_at = datetime.utcnow()
        sub.cancellation_reason = reason or "Cancelled by user"

    db.commit()
    db.refresh(sub)
    return sub


# ─── 3. Invoice Generation with Fixed-Precision Arithmetic ────────────────────

def generate_invoice(
    db: Session,
    subscription_id: str,
    billing_period_start: Optional[datetime] = None,
    billing_period_end: Optional[datetime] = None,
    discount_amount: float = 0.0,
    tax_rate: float = 18.0,  # Standard Indian GST rate 18.00%
    idempotency_key: Optional[str] = None,
    notes: Optional[str] = None
) -> Invoice:
    """
    Generate an authoritative, itemized invoice with fixed-precision decimal arithmetic.
    Prevents duplicate billing for the same subscription and billing period.
    Posts a balanced double-entry General Ledger entry.
    """
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == subscription_id).first()
    if not sub:
        raise ValueError(f"Subscription '{subscription_id}' not found.")

    account = sub.billing_account
    customer = sub.customer

    period_start = billing_period_start or sub.current_period_start
    period_end = billing_period_end or sub.current_period_end

    # Idempotency check: prevent duplicate invoice for same subscription & period or idempotency key
    if idempotency_key:
        existing_by_key = db.query(Invoice).filter(Invoice.idempotency_key == idempotency_key).first()
        if existing_by_key:
            return existing_by_key

    existing_overlap = db.query(Invoice).filter(
        Invoice.subscription_id == sub.id,
        Invoice.billing_period_start == period_start,
        Invoice.billing_period_end == period_end,
        Invoice.status != InvoiceStatus.VOIDED
    ).first()

    if existing_overlap:
        return existing_overlap

    # Fixed-precision Decimal calculations
    unit_price = _format_money(sub.unit_price)
    qty = Decimal('1.00')
    subtotal = (unit_price * qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    discount = min(_format_money(discount_amount), subtotal)
    taxable_amount = subtotal - discount

    tax_rate_dec = Decimal(str(tax_rate))
    tax_amount = (taxable_amount * tax_rate_dec / Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_amount = taxable_amount + tax_amount

    # Generate sequential unique invoice code e.g. INV-MUM-2026-00001
    count = db.query(Invoice).count()
    mkt = sub.market_id[:3].upper()
    inv_code = f"INV-{mkt}-{datetime.utcnow().year}-{count + 1:05d}"
    due_date = datetime.utcnow() + timedelta(days=account.payment_term_days)

    invoice = Invoice(
        market_id=sub.market_id,
        invoice_code=inv_code,
        customer_id=sub.customer_id,
        billing_account_id=account.id,
        subscription_id=sub.id,
        plan_name=sub.plan_name,
        transaction_type="Postpaid Bill" if sub.billing_type == BillingType.POSTPAID else "Recharge Pack",
        payment_method="UPI",
        billed_amount=float(total_amount),        # Backward-compatible float
        expected_amount=float(total_amount),      # Backward-compatible float
        due_date=due_date,
        status=InvoiceStatus.ISSUED,
        currency=sub.currency,
        subtotal=subtotal,
        tax_amount=tax_amount,
        discount_amount=discount,
        total_amount=total_amount,
        paid_amount=Decimal('0.00'),
        outstanding_amount=total_amount,
        billing_period_start=period_start,
        billing_period_end=period_end,
        idempotency_key=idempotency_key or f"inv_gen_{sub.id}_{int(period_start.timestamp())}",
        notes=notes or f"Service subscription charges for {sub.plan_name}",
        created_at=datetime.utcnow()
    )
    db.add(invoice)
    db.flush()

    # Create detailed InvoiceLineItem
    line_item = InvoiceLineItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        subscription_id=sub.id,
        description=f"Broadband Plan: {sub.plan_name} ({period_start.strftime('%d %b')} - {period_end.strftime('%d %b %Y')})",
        quantity=qty,
        unit_price=unit_price,
        amount=subtotal,
        tax_rate=tax_rate_dec,
        tax_amount=tax_amount,
        discount_amount=discount,
        total_amount=total_amount,
        currency=sub.currency,
        created_at=datetime.utcnow()
    )
    db.add(line_item)
    db.flush()

    # Post Balanced General Ledger Journal Entry
    # Debit:  1200 Accounts Receivable (total_amount)
    # Credit: 4100 Broadband Service Revenue (taxable_amount)
    # Credit: 2200 GST Output Tax Payable (tax_amount)
    ledger_lines = [
        {
            "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
            "debit": total_amount,
            "credit": Decimal('0.00'),
            "currency": sub.currency
        },
        {
            "account_code": ChartOfAccounts.BROADBAND_REVENUE,
            "debit": Decimal('0.00'),
            "credit": taxable_amount,
            "currency": sub.currency
        }
    ]
    if tax_amount > Decimal('0.00'):
        ledger_lines.append({
            "account_code": ChartOfAccounts.TAX_PAYABLE_GST,
            "debit": Decimal('0.00'),
            "credit": tax_amount,
            "currency": sub.currency
        })

    post_journal_entry(
        db=db,
        market_id=sub.market_id,
        reference_type="INVOICE",
        reference_id=invoice.invoice_code,
        description=f"Invoice issuance {invoice.invoice_code} for customer #{sub.customer_id}",
        lines=ledger_lines,
        allow_idempotent_replay=True
    )

    db.commit()
    db.refresh(invoice)
    return invoice


# ─── 4. Payments, Allocations & Ledger ────────────────────────────────────────

def record_payment(
    db: Session,
    amount: float,
    idempotency_key: str,
    billing_account_id: Optional[str] = None,
    customer_id: Optional[int] = None,
    currency: str = "INR",
    payment_method: str = PaymentMethod.UPI,
    invoice_id: Optional[int] = None,
    provider_name: str = "SIMULATED_GATEWAY",
    auto_allocate: bool = True
) -> Payment:
    """
    Process an authorized payment, allocate against customer invoices, and post balanced ledger entries.
    Supports partial payments and unallocated credit balance.
    Enforces idempotency keys against duplicate submissions.
    """
    # 1. Duplicate check via idempotency key
    existing_payment = db.query(Payment).filter(Payment.idempotency_key == idempotency_key).first()
    if existing_payment:
        return existing_payment

    # 2. Resolve billing account
    if billing_account_id:
        account = db.query(BillingAccount).filter(BillingAccount.id == billing_account_id).first()
    elif customer_id:
        account = ensure_billing_account(db, customer_id)
    else:
        raise ValueError("Either billing_account_id or customer_id must be provided.")

    if not account:
        raise ValueError("Billing account not found.")

    dec_amount = _format_money(amount)
    if dec_amount <= Decimal('0.00'):
        raise ValueError("Payment amount must be strictly positive.")

    # 3. Call Payment Provider
    provider = get_payment_provider(force_provider=provider_name)
    charge_result: PaymentChargeResult = provider.charge(
        amount=dec_amount,
        currency=currency,
        payment_method=payment_method,
        idempotency_key=idempotency_key
    )

    # 4. Generate payment reference e.g. PAY-MUM-2026-00001
    count = db.query(Payment).count()
    pay_ref = f"PAY-{account.market_id[:3].upper()}-{datetime.utcnow().year}-{count + 1:05d}"

    payment = Payment(
        id=uuid.uuid4(),
        payment_reference=pay_ref,
        billing_account_id=account.id,
        customer_id=account.customer_id,
        market_id=account.market_id,
        amount=dec_amount,
        currency=currency,
        payment_method=payment_method,
        provider_name=charge_result.provider_name,
        provider_transaction_id=charge_result.provider_transaction_id,
        idempotency_key=idempotency_key,
        status=PaymentStatus.COMPLETED if charge_result.success else PaymentStatus.FAILED,
        allocated_amount=Decimal('0.00'),
        unallocated_amount=dec_amount if charge_result.success else Decimal('0.00'),
        failure_reason=charge_result.failure_reason,
        is_simulated=charge_result.is_simulated,
        payment_date=datetime.utcnow(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(payment)
    db.flush()

    if not charge_result.success:
        db.commit()
        db.refresh(payment)
        return payment

    # 5. Allocate Payment to Invoices
    remaining_unallocated = dec_amount
    total_allocated = Decimal('0.00')

    # Target specific invoice if provided
    invoices_to_allocate = []
    if invoice_id:
        specific_inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if specific_inv and specific_inv.outstanding_amount > Decimal('0.00'):
            invoices_to_allocate.append(specific_inv)
    elif auto_allocate:
        # Allocate to open/partially paid invoices ordered by oldest due date
        open_invoices = db.query(Invoice).filter(
            Invoice.customer_id == account.customer_id,
            Invoice.outstanding_amount > Decimal('0.00'),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.LATE, InvoiceStatus.OVERDUE])
        ).order_by(Invoice.due_date.asc()).all()
        invoices_to_allocate.extend(open_invoices)

    for inv in invoices_to_allocate:
        if remaining_unallocated <= Decimal('0.00'):
            break

        alloc_amount = min(remaining_unallocated, _format_money(inv.outstanding_amount))
        if alloc_amount <= Decimal('0.00'):
            continue

        allocation = PaymentAllocation(
            id=uuid.uuid4(),
            payment_id=payment.id,
            invoice_id=inv.id,
            allocated_amount=alloc_amount,
            currency=currency,
            allocated_at=datetime.utcnow()
        )
        db.add(allocation)

        inv.paid_amount = _format_money(inv.paid_amount) + alloc_amount
        inv.outstanding_amount = _format_money(inv.outstanding_amount) - alloc_amount
        if inv.outstanding_amount <= Decimal('0.00'):
            inv.status = InvoiceStatus.PAID
            inv.paid_date = datetime.utcnow()
        else:
            inv.status = InvoiceStatus.PARTIALLY_PAID

        total_allocated += alloc_amount
        remaining_unallocated -= alloc_amount

    payment.allocated_amount = total_allocated
    payment.unallocated_amount = remaining_unallocated
    if total_allocated == dec_amount:
        payment.status = PaymentStatus.ALLOCATED
    elif total_allocated > Decimal('0.00'):
        payment.status = PaymentStatus.PARTIALLY_ALLOCATED
    else:
        payment.status = PaymentStatus.COMPLETED

    # If prepaid or excess payment, add unallocated credit to account balance
    if remaining_unallocated > Decimal('0.00'):
        account.balance = _format_money(account.balance) + remaining_unallocated

    # 6. Post Balanced General Ledger Journal Entry
    # Debit:  1100 Cash & Gateway Clearing (dec_amount)
    # Credit: 1200 Accounts Receivable (total_allocated)
    # Credit: 2100 Customer Credit Deposits (remaining_unallocated)
    ledger_lines = [
        {
            "account_code": ChartOfAccounts.CASH_CLEARING,
            "debit": dec_amount,
            "credit": Decimal('0.00'),
            "currency": currency
        }
    ]
    if total_allocated > Decimal('0.00'):
        ledger_lines.append({
            "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
            "debit": Decimal('0.00'),
            "credit": total_allocated,
            "currency": currency
        })
    if remaining_unallocated > Decimal('0.00'):
        ledger_lines.append({
            "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
            "debit": Decimal('0.00'),
            "credit": remaining_unallocated,
            "currency": currency
        })

    post_journal_entry(
        db=db,
        market_id=account.market_id,
        reference_type="PAYMENT",
        reference_id=payment.payment_reference,
        description=f"Payment received {payment.payment_reference} ({payment_method})",
        lines=ledger_lines,
        allow_idempotent_replay=True
    )

    db.commit()
    db.refresh(payment)
    return payment


# ─── 5. Credit Notes & Refunds ────────────────────────────────────────────────

def issue_credit_note(
    db: Session,
    billing_account_id: str,
    amount: float,
    reason: str,
    invoice_id: Optional[int] = None,
    created_by_id: Optional[int] = None
) -> CreditNote:
    """Issue a traceable credit note and post balancing ledger adjustment."""
    account = db.query(BillingAccount).filter(BillingAccount.id == billing_account_id).first()
    if not account:
        raise ValueError(f"Billing account '{billing_account_id}' not found.")

    dec_amount = _format_money(amount)
    if dec_amount <= Decimal('0.00'):
        raise ValueError("Credit note amount must be positive.")

    count = db.query(CreditNote).count()
    cn_code = f"CN-{account.market_id[:3].upper()}-{datetime.utcnow().year}-{count + 1:05d}"

    credit_note = CreditNote(
        id=uuid.uuid4(),
        credit_note_code=cn_code,
        billing_account_id=account.id,
        invoice_id=invoice_id,
        market_id=account.market_id,
        amount=dec_amount,
        currency=account.currency,
        reason=reason,
        status="ISSUED",
        created_by_id=created_by_id,
        created_at=datetime.utcnow()
    )
    db.add(credit_note)

    # If linked to invoice, reduce invoice outstanding amount
    if invoice_id:
        inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if inv:
            inv.outstanding_amount = max(Decimal('0.00'), _format_money(inv.outstanding_amount) - dec_amount)
            if inv.outstanding_amount == Decimal('0.00'):
                inv.status = InvoiceStatus.CREDITED
    else:
        # Credit customer balance
        account.balance = _format_money(account.balance) + dec_amount

    # Post Balanced Ledger Entry:
    # Debit:  4200 Voucher & Retention Discounts (Contra-Revenue)
    # Credit: 1200 Accounts Receivable (or 2100 Customer Credit Deposits)
    credit_account = ChartOfAccounts.ACCOUNTS_RECEIVABLE if invoice_id else ChartOfAccounts.CUSTOMER_DEPOSITS
    post_journal_entry(
        db=db,
        market_id=account.market_id,
        reference_type="CREDIT_NOTE",
        reference_id=cn_code,
        description=f"Credit note {cn_code}: {reason}",
        lines=[
            {
                "account_code": ChartOfAccounts.VOUCHER_DISCOUNTS,
                "debit": dec_amount,
                "credit": Decimal('0.00'),
                "currency": account.currency
            },
            {
                "account_code": credit_account,
                "debit": Decimal('0.00'),
                "credit": dec_amount,
                "currency": account.currency
            }
        ],
        created_by_id=created_by_id,
        allow_idempotent_replay=True
    )

    db.commit()
    db.refresh(credit_note)
    return credit_note


def process_refund(
    db: Session,
    payment_id: str,
    amount: float,
    reason: str,
    created_by_id: Optional[int] = None
) -> RefundRecord:
    """Process an auditable refund against an authorized Payment."""
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise ValueError(f"Payment '{payment_id}' not found.")

    dec_amount = _format_money(amount)
    if dec_amount <= Decimal('0.00') or dec_amount > _format_money(payment.amount):
        raise ValueError(f"Refund amount {dec_amount} is invalid for payment of {payment.amount}.")

    # Call provider refund
    provider = get_payment_provider(force_provider=payment.provider_name)
    refund_res = provider.refund(
        provider_transaction_id=payment.provider_transaction_id or payment.idempotency_key,
        amount=dec_amount,
        currency=payment.currency,
        reason=reason
    )

    count = db.query(RefundRecord).count()
    rf_code = f"REF-{payment.market_id[:3].upper()}-{datetime.utcnow().year}-{count + 1:05d}"

    refund = RefundRecord(
        id=uuid.uuid4(),
        refund_code=rf_code,
        payment_id=payment.id,
        billing_account_id=payment.billing_account_id,
        market_id=payment.market_id,
        amount=dec_amount,
        currency=payment.currency,
        reason=reason,
        status="COMPLETED" if refund_res.success else "FAILED",
        provider_refund_id=refund_res.provider_refund_id,
        created_by_id=created_by_id,
        created_at=datetime.utcnow()
    )
    db.add(refund)

    if refund_res.success:
        payment.status = PaymentStatus.REFUNDED
        # Reversing Ledger Entry:
        # Debit:  2100 Customer Credit Deposits
        # Credit: 1100 Cash / Clearing Account
        post_journal_entry(
            db=db,
            market_id=payment.market_id,
            reference_type="REFUND",
            reference_id=rf_code,
            description=f"Refund {rf_code} for payment {payment.payment_reference}",
            lines=[
                {
                    "account_code": ChartOfAccounts.CUSTOMER_DEPOSITS,
                    "debit": dec_amount,
                    "credit": Decimal('0.00'),
                    "currency": payment.currency
                },
                {
                    "account_code": ChartOfAccounts.CASH_CLEARING,
                    "debit": Decimal('0.00'),
                    "credit": dec_amount,
                    "currency": payment.currency
                }
            ],
            created_by_id=created_by_id,
            allow_idempotent_replay=True
        )

    db.commit()
    db.refresh(refund)
    return refund


# ─── 6. Background Jobs: Recurring Billing & Overdue Dunning ──────────────────

def run_billing_cycle(db: Session, market_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Idempotent recurring billing run:
    Finds active postpaid subscriptions whose current period has ended or is due,
    generates invoices, advances billing periods, and records ledger postings.
    """
    now = datetime.utcnow()
    query = db.query(BillingSubscription).filter(
        BillingSubscription.status == SubscriptionStatus.ACTIVE,
        BillingSubscription.billing_type == BillingType.POSTPAID,
        BillingSubscription.current_period_end <= now + timedelta(days=1)
    )
    if market_id:
        query = query.filter(BillingSubscription.market_id == market_id)

    subs = query.all()
    invoices_generated = 0
    total_billed = Decimal('0.00')

    for sub in subs:
        try:
            inv = generate_invoice(
                db=db,
                subscription_id=str(sub.id),
                billing_period_start=sub.current_period_start,
                billing_period_end=sub.current_period_end
            )
            invoices_generated += 1
            total_billed += _format_money(inv.total_amount)

            # Advance subscription billing cycle
            plan = get_service_plan_by_id(sub.plan_id)
            validity = plan.get("validity_days", 30) if plan else 30
            sub.current_period_start = sub.current_period_end
            sub.current_period_end = sub.current_period_start + timedelta(days=validity)
            sub.next_billing_date = sub.current_period_end
            sub.updated_at = datetime.utcnow()
        except Exception:
            continue

    db.commit()
    return {
        "status": "COMPLETED",
        "market_id": market_id or "all",
        "subscriptions_evaluated": len(subs),
        "invoices_generated": invoices_generated,
        "total_amount_billed": float(total_billed),
        "currency": "INR",
        "timestamp": now
    }


def evaluate_overdue_and_dunning(db: Session, market_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Identify overdue invoices, update lifecycle status, and calculate aging brackets.
    """
    now = datetime.utcnow()
    query = db.query(Invoice).filter(
        Invoice.outstanding_amount > Decimal('0.00'),
        Invoice.due_date < now,
        Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID])
    )
    if market_id:
        query = query.filter(Invoice.market_id == market_id)

    overdue_invoices = query.all()
    count_marked = 0
    aging = {"1_to_15_days": 0, "16_to_30_days": 0, "over_30_days": 0}

    for inv in overdue_invoices:
        days_past = (now - inv.due_date).days
        if days_past > 30:
            aging["over_30_days"] += 1
            inv.status = InvoiceStatus.OVERDUE
        elif days_past > 15:
            aging["16_to_30_days"] += 1
            inv.status = InvoiceStatus.LATE
        else:
            aging["1_to_15_days"] += 1
            inv.status = InvoiceStatus.LATE
        count_marked += 1

    db.commit()
    return {
        "status": "COMPLETED",
        "invoices_evaluated": len(overdue_invoices),
        "invoices_marked_overdue": count_marked,
        "aging_breakdown": aging,
        "timestamp": now
    }


# ─── 7. Finance Overview KPI Aggregation ──────────────────────────────────────

def get_finance_overview(db: Session, market_id: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate executive billing KPIs, outstanding receivables, and gateway health."""
    from app.services.payment_provider import get_payment_gateway_status

    inv_query = db.query(Invoice)
    pay_query = db.query(Payment).filter(Payment.status.in_([PaymentStatus.COMPLETED, PaymentStatus.ALLOCATED, PaymentStatus.PARTIALLY_ALLOCATED]))
    sub_query = db.query(BillingSubscription).filter(BillingSubscription.status == SubscriptionStatus.ACTIVE)
    acct_query = db.query(BillingAccount).filter(BillingAccount.status == BillingAccountStatus.ACTIVE)

    if market_id:
        inv_query = inv_query.filter(Invoice.market_id == market_id)
        pay_query = pay_query.filter(Payment.market_id == market_id)
        sub_query = sub_query.filter(BillingSubscription.market_id == market_id)
        acct_query = acct_query.filter(BillingAccount.market_id == market_id)

    # Invoiced amount
    total_invoiced = inv_query.with_entities(func.sum(Invoice.total_amount)).scalar() or Decimal('0.00')
    total_collected = pay_query.with_entities(func.sum(Payment.amount)).scalar() or Decimal('0.00')
    total_receivables = inv_query.filter(Invoice.status != InvoiceStatus.PAID).with_entities(func.sum(Invoice.outstanding_amount)).scalar() or Decimal('0.00')

    # Overdue
    now = datetime.utcnow()
    overdue_items = inv_query.filter(
        Invoice.outstanding_amount > Decimal('0.00'),
        Invoice.due_date < now
    ).all()
    overdue_count = len(overdue_items)
    overdue_balance = sum((_format_money(i.outstanding_amount) for i in overdue_items), Decimal('0.00'))

    # Recent activity
    recent_pays = pay_query.order_by(Payment.created_at.desc()).limit(10).all()
    recent_invs = inv_query.order_by(Invoice.created_at.desc()).limit(10).all()

    return {
        "currency": "INR",
        "date_range": f"Current Fiscal Period ({datetime.utcnow().year})",
        "total_invoiced_amount": float(total_invoiced),
        "total_collected_amount": float(total_collected),
        "total_receivables": float(total_receivables),
        "overdue_invoices_count": overdue_count,
        "overdue_invoices_balance": float(overdue_balance),
        "active_subscriptions_count": sub_query.count(),
        "active_billing_accounts_count": acct_query.count(),
        "recent_payments_count": len(recent_pays),
        "recent_payments": recent_pays,
        "recent_invoices": recent_invs,
        "gateway_status": get_payment_gateway_status()
    }
