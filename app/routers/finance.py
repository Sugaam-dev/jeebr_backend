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

import uuid
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status, Response, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.database import get_db
from app.models import User, Customer, Invoice, AuditLog
from app.models_crm import CRMCustomer
from app.models_finance import (
    BillingAccount, BillingSubscription, Payment, CreditNote,
    RefundRecord, JournalEntry, VoucherRedemption, BillingAccountStatus,
    SubscriptionStatus, InvoiceStatus, PaymentStatus, BillingJobRun, NotificationOutbox,
    BillingType
)
from app.schemas import (
    BillingAccountResponse, BillingAccountCreateRequest,
    PaymentOrderCreateRequest, PaymentOrderResponse,
    StorageDiagnosticResponse, CustomerBillingSummaryResponse,
    BillingSubscriptionResponse, BillingSubscriptionCreateRequest, SubscriptionTransitionRequest,
    InvoiceDetailResponse, InvoiceCreateRequest,
    PaymentResponse, PaymentRecordRequest,
    CreditNoteResponse, CreditNoteCreateRequest,
    RefundRecordResponse, RefundCreateRequest,
    JournalEntryResponse, JournalEntryReverseRequest, LedgerBalanceResponse,
    VoucherRedeemRequest, VoucherRedemptionResponse,
    FinanceOverviewResponse, BillingRunJobResponse, GatewayStatusResponse,
    BillingJobRunResponse, BillingJobRunsListResponse, ScheduledJobDescriptor,
    JobRunManualRequest, JobReconciliationRequest,
    NotificationOutboxItemResponse, NotificationOutboxListResponse, SchedulerOverviewResponse,
    SchedulerRunnerStatusResponse, SchedulerRunnerToggleRequest
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
from app.services.billing_scheduler import (
    execute_durable_billing_job, get_scheduled_job_descriptors, get_job_runs,
    get_job_run_detail, reconcile_job_run, get_notification_outbox_items,
    get_scheduler_overview_kpis, ALLOWLISTED_JOBS, normalize_job_type
)
from app.services.notification_service import process_notification_outbox

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
    # 1. Resolve target customer by int ID, customer_code or CRM customer number
    cust_id_raw = req.customer_id
    customer = None
    if isinstance(cust_id_raw, int) or (isinstance(cust_id_raw, str) and cust_id_raw.isdigit()):
        customer = db.query(Customer).filter(Customer.id == int(cust_id_raw)).first()
    if not customer:
        customer = db.query(Customer).filter(Customer.customer_code == str(cust_id_raw).strip()).first()
    if not customer:
        crm_c = db.query(CRMCustomer).filter(CRMCustomer.customer_number == str(cust_id_raw).strip()).first()
        if crm_c and crm_c.legacy_customer_id:
            customer = db.query(Customer).filter(Customer.id == crm_c.legacy_customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer '{req.customer_id}' not found."
        )

    # 2. Market isolation
    target_market = (req.market_id or market).strip().lower()
    if current_user.role != "SUPER_ADMIN" and customer.market_id.lower() != target_market:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cross-market violation: Customer belongs to market '{customer.market_id}', not authorized for '{target_market}'."
        )

    # 3. Validate billing type
    raw_btype = req.billing_type or req.account_type or "POSTPAID"
    btype = raw_btype.upper().strip()
    if btype not in BillingType.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid billing_type '{raw_btype}'. Allowed types: {list(BillingType.ALL)}"
        )

    # 4. Validate monetary fields (credit_limit >= 0)
    if req.credit_limit is not None and req.credit_limit < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Credit limit cannot be negative."
        )

    # 5. Prevent duplicate accounts for same customer
    existing_acct = db.query(BillingAccount).filter(
        BillingAccount.customer_id == customer.id
    ).first()
    if existing_acct:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Billing account already exists for customer '{customer.customer_code}' ({existing_acct.account_number})."
        )

    try:
        acct = ensure_billing_account(
            db=db,
            customer_id=customer.id,
            market_id=customer.market_id,
            billing_type=btype,
            currency=req.currency or "INR",
            credit_limit=req.credit_limit or 0.0,
            payment_term_days=req.payment_term_days or 15
        )
        resp = BillingAccountResponse.model_validate(acct)
        resp.account_code = acct.account_number
        if acct.customer:
            resp.customer_name = acct.customer.name
            resp.customer_code = acct.customer.customer_code
        return resp
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        db.rollback()
        raise


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


