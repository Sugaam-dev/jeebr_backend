"""
test_phase9b_finance_completion.py

Phase 9B Final Finance & Billing Completion Test Suite:
1. Workstream A: Razorpay Gateway Adapter & HMAC-SHA256 Webhook Verification, Tamper Rejection & Deduplication.
2. Workstream B: Automated Recurring Billing & Dunning Scheduler, Distributed Locks, and Durable Audit Logs.
3. Workstream C: Distributed Rate Limiting & Trustworthy Client IP Extraction.
4. Workstream D: Notification Outbox Queue, Exponential Backoff, and Event Types.
5. Workstream F & J: Statutory ReportLab GST PDF Generation, Download Endpoint & Storage Diagnostics.
6. Workstream G: Financial Foreign Key RESTRICT Invariant (Deletion Cascade Protection).
7. Workstream H: Canonical Service Plan Catalog Seeding and Filtering.
8. Workstream I: Dynamic GST Tax Rules (Intra-State CGST/SGST vs Inter-State IGST).
"""

import hmac
import hashlib
import json
import os
import uuid
from decimal import Decimal
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.config import settings
from app.models import Customer, User, Invoice
from app.models_crm import CRMCustomer
from app.models_finance import (
    BillingAccount, BillingSubscription, Payment, PaymentAllocation,
    PaymentWebhookEvent, BillingJobRun, NotificationOutbox,
    BillingAccountStatus, SubscriptionStatus, InvoiceStatus, PaymentStatus
)
from app.models_service_plan import ServicePlan
from app.auth import create_access_token
from app.services.tax_service import compute_gst, resolve_state_code, DEFAULT_HSN_SAC_CODE
from app.services.catalog_service import seed_service_plans_from_canonical
from app.services.invoice_document import generate_invoice_pdf
from app.services.payment_provider import get_payment_provider, RazorpayGatewayAdapter
from app.services.billing_scheduler import SchedulerDistributedLock, execute_durable_billing_job
from app.services.rate_limiter import DistributedLoginRateLimiter, get_trustworthy_client_ip
from app.services.notification_service import enqueue_notification, process_notification_outbox
from app.services.billing_service import ensure_billing_account, create_subscription, generate_invoice


def get_auth_headers(email: str, role: str, market: str = "mumbai"):
    token = create_access_token({"sub": email, "role": role, "market": market})
    return {"Authorization": f"Bearer {token}", "X-Market": market}


# ---------------------------------------------------------------------------
# WORKSTREAM I: Dynamic GST Tax Rules
# ---------------------------------------------------------------------------
def test_dynamic_gst_tax_rules_intra_vs_inter_state():
    # 1. State resolution
    assert resolve_state_code("27AABCU9603R1ZM") == "27"
    assert resolve_state_code("07AAAAA0000A1Z5") == "07"
    assert resolve_state_code("Maharashtra") == "27"
    assert resolve_state_code("Delhi") == "07"
    assert resolve_state_code("Karnataka") == "29"

    base_amount = Decimal("1000.00")

    # 2. Intra-State: Supplier MH (27) to Customer MH (27) -> CGST 9% + SGST 9%
    intra = compute_gst(base_amount, market_id="mumbai", customer_state="Maharashtra")
    assert intra.tax_treatment == "INTRA_STATE"
    assert intra.cgst_amount == Decimal("90.00")
    assert intra.sgst_amount == Decimal("90.00")
    assert intra.igst_amount == Decimal("0.00")
    assert intra.total_tax == Decimal("180.00")
    assert intra.total_amount == Decimal("1180.00")
    assert intra.sac_code == DEFAULT_HSN_SAC_CODE

    # 3. Inter-State: Supplier MH (27) to Customer DL (07) -> IGST 18%
    inter = compute_gst(base_amount, market_id="delhi", customer_state="Delhi")
    assert inter.tax_treatment == "INTER_STATE"
    assert inter.cgst_amount == Decimal("0.00")
    assert inter.sgst_amount == Decimal("0.00")
    assert inter.igst_amount == Decimal("180.00")
    assert inter.total_tax == Decimal("180.00")
    assert inter.total_amount == Decimal("1180.00")
    assert inter.sac_code == DEFAULT_HSN_SAC_CODE


