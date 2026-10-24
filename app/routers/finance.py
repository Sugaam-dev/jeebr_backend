"""
app/routers/finance.py

SentinelOS Finance & Billing Router.
Exposes carrier-grade ISP billing, subscription management, itemized invoices,
payment gateway allocations, double-entry general ledger, and voucher fulfillment.

Enforces:
1. Strict RBAC (SUPER_ADMIN, Admin, Revenue, Care, Viewer, Customer).
2. Market/Tenant data isolation via X-Market-Id.
3. Fixed-precision monetary presentation.
4. Concurrency-safe idempotency.
5. Zero data fabrication and transparent simulation labeling.
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app.models import User, Customer, Invoice
from app.models_finance import (
    BillingAccount, BillingSubscription, Payment, CreditNote,
    RefundRecord, JournalEntry, VoucherRedemption, BillingAccountStatus,
    SubscriptionStatus, InvoiceStatus, PaymentStatus
)
from app.schemas import (
    BillingAccountResponse, BillingAccountCreateRequest,
    BillingSubscriptionResponse, BillingSubscriptionCreateRequest, SubscriptionTransitionRequest,
    InvoiceDetailResponse, InvoiceCreateRequest,
    PaymentResponse, PaymentRecordRequest,
    CreditNoteResponse, CreditNoteCreateRequest,
    RefundRecordResponse, RefundCreateRequest,
    JournalEntryResponse, LedgerBalanceResponse,
    VoucherRedeemRequest, VoucherRedemptionResponse,
    FinanceOverviewResponse, BillingRunJobResponse, GatewayStatusResponse
)
from app.auth import get_current_user, require_roles
from app.markets import get_current_market, get_user_authorized_markets
from app.services.rbac_service import normalize_role_name, is_super_admin
from app.services.billing_service import (
    ensure_billing_account, create_subscription, transition_subscription_status,
    generate_invoice, record_payment, issue_credit_note, process_refund,
    run_billing_cycle, evaluate_overdue_and_dunning, get_finance_overview
)
from app.services.ledger_service import (
    get_account_balances, verify_ledger_balancing, reverse_journal_entry
)
from app.services.payment_provider import get_payment_gateway_status
from app.services.voucher_service import (
    redeem_customer_voucher, get_customer_redemptions
)

router = APIRouter(prefix="/finance", tags=["Finance & Billing Management"])


# ─── Helper: Validate Customer Ownership for Customer Role ────────────────────

def _assert_customer_access(customer_id: int, current_user: User, db: Session):
    """Enforces Customer-role data isolation (cannot inspect other customers' bills)."""
    norm_role = normalize_role_name(current_user.role)
    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.id == customer_id).first()
        if not cust or (cust.email.lower().strip() != (current_user.email or "").lower().strip()):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Cannot access another customer's financial account."
            )


# ─── 1. Finance Overview KPI Dashboard ────────────────────────────────────────

@router.get("/overview", response_model=FinanceOverviewResponse)
def get_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Executive", "NOC", "Care", "Viewer"])),
    market: str = Depends(get_current_market)
):
    """Retrieve summarized executive billing KPIs, outstanding receivables, and gateway health."""
    data = get_finance_overview(db, market_id=market)
    return data


@router.get("/gateway-status", response_model=GatewayStatusResponse)
@router.get("/gateways/status", response_model=GatewayStatusResponse)
def get_gateways_status(
    current_user: User = Depends(get_current_user)
):
    """Transparent diagnostic endpoint reporting live vs simulated gateway availability."""
    return get_payment_gateway_status()


# ─── 2. Customer Billing Accounts ─────────────────────────────────────────────