@router.post("/invoices/issue", response_model=InvoiceDetailResponse)
@router.post("/invoices", response_model=InvoiceDetailResponse)
def create_invoice(
    req: InvoiceCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Generate an itemized invoice for a billable subscription."""
    # 1. Resolve subscription
    sub = None
    try:
        uuid_obj = uuid.UUID(str(req.subscription_id))
        sub = db.query(BillingSubscription).filter(BillingSubscription.id == uuid_obj).first()
    except (ValueError, AttributeError):
        pass
    if not sub:
        sub = db.query(BillingSubscription).filter(BillingSubscription.subscription_code == str(req.subscription_id).strip()).first()
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription '{req.subscription_id}' not found."
        )

    # 2. Market isolation
    if current_user.role != "SUPER_ADMIN" and sub.market_id.lower() != market.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not authorized to bill subscription in market '{sub.market_id}'."
        )

    # 3. Validate dates
    if req.billing_period_start and req.billing_period_end:
        if req.billing_period_start >= req.billing_period_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Billing period start must be strictly before billing period end."
            )

    # 4. Validate monetary fields
    if req.discount_amount is not None and req.discount_amount < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Discount amount cannot be negative."
        )
    if req.tax_rate is not None and (req.tax_rate < 0 or req.tax_rate > 100):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tax rate must be between 0.0 and 100.0 percent."
        )

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
        db.rollback()
        err_msg = str(e)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
    except Exception:
        db.rollback()
        raise


@router.get("/invoices/{invoice_id}/download")
def download_invoice_document(
    invoice_id: int,
    format: str = Query("json", description="Document format: 'pdf' or 'json'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Generate downloadable tax invoice document (valid PDF binary or JSON metadata)."""
    inv = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.market_id == market).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    _assert_customer_access(inv.customer_id, current_user, db)

    if format.lower() == "pdf":
        from app.services.invoice_document import generate_invoice_pdf
        pdf_bytes = generate_invoice_pdf(inv)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="Invoice-{inv.invoice_code}.pdf"',
                "Content-Type": "application/pdf"
            }
        )

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
    reason: Optional[str] = Query(None, description="Reversal reason"),
    payload: Optional[JournalEntryReverseRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """Post a balancing reversing journal entry for corrections."""
    effective_reason = (payload.reason if payload and payload.reason else None) or reason or "General ledger entry reversal"
    try:
        rev = reverse_journal_entry(db, entry_id=entry_id, reason=effective_reason, created_by_id=current_user.id)
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


# ─── 9. Scheduled Background Jobs & Operations Console ────────────────────────

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


@router.get("/scheduler/overview", response_model=SchedulerOverviewResponse)
def get_scheduler_overview(
    market_id: Optional[str] = Query(None, description="Optional market filter"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Viewer", "Care"])),
    current_market: str = Depends(get_current_market)
):
    """Aggregates scheduler health, active distributed locks, run statistics, and outbox KPIs."""
    target_market = market_id or current_market
    return get_scheduler_overview_kpis(db, market_id=target_market)


@router.get("/scheduler/jobs", response_model=List[ScheduledJobDescriptor])
def list_scheduled_jobs(
    market_id: Optional[str] = Query(None, description="Optional market filter"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Viewer", "Care"])),
    current_market: str = Depends(get_current_market)
):
    """Returns catalog of known allowlisted jobs, active lock states, cadence, and last execution outcome."""
    target_market = market_id or current_market
    return get_scheduled_job_descriptors(db, market_id=target_market)


@router.get("/scheduler/runner/status", response_model=SchedulerRunnerStatusResponse)
def get_scheduler_runner_telemetry(
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Viewer", "Care"]))
):
    """Deep telemetry for automated background scheduler runner (leader, heartbeat, schedules)."""
    from app.services.billing_scheduler_runner import scheduler_runner
    return scheduler_runner.get_status()


@router.post("/scheduler/runner/toggle")
async def toggle_scheduler_runner_state(
    payload: SchedulerRunnerToggleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN"]))
):
    """Enables or pauses the automated background scheduler daemon loop."""
    from app.services.billing_scheduler_runner import scheduler_runner
    if payload.enabled:
        await scheduler_runner.start()
        action = "START_SCHEDULER_RUNNER"
    else:
        await scheduler_runner.stop()
        action = "PAUSE_SCHEDULER_RUNNER"

    try:
        audit = AuditLog(
            market_id="mumbai",
            source_module="BILLING_SCHEDULER",
            action_taken=action,
            decision="EXECUTED",
            user_id=current_user.id,
            user_name=current_user.email or "Admin",
            user_role=current_user.role,
            confidence_score=1.0,
            original_signals={"requested_enabled": payload.enabled},
            execution_result={"runner_enabled": payload.enabled}
        )
        db.add(audit)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to record audit log for scheduler runner toggle: {e}")

    return scheduler_runner.get_status()


@router.get("/scheduler/runs", response_model=BillingJobRunsListResponse)
def list_billing_job_runs(
    job_type: Optional[str] = Query(None, description="Filter by job type"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by execution status"),
    market_id: Optional[str] = Query(None, description="Filter by market ID"),
    date_from: Optional[str] = Query(None, description="ISO format start date"),
    date_to: Optional[str] = Query(None, description="ISO format end date"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Viewer", "Care"])),
    current_market: str = Depends(get_current_market)
):
    """Paginated execution history of background billing and dunning job runs."""
    d_from = None
    d_to = None
    if date_from:
        try:
            d_from = datetime.fromisoformat(date_from)
        except Exception:
            pass
    if date_to:
        try:
            d_to = datetime.fromisoformat(date_to)
        except Exception:
            pass

    target_market = market_id if market_id is not None else current_market
    return get_job_runs(
        db=db,
        job_type=job_type,
        status=status_filter,
        market_id=target_market,
        date_from=d_from,
        date_to=d_to,
        page=page,
        page_size=page_size
    )


@router.get("/scheduler/runs/{run_id}", response_model=BillingJobRunResponse)
def get_billing_job_run_detail(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Viewer", "Care"]))
):
    """Retrieves deep execution details, durations, processed records, and execution metrics for a specific run."""
    run = get_job_run_detail(db, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Billing job run '{run_id}' not found."
        )
    return run


@router.post("/scheduler/jobs/{job_type}/run")
def run_scheduled_job_manually(
    job_type: str,
    payload: Optional[JobRunManualRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"])),
    current_market: str = Depends(get_current_market)
):
    """
    Manually triggers an allowlisted background job under distributed lock protection.
    Enforces RBAC, market scoping, concurrency protection, and audit log recording.
    """
    canonical_type = normalize_job_type(job_type)
    if canonical_type not in ALLOWLISTED_JOBS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job type '{job_type}' is not allowlisted for manual execution. Permitted: {', '.join(ALLOWLISTED_JOBS)}"
        )

    target_market = None
    if payload and payload.market_id:
        target_market = payload.market_id if payload.market_id != "all" else None
    elif current_market and current_market != "all":
        target_market = current_market

    res = execute_durable_billing_job(
        db=db,
        job_type=canonical_type,
        market_id=target_market,
        initiated_by=f"MANUAL_{current_user.email}"
    )

    if res.get("status") == "SKIPPED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=res.get("reason", "Job execution skipped due to active concurrent lock.")
        )

    # Durable audit attribution
    try:
        audit = AuditLog(
            market_id=target_market or "mumbai",
            source_module="BILLING_SCHEDULER",
            action_taken=f"TRIGGER_MANUAL_JOB_{canonical_type}",
            decision="EXECUTED" if res.get("status") == "COMPLETED" else "FAILED",
            user_id=current_user.id,
            user_name=current_user.email or current_user.full_name or "Operator",
            user_role=current_user.role,
            confidence_score=1.0,
            original_signals={"job_type": canonical_type, "market_id": target_market},
            execution_result=res
        )
        db.add(audit)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to record audit log for manual job execution: {e}")

    return res


@router.post("/scheduler/runs/{run_id}/reconcile")
def reconcile_failed_or_stalled_run(
    run_id: str,
    payload: JobReconciliationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"]))
):
    """
    Reconciles a stalled or failed job run, clearing any stuck distributed locks.
    Supports action='RECONCILE' to mark recovered or action='RETRY' to re-execute safely.
    """
    try:
        result = reconcile_job_run(
            db=db,
            run_id=run_id,
            action=payload.action,
            reason=payload.reason or "Manual operator reconciliation",
            initiated_by=current_user.email
        )

        audit = AuditLog(
            market_id="mumbai",
            source_module="BILLING_SCHEDULER",
            action_taken=f"RECONCILE_JOB_{payload.action.upper()}",
            decision="EXECUTED",
            user_id=current_user.id,
            user_name=current_user.email or "Operator",
            user_role=current_user.role,
            confidence_score=1.0,
            original_signals={"run_id": run_id, "action": payload.action, "reason": payload.reason},
            execution_result={"status": "RECONCILED", "run_id": run_id}
        )
        db.add(audit)
        db.commit()

        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


# ─── 10. Durable Notification Outbox Queue Operations ────────────────────────

@router.get("/notifications/outbox", response_model=NotificationOutboxListResponse)
def list_notification_outbox(
    channel: Optional[str] = Query(None, description="Filter by delivery channel (SMS, EMAIL, WHATSAPP)"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by queue status"),
    market_id: Optional[str] = Query(None, description="Filter by market ID"),
    customer_id: Optional[int] = Query(None, description="Filter by customer ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue", "Viewer", "Care"])),
    current_market: str = Depends(get_current_market)
):
    """
    Returns durable customer notification outbox records with strict PII & OTP redaction.
    Displays recipient masking, delivery channel, retry counts, simulated status, and last errors.
    """
    target_market = market_id if market_id is not None else current_market
    return get_notification_outbox_items(
        db=db,
        channel=channel,
        status=status_filter,
        market_id=target_market,
        customer_id=customer_id,
        page=page,
        page_size=page_size
    )


@router.post("/notifications/outbox/process")
def process_outbox_manually(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "Revenue"]))
):
    """Triggers safe idempotent dispatch of pending outbox notifications."""
    res = execute_durable_billing_job(
        db=db,
        job_type="OUTBOX_DISPATCH",
        market_id="all",
        initiated_by=f"MANUAL_DISPATCH_{current_user.email}"
    )
    return res