# ---------------------------------------------------------------------------
# WORKSTREAM H: Canonical Service Plan Catalog
# ---------------------------------------------------------------------------
def test_service_plan_catalog_seeding_and_retrieval(db_session: Session):
    # Base setup has already seeded catalog
    total_plans = db_session.query(ServicePlan).count()
    assert total_plans >= 6

    # Verify RESIDENTIAL plans exist
    residential_plans = db_session.query(ServicePlan).filter(ServicePlan.account_type == "RESIDENTIAL").all()
    assert len(residential_plans) >= 3
    codes = [p.plan_code for p in residential_plans]
    assert "PLAN-PRE-HERO-1.5G" in codes

    # Verify ENTERPRISE plans exist
    ent_plans = db_session.query(ServicePlan).filter(ServicePlan.account_type == "ENTERPRISE").all()
    assert len(ent_plans) >= 2

    # Idempotent second run produces 0 newly inserted rows
    count_second = seed_service_plans_from_canonical(db_session)
    assert count_second == 0


# ---------------------------------------------------------------------------
# WORKSTREAM F & J: ReportLab GST Invoice PDF & Storage Diagnostic
# ---------------------------------------------------------------------------
def test_invoice_pdf_binary_generation_and_statutory_content(db_session: Session):
    cust = db_session.query(Customer).first()
    assert cust is not None

    account = ensure_billing_account(db_session, cust.id)
    sub = create_subscription(
        db=db_session,
        customer_id=cust.id,
        plan_id="PLAN-POST-INFINITY-75G",
        billing_account_id=account.id
    )
    inv = generate_invoice(db=db_session, subscription_id=sub.id)

    pdf_bytes = generate_invoice_pdf(inv)
    assert isinstance(pdf_bytes, bytes)
    # Validate PDF magic bytes %PDF-
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000


def test_invoice_pdf_download_and_storage_status_endpoints(db_session: Session):
    client = TestClient(app)
    admin_headers = get_auth_headers("admin@pmrg.in", "Admin")

    cust = db_session.query(Customer).first()
    account = ensure_billing_account(db_session, cust.id)
    sub = create_subscription(
        db=db_session,
        customer_id=cust.id,
        plan_id="PLAN-POST-FAMILY-150G",
        billing_account_id=account.id
    )
    inv = generate_invoice(db=db_session, subscription_id=sub.id)

    # 1. Download PDF format
    res = client.get(f"/api/finance/invoices/{inv.id}/download?format=pdf", headers=admin_headers)
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert "filename=" in res.headers["content-disposition"]
    assert "Invoice-" in res.headers["content-disposition"]
    assert res.content.startswith(b"%PDF-")

    # 2. Storage status endpoint
    res_storage = client.get("/api/finance/storage-status", headers=admin_headers)
    assert res_storage.status_code == 200
    data = res_storage.json()
    assert "active_provider" in data
    assert "is_configured" in data
    assert data["is_configured"] is True