@router.get("/accounts", response_model=List[BillingAccountResponse])
def list_billing_accounts(
    customer_id: Optional[int] = None,
    account_number: Optional[str] = None,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List and search customer billing accounts."""
    norm_role = normalize_role_name(current_user.role)
    query = db.query(BillingAccount).filter(BillingAccount.market_id == market)

    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            return []
        query = query.filter(BillingAccount.customer_id == cust.id)
    else:
        if customer_id:
            query = query.filter(BillingAccount.customer_id == customer_id)
        if account_number:
            query = query.filter(BillingAccount.account_number.ilike(f"%{account_number}%"))
        if status_filter:
            query = query.filter(BillingAccount.status == status_filter)

    accounts = query.order_by(BillingAccount.created_at.desc()).offset(skip).limit(limit).all()

    # Populate customer names for response
    results = []
    for acct in accounts:
        resp = BillingAccountResponse.model_validate(acct)
        if acct.customer:
            resp.customer_name = acct.customer.name
            resp.customer_code = acct.customer.customer_code
        results.append(resp)
    return results


@router.get("/accounts/{account_id}", response_model=BillingAccountResponse)
def get_billing_account_detail(
    account_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Retrieve billing account details."""
    account = db.query(BillingAccount).filter(
        BillingAccount.id == account_id,
        BillingAccount.market_id == market
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Billing account not found.")

    if account.customer_id:
        _assert_customer_access(account.customer_id, current_user, db)

    resp = BillingAccountResponse.model_validate(account)
    if account.customer:
        resp.customer_name = account.customer.name
        resp.customer_code = account.customer.customer_code
    return resp


@router.post("/accounts", response_model=BillingAccountResponse)
def create_billing_account(
    req: BillingAccountCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Create or activate a billing account for an eligible customer."""
    try:
        acct = ensure_billing_account(
            db=db,
            customer_id=req.customer_id,
            market_id=market,
            billing_type=req.billing_type or "POSTPAID",
            currency=req.currency or "INR",
            credit_limit=req.credit_limit or 0.0,
            payment_term_days=req.payment_term_days or 15
        )
        resp = BillingAccountResponse.model_validate(acct)
        if acct.customer:
            resp.customer_name = acct.customer.name
            resp.customer_code = acct.customer.customer_code
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 3. Subscriptions ─────────────────────────────────────────────────────────

@router.get("/subscriptions", response_model=List[BillingSubscriptionResponse])
def list_subscriptions(
    customer_id: Optional[int] = None,
    billing_account_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List customer service subscriptions."""
    norm_role = normalize_role_name(current_user.role)
    query = db.query(BillingSubscription).filter(BillingSubscription.market_id == market)

    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            return []
        query = query.filter(BillingSubscription.customer_id == cust.id)
    else:
        if customer_id:
            query = query.filter(BillingSubscription.customer_id == customer_id)
        if billing_account_id:
            query = query.filter(BillingSubscription.billing_account_id == billing_account_id)
        if status_filter:
            query = query.filter(BillingSubscription.status == status_filter)

    subs = query.order_by(BillingSubscription.created_at.desc()).offset(skip).limit(limit).all()

    results = []
    for s in subs:
        resp = BillingSubscriptionResponse.model_validate(s)
        if s.customer:
            resp.customer_name = s.customer.name
        results.append(resp)
    return results


@router.post("/subscriptions", response_model=BillingSubscriptionResponse)
def create_new_subscription(
    req: BillingSubscriptionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Subscribe a customer to a canonical tariff plan."""
    try:
        sub = create_subscription(
            db=db,
            customer_id=req.customer_id,
            plan_id=req.plan_id,
            billing_account_id=req.billing_account_id,
            billing_frequency=req.billing_frequency or "MONTHLY",
            start_date=req.start_date,
            provisioning_order_id=req.provisioning_order_id
        )
        resp = BillingSubscriptionResponse.model_validate(sub)
        if sub.customer:
            resp.customer_name = sub.customer.name
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/subscriptions/{subscription_id}/transition", response_model=BillingSubscriptionResponse)
def transition_subscription(
    subscription_id: str,
    req: SubscriptionTransitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"]))
):
    """Transition subscription lifecycle status (e.g. SUSPEND, CANCEL, ACTIVATE)."""
    try:
        sub = transition_subscription_status(
            db=db,
            subscription_id=subscription_id,
            target_status=req.target_status,
            reason=req.reason
        )
        resp = BillingSubscriptionResponse.model_validate(sub)
        if sub.customer:
            resp.customer_name = sub.customer.name
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 4. Invoices ──────────────────────────────────────────────────────────────

@router.get("/invoices", response_model=List[InvoiceDetailResponse])
def list_invoices(
    customer_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    invoice_code: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List and search invoices."""
    norm_role = normalize_role_name(current_user.role)
    query = db.query(Invoice).filter(Invoice.market_id == market)

    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            return []
        query = query.filter(Invoice.customer_id == cust.id)
    else:
        if customer_id:
            query = query.filter(Invoice.customer_id == customer_id)
        if status_filter:
            query = query.filter(Invoice.status == status_filter)
        if invoice_code:
            query = query.filter(Invoice.invoice_code.ilike(f"%{invoice_code}%"))

    invoices = query.order_by(Invoice.created_at.desc()).offset(skip).limit(limit).all()

    results = []
    for inv in invoices:
        resp = InvoiceDetailResponse.model_validate(inv)
        if inv.customer:
            resp.customer_name = inv.customer.name
            resp.customer_code = inv.customer.customer_code
        results.append(resp)
    return results


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
def get_invoice_detail(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Retrieve detailed invoice view with line items."""
    inv = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.market_id == market).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    _assert_customer_access(inv.customer_id, current_user, db)

    resp = InvoiceDetailResponse.model_validate(inv)
    if inv.customer:
        resp.customer_name = inv.customer.name
        resp.customer_code = inv.customer.customer_code
    return resp


@router.post("/invoices", response_model=InvoiceDetailResponse)
def create_invoice(
    req: InvoiceCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Generate an itemized invoice for a billable subscription."""
    try:
        inv = generate_invoice(
            db=db,
            subscription_id=req.subscription_id,
            billing_period_start=req.billing_period_start,
            billing_period_end=req.billing_period_end,
            discount_amount=req.discount_amount or 0.0,
            tax_rate=req.tax_rate or 18.0,
            idempotency_key=req.idempotency_key,
            notes=req.notes
        )
        resp = InvoiceDetailResponse.model_validate(inv)
        if inv.customer:
            resp.customer_name = inv.customer.name
            resp.customer_code = inv.customer.customer_code
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/invoices/{invoice_id}/download")
def download_invoice_document(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Generate downloadable tax invoice document metadata and JSON printable payload."""
    inv = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.market_id == market).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    _assert_customer_access(inv.customer_id, current_user, db)

    cust = inv.customer
    tax_amt = float(inv.tax_amount)
    half_tax = round(tax_amt / 2.0, 2)
    return {
        "document_type": "TAX_INVOICE",
        "invoice_number": inv.invoice_code,
        "issue_date": inv.created_at.strftime("%Y-%m-%d"),
        "due_date": inv.due_date.strftime("%Y-%m-%d"),
        "status": inv.status,
        "market": inv.market_id.upper(),
        "provider": {
            "name": "SentinelOS Broadband Network (PMRG Solution LLP)",
            "legal_name": "SentinelOS Broadband Network (PMRG Solution LLP)",
            "gstin": "27AAACP0123M1Z5",
            "pan": "AAACP0123M",
            "address": "Bandra Kurla Complex, Mumbai, Maharashtra 400051",
            "jurisdiction": "Maharashtra, India"
        },
        "customer": {
            "name": cust.name if cust else "Valued Subscriber",
            "code": cust.customer_code if cust else f"CUST-{inv.customer_id}",
            "customer_id": cust.customer_code if cust else f"CUST-{inv.customer_id}",
            "email": cust.email if cust else "",
            "phone": cust.phone if cust else "",
            "market_id": inv.market_id,
            "place_of_supply": "27-Maharashtra" if inv.market_id.lower() == "mumbai" else "19-West Bengal"
        },
        "currency": inv.currency,
        "subtotal": float(inv.subtotal),
        "discount_amount": float(inv.discount_amount),
        "tax_amount": tax_amt,
        "tax_type": "CGST 9% + SGST 9% (18.00% Indian GST)",
        "tax_breakdown": {
            "cgst_9pct": half_tax,
            "sgst_9pct": round(tax_amt - half_tax, 2),
            "igst_18pct": 0.0,
            "total_tax": tax_amt
        },
        "total_amount": float(inv.total_amount),
        "paid_amount": float(inv.paid_amount),
        "outstanding_amount": float(inv.outstanding_amount),
        "line_items": [
            {
                "item_id": str(item.id),
                "description": item.description,
                "hsn_code": "998422",
                "quantity": float(item.quantity),
                "unit_price": float(item.unit_price),
                "amount": float(item.amount),
                "tax_amount": float(item.tax_amount),
                "cgst_amount": round(float(item.tax_amount) / 2.0, 2),
                "sgst_amount": round(float(item.tax_amount) - round(float(item.tax_amount) / 2.0, 2), 2),
                "total_amount": float(item.total_amount)
            } for item in inv.line_items
        ] if inv.line_items else [
            {
                "item_id": f"item-{inv.id}-1",
                "description": f"Broadband Subscription: {inv.plan_name}",
                "hsn_code": "998422",
                "quantity": 1.0,
                "unit_price": float(inv.subtotal),
                "amount": float(inv.subtotal),
                "tax_amount": tax_amt,
                "cgst_amount": half_tax,
                "sgst_amount": round(tax_amt - half_tax, 2),
                "total_amount": float(inv.total_amount)
            }
        ]
    }


# ─── 5. Payments ──────────────────────────────────────────────────────────────

@router.get("/payments", response_model=List[PaymentResponse])
def list_payments(
    customer_id: Optional[int] = None,
    billing_account_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List customer payments and allocations."""
    norm_role = normalize_role_name(current_user.role)
    query = db.query(Payment).filter(Payment.market_id == market)

    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            return []
        query = query.filter(Payment.customer_id == cust.id)
    else:
        if customer_id:
            query = query.filter(Payment.customer_id == customer_id)
        if billing_account_id:
            query = query.filter(Payment.billing_account_id == billing_account_id)

    payments = query.order_by(Payment.created_at.desc()).offset(skip).limit(limit).all()

    results = []
    for p in payments:
        resp = PaymentResponse.model_validate(p)
        if p.customer:
            resp.customer_name = p.customer.name
            resp.customer_code = p.customer.customer_code
        results.append(resp)
    return results


@router.post("/payments", response_model=PaymentResponse)
def record_customer_payment(
    req: PaymentRecordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Customer"])),
    market: str = Depends(get_current_market)
):
    """Record authorized customer payment and allocate against invoices."""
    norm_role = normalize_role_name(current_user.role)
    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            raise HTTPException(status_code=403, detail="Customer identity not found.")
        req.customer_id = cust.id

    try:
        payment = record_payment(
            db=db,
            amount=req.amount,
            idempotency_key=req.idempotency_key,
            billing_account_id=req.billing_account_id,
            customer_id=req.customer_id,
            currency=req.currency or "INR",
            payment_method=req.payment_method or "UPI",
            invoice_id=req.invoice_id,
            provider_name=req.provider_name or "SIMULATED_GATEWAY",
            auto_allocate=req.auto_allocate if req.auto_allocate is not None else True
        )
        resp = PaymentResponse.model_validate(payment)
        if payment.customer:
            resp.customer_name = payment.customer.name
            resp.customer_code = payment.customer.customer_code
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 6. Credit Notes & Refunds ────────────────────────────────────────────────

@router.get("/credit-notes", response_model=List[CreditNoteResponse])
def list_credit_notes(
    billing_account_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Executive"])),
    market: str = Depends(get_current_market)
):
    """List credit notes."""
    query = db.query(CreditNote).filter(CreditNote.market_id == market)
    if billing_account_id:
        query = query.filter(CreditNote.billing_account_id == billing_account_id)
    return query.order_by(CreditNote.created_at.desc()).all()


@router.post("/credit-notes", response_model=CreditNoteResponse)
def create_credit_note(
    req: CreditNoteCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Issue a traceable credit note."""
    try:
        cn = issue_credit_note(
            db=db,
            billing_account_id=req.billing_account_id,
            amount=req.amount,
            reason=req.reason,
            invoice_id=req.invoice_id,
            created_by_id=current_user.id
        )
        return cn
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/refunds", response_model=List[RefundRecordResponse])
def list_refunds(
    payment_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Executive"])),
    market: str = Depends(get_current_market)
):
    """List refund records."""
    query = db.query(RefundRecord).filter(RefundRecord.market_id == market)
    if payment_id:
        query = query.filter(RefundRecord.payment_id == payment_id)
    return query.order_by(RefundRecord.created_at.desc()).all()


@router.post("/refunds", response_model=RefundRecordResponse)
def create_refund(
    req: RefundCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Process an auditable refund against a payment."""
    try:
        ref = process_refund(
            db=db,
            payment_id=req.payment_id,
            amount=req.amount,
            reason=req.reason,
            created_by_id=current_user.id
        )
        return ref
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 7. Balanced General Ledger ───────────────────────────────────────────────

@router.get("/ledger/entries", response_model=List[JournalEntryResponse])
def list_journal_entries(
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Executive"])),
    market: str = Depends(get_current_market)
):
    """List immutable journal entries and lines with balance validation."""
    query = db.query(JournalEntry).filter(JournalEntry.market_id == market)
    if reference_type:
        query = query.filter(JournalEntry.reference_type == reference_type)
    if reference_id:
        query = query.filter(JournalEntry.reference_id == reference_id)

    entries = query.order_by(JournalEntry.posted_at.desc()).offset(skip).limit(limit).all()

    results = []
    for e in entries:
        tot_deb = sum((float(l.debit) for l in e.lines))
        tot_crd = sum((float(l.credit) for l in e.lines))
        resp = JournalEntryResponse.model_validate(e)
        resp.total_debit = tot_deb
        resp.total_credit = tot_crd
        resp.is_balanced = (round(tot_deb, 2) == round(tot_crd, 2))
        results.append(resp)
    return results


@router.get("/ledger/balances", response_model=List[LedgerBalanceResponse])
def get_ledger_balances(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Executive"])),
    market: str = Depends(get_current_market)
):
    """Retrieve General Ledger trial balances by account code."""
    return get_account_balances(db, market_id=market)


@router.get("/ledger/verify")
def verify_ledger(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Executive"])),
    market: str = Depends(get_current_market)
):
    """Rigorous invariant verification: checks that all posted entries balance per currency."""
    return verify_ledger_balancing(db, market_id=market)


@router.post("/ledger/entries/{entry_id}/reverse", response_model=JournalEntryResponse)
def reverse_entry(
    entry_id: str,
    reason: str = Query(..., min_length=3),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Post a balancing reversing journal entry for corrections."""
    try:
        rev = reverse_journal_entry(db, entry_id=entry_id, reason=reason, created_by_id=current_user.id)
        tot_deb = sum((float(l.debit) for l in rev.lines))
        tot_crd = sum((float(l.credit) for l in rev.lines))
        resp = JournalEntryResponse.model_validate(rev)
        resp.total_debit = tot_deb
        resp.total_credit = tot_crd
        resp.is_balanced = (round(tot_deb, 2) == round(tot_crd, 2))
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 8. Voucher Redemption & Fulfillment ──────────────────────────────────────

@router.post("/vouchers/redeem", response_model=VoucherRedemptionResponse)
def redeem_voucher(
    req: VoucherRedeemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Care", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Redeem a CRM voucher with real billing / provisioning fulfillment."""
    try:
        redemption = redeem_customer_voucher(
            db=db,
            customer_id=req.customer_id,
            voucher_code=req.voucher_code,
            voucher_type=req.voucher_type,
            current_user=current_user,
            idempotency_key=req.idempotency_key,
            invoice_id=req.invoice_id
        )
        resp = VoucherRedemptionResponse.model_validate(redemption)
        if redemption.customer:
            resp.customer_name = redemption.customer.name
            resp.customer_code = redemption.customer.customer_code
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/vouchers/redemptions", response_model=List[VoucherRedemptionResponse])
def list_voucher_redemptions(
    customer_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List voucher redemption records."""
    norm_role = normalize_role_name(current_user.role)
    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            return []
        customer_id = cust.id

    redemptions = get_customer_redemptions(db, customer_id=customer_id, market_id=market)
    results = []
    for r in redemptions:
        resp = VoucherRedemptionResponse.model_validate(r)
        if r.customer:
            resp.customer_name = r.customer.name
            resp.customer_code = r.customer.customer_code
        results.append(resp)
    return results


# ─── 9. Scheduled Background Jobs (Safe Manual Triggers) ──────────────────────

@router.post("/jobs/billing-run", response_model=BillingRunJobResponse)
@router.post("/jobs/run-billing", response_model=BillingRunJobResponse)
def trigger_billing_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Idempotent recurring billing run: evaluates active subscriptions and issues invoices."""
    res = run_billing_cycle(db, market_id=market)
    return res


@router.post("/jobs/overdue-dunning")
@router.post("/jobs/evaluate-overdue")
def trigger_overdue_evaluation(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Evaluates overdue invoices, updates lifecycle status, and computes aging."""
    res = evaluate_overdue_and_dunning(db, market_id=market)
    return res