# ─── Payment Order Initiation & Razorpay Webhooks (Phase 9B Workstream A) ─────

@router.post("/payments/create-order", response_model=PaymentOrderResponse)
@router.post("/payments/order", response_model=PaymentOrderResponse)
def create_payment_order_endpoint(
    req: PaymentOrderCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Customer", "Admin", "SUPER_ADMIN", "Revenue"])),
    market: str = Depends(get_current_market)
):
    """
    Authorizes an online payment order through Razorpay or simulated gateway.
    Returns payment order parameters for customer frontend checkout.
    """
    norm_role = normalize_role_name(current_user.role)
    if norm_role == "Customer":
        cust = db.query(Customer).filter(Customer.email == current_user.email).first()
        if not cust:
            raise HTTPException(status_code=403, detail="Customer identity not found.")
        target_customer_id = cust.id
    else:
        inv = db.query(Invoice).filter(Invoice.id == req.invoice_id).first()
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        target_customer_id = inv.customer_id

    try:
        from app.services.billing_service import initiate_payment_order
        res = initiate_payment_order(
            db=db,
            invoice_id=req.invoice_id,
            customer_id=target_customer_id,
            amount=req.amount,
            payment_method=req.payment_method or "UPI"
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhooks/razorpay")
async def razorpay_webhook_endpoint(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Durable, idempotent webhook receiver for Razorpay payment callbacks.
    Verifies HMAC-SHA256 signature against exact raw request body bytes.
    Deduplicates events and reconciles ledger state.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id")

    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Razorpay-Signature header.")

    try:
        from app.services.billing_service import process_razorpay_webhook
        result = process_razorpay_webhook(
            db=db,
            raw_body=raw_body,
            signature=signature,
            event_id=event_id
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Storage Diagnostics (Phase 9B Workstream J) ─────────────────────────────

@router.get("/storage-status", response_model=StorageDiagnosticResponse)
def get_storage_status_endpoint(
    current_user: User = Depends(get_current_user)
):
    """Inspect and report non-sensitive document object storage configuration."""
    from app.services.document_storage import get_storage_diagnostic
    return get_storage_diagnostic()


# ─── Customer Self-Service Billing Summary (Phase 9B Workstream E) ───────────

@router.get("/my-billing", response_model=CustomerBillingSummaryResponse)
def get_my_billing_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Customer", "Admin", "SUPER_ADMIN"]))
):
    """
    Consolidated customer self-service billing endpoint.
    Resolves customer identity from authenticated session.
    """
    email_clean = current_user.email.lower().strip()
    cust = db.query(Customer).filter(func.lower(Customer.email) == email_clean).first()
    if not cust:
        # Fallback for admin previewing customer billing
        if current_user.role in ("Admin", "SUPER_ADMIN", "Viewer"):
            cust = db.query(Customer).first()
            if not cust:
                raise HTTPException(status_code=404, detail="Customer record not found.")
        else:
            return {
                "customer_id": 0,
                "customer_code": "PENDING-PROVISIONING",
                "customer_name": current_user.full_name or current_user.email,
                "billing_account": None,
                "active_subscriptions": [],
                "recent_invoices": [],
                "recent_payments": [],
                "total_outstanding": 0.0,
                "account_balance": 0.0
            }

    account = db.query(BillingAccount).filter(BillingAccount.customer_id == cust.id).first()
    subs = db.query(BillingSubscription).filter(BillingSubscription.customer_id == cust.id).all()
    invoices = db.query(Invoice).filter(Invoice.customer_id == cust.id).order_by(Invoice.created_at.desc()).limit(20).all()
    payments = db.query(Payment).filter(Payment.customer_id == cust.id).order_by(Payment.created_at.desc()).limit(20).all()

    total_outstanding = sum(float(inv.outstanding_amount) for inv in invoices if inv.status in ("Issued", "Late", "Overdue", "Partially_Paid"))
    balance = float(account.balance) if account else 0.0

    return {
        "customer_id": cust.id,
        "customer_code": cust.customer_code,
        "customer_name": cust.name,
        "billing_account": BillingAccountResponse.model_validate(account) if account else None,
        "active_subscriptions": [BillingSubscriptionResponse.model_validate(s) for s in subs],
        "recent_invoices": [InvoiceDetailResponse.model_validate(i) for i in invoices],
        "recent_payments": [PaymentResponse.model_validate(p) for p in payments],
        "total_outstanding": total_outstanding,
        "account_balance": balance
    }