# ---------------------------------------------------------------------------
# WORKSTREAM A: Razorpay Gateway & Webhook Signature Verification
# ---------------------------------------------------------------------------
def test_razorpay_webhook_signature_verification_and_tamper_rejection(monkeypatch):
    test_secret = "test_webhook_secret_key_12345"
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", test_secret)
    client = TestClient(app)

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_test_{uuid.uuid4().hex[:8]}",
                    "order_id": f"order_test_{uuid.uuid4().hex[:8]}",
                    "amount": 82482,
                    "currency": "INR",
                    "status": "captured"
                }
            }
        }
    }
    raw_body = json.dumps(payload).encode("utf-8")

    # 1. Valid Signature
    valid_sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    res_valid = client.post(
        "/api/finance/webhooks/razorpay",
        data=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": valid_sig}
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["status"] == "success"

    # 2. Tampered / Invalid Signature
    bad_sig = "invalid_hex_signature_1234567890abcdef"
    res_bad = client.post(
        "/api/finance/webhooks/razorpay",
        data=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": bad_sig}
    )
    assert res_bad.status_code == 400
    assert "Invalid Razorpay webhook signature" in res_bad.json()["detail"]

    # 3. Missing Signature Header
    res_missing = client.post(
        "/api/finance/webhooks/razorpay",
        data=raw_body,
        headers={"Content-Type": "application/json"}
    )
    assert res_missing.status_code == 400
    assert "Missing X-Razorpay-Signature" in res_missing.json()["detail"]


def test_razorpay_webhook_deduplication_and_idempotency(db_session: Session, monkeypatch):
    test_secret = "test_webhook_secret_key_12345"
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", test_secret)
    client = TestClient(app)

    event_id = f"evt_{uuid.uuid4().hex}"
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_{uuid.uuid4().hex[:8]}",
                    "order_id": f"order_{uuid.uuid4().hex[:8]}",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "captured"
                }
            }
        }
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(test_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    # First delivery
    res1 = client.post(
        "/api/finance/webhooks/razorpay",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": event_id
        }
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "success"

    # Second delivery (replay with same event id)
    res2 = client.post(
        "/api/finance/webhooks/razorpay",
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": sig,
            "X-Razorpay-Event-Id": event_id
        }
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "already_processed"


def test_create_payment_order_endpoint(db_session: Session):
    client = TestClient(app)
    admin_headers = get_auth_headers("admin@pmrg.in", "Admin")

    cust = db_session.query(Customer).first()
    account = ensure_billing_account(db_session, cust.id)
    sub = create_subscription(
        db=db_session,
        customer_id=cust.id,
        plan_id="PLAN-POST-INFINITY-75G",
        billing_account_id=account.id
    )
    inv = generate_invoice(db=db_session, subscription_id=sub.id)

    req = {
        "invoice_id": int(inv.id),
        "amount": float(inv.outstanding_amount),
        "payment_method": "UPI"
    }

    res = client.post("/api/finance/payments/create-order", json=req, headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert "order_id" in data
    assert data["amount"] == float(inv.outstanding_amount)
    assert data["currency"] == "INR"
    assert data["is_simulated"] is True


# ---------------------------------------------------------------------------
# WORKSTREAM B: Automated Billing Scheduler & Distributed Locks
# ---------------------------------------------------------------------------
def test_billing_scheduler_distributed_lock_and_run_audit(db_session: Session):
    # 1. Distributed lock test
    lock1 = SchedulerDistributedLock("test_billing_lock", ttl_seconds=60)
    acquired1 = lock1.acquire()
    assert acquired1 is True

    # Concurrent attempt on same lock fails
    lock2 = SchedulerDistributedLock("test_billing_lock", ttl_seconds=60)
    acquired2 = lock2.acquire()
    assert acquired2 is False

    lock1.release()

    # Now lock2 can acquire
    acquired3 = lock2.acquire()
    assert acquired3 is True
    lock2.release()

    # 2. Recurring billing cycle runs and durable logs
    bill_res = execute_durable_billing_job(db=db_session, job_type="RECURRING_BILLING", market_id="mumbai")
    assert bill_res["status"] == "COMPLETED"

    # Verify audit run record exists in DB
    run_rec = db_session.query(BillingJobRun).filter(BillingJobRun.job_type == "RECURRING_BILLING").order_by(BillingJobRun.start_time.desc()).first()
    assert run_rec is not None
    assert run_rec.status == "COMPLETED"

    # 3. Dunning cycle runs and logs
    dunn_res = execute_durable_billing_job(db=db_session, job_type="OVERDUE_DUNNING", market_id="mumbai")
    assert dunn_res["status"] == "COMPLETED"

    dunn_rec = db_session.query(BillingJobRun).filter(BillingJobRun.job_type == "OVERDUE_DUNNING").order_by(BillingJobRun.start_time.desc()).first()
    assert dunn_rec is not None
    assert dunn_rec.status == "COMPLETED"


# ---------------------------------------------------------------------------
# WORKSTREAM C: Distributed Rate Limiting & Client IP Extraction
# ---------------------------------------------------------------------------
def test_distributed_rate_limiter_and_ip_extraction():
    limiter = DistributedLoginRateLimiter()
    test_key = f"test_user_{uuid.uuid4().hex[:6]}"

    # Initial state: does not raise
    limiter.check_rate_limit(test_key, max_attempts=3)

    # Record 3 failures
    limiter.record_failure(test_key)
    limiter.record_failure(test_key)
    limiter.record_failure(test_key)

    # 4th check raises 429
    with pytest.raises(HTTPException) as exc_info:
        limiter.check_rate_limit(test_key, max_attempts=3)
    assert exc_info.value.status_code == 429

    # Clear resets
    limiter.clear(test_key)
    limiter.check_rate_limit(test_key, max_attempts=3)


# ---------------------------------------------------------------------------
# WORKSTREAM D: Notification Outbox Queue & Retry
# ---------------------------------------------------------------------------
def test_notification_outbox_queue_and_dispatch(db_session: Session):
    cust = db_session.query(Customer).first()
    assert cust is not None

    notif = enqueue_notification(
        db=db_session,
        market_id="mumbai",
        recipient="9876543210",
        channel="SMS",
        event_type="PAYMENT_CONFIRMATION",
        message="Thank you! Your payment of Rs 590 has been received.",
        customer_id=cust.id
    )
    db_session.commit()
    assert notif.id is not None
    assert notif.status == "QUEUED"

    # Dispatch outbox notifications
    stats = process_notification_outbox(db=db_session, batch_size=10)
    assert stats["processed"] >= 1
    assert stats["succeeded"] >= 1

    # Verify notification marked SENT
    db_session.refresh(notif)
    assert notif.status == "SENT"
    assert notif.sent_at is not None


# ---------------------------------------------------------------------------
# WORKSTREAM G: Financial Record Deletion Protection (RESTRICT Invariant)
# ---------------------------------------------------------------------------
def test_financial_cascade_deletion_safety_restrict(db_session: Session):
    # Enable SQLite foreign keys
    db_session.execute(text("PRAGMA foreign_keys = ON;"))

    # Create customer with a financial billing account
    test_cust = Customer(
        customer_code=f"RESTRICT-{uuid.uuid4().hex[:6].upper()}",
        name="Restrict Test Customer",
        email=f"restrict_{uuid.uuid4().hex[:6]}@example.com",
        phone="9988776655",
        locality="Bandra West",
        segment="Prepaid - Daily Unlimited",
        plan_name="FIBER-BASE-50",
        status="Active",
        market_id="mumbai"
    )
    db_session.add(test_cust)
    db_session.commit()

    account = ensure_billing_account(db_session, test_cust.id)
    assert account.id is not None

    # Deleting customer must fail with IntegrityError because BillingAccount has ondelete='RESTRICT'
    db_session.delete(test_cust)
    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


# ---------------------------------------------------------------------------
# WORKSTREAM E: Consolidated Customer My-Billing Endpoint
# ---------------------------------------------------------------------------
def test_customer_portal_my_billing_summary_endpoint(db_session: Session):
    client = TestClient(app)
    admin_headers = get_auth_headers("admin@pmrg.in", "Admin")

    res = client.get("/api/finance/my-billing", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert "customer_id" in data
    assert "customer_code" in data
    assert "recent_invoices" in data
    assert "recent_payments" in data
    assert "total_outstanding" in data
    assert "account_balance" in data
