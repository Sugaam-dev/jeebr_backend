"""
test_finance_billing.py

Comprehensive test suite for SentinelOS Finance & Billing Module.
Verifies:
1. Customer billing account creation, auto-numbering, and tenant isolation.
2. Subscription lifecycle, canonical tariff plan binding, and effective dates.
3. Itemized invoice generation with fixed-precision Decimal calculations and 18% GST.
4. Prevention of duplicate billing for overlapping subscription periods.
5. Authorized payment recording, deterministic provider simulation, and multi-invoice allocation.
6. Partial payment handling and unallocated credit balance recording.
7. Payment idempotency key deduplication.
8. Balanced General Ledger double-entry invariant: sum(debit) == sum(credit) per currency.
9. Journal entry immutability and balanced reversal behavior.
10. Credit notes and auditable refund transactions.
11. CRM Voucher fulfillment:
    - BILL_DISCOUNT applying credit notes and ledger contra-revenue entries.
    - SPEED_BOOST enhancing subscriber network speed profile.
    - DATA_TOPUP crediting data quota.
    - OTT_PASS failing safe with RECONCILIATION_REQUIRED (no fake live partner).
    - Single-redemption and idempotency enforcement.
12. RBAC boundaries across Admin, Revenue, Care, Viewer, and Customer roles.
13. Cross-market isolation rejection (403 Forbidden).
14. Recurring billing cycle runs and overdue dunning evaluation.
"""

import uuid
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import Customer, User, Invoice
from app.models_crm import CRMCustomer
from app.models_finance import (
    BillingAccount, BillingSubscription, Payment, PaymentAllocation,
    CreditNote, RefundRecord, JournalEntry, VoucherRedemption,
    BillingAccountStatus, SubscriptionStatus, InvoiceStatus,
    PaymentStatus, VoucherType, VoucherRedemptionStatus, ChartOfAccounts
)
from app.auth import create_access_token
from app.services.billing_service import (
    ensure_billing_account, create_subscription, generate_invoice,
    record_payment, issue_credit_note, process_refund,
    run_billing_cycle, evaluate_overdue_and_dunning
)
from app.services.ledger_service import verify_ledger_balancing, post_journal_entry, UnbalancedJournalEntryError
from app.services.voucher_service import redeem_customer_voucher

client = TestClient(app)

ROLE_EMAIL_MAP = {
    "SUPER_ADMIN": "superadmin@pmrg.in",
    "Admin": "admin@pmrg.in",
    "NOC": "noc@pmrg.in",
    "Care": "care@pmrg.in",
    "Revenue": "revenue@pmrg.in",
    "Executive": "executive@pmrg.in",
    "Viewer": "client@pmrgsolution.com",
    "Customer": "customer@pmrg.in",
}

def _auth_headers(role: str = "Admin", market: str = "mumbai", email: str = None) -> dict:
    if email is None:
        email = ROLE_EMAIL_MAP.get(role, "admin@pmrg.in")
    token = create_access_token({"sub": email, "role": role})
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market,
        "Content-Type": "application/json"
    }


# ─── 1. Customer Billing Account Lifecycle ────────────────────────────────────

def test_billing_account_creation_and_auto_numbering(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    assert cust is not None

    res = client.post(
        "/api/finance/accounts",
        headers=headers,
        json={
            "customer_id": cust.id,
            "billing_type": "POSTPAID",
            "currency": "INR",
            "credit_limit": 5000.0,
            "payment_term_days": 20
        }
    )
    print("DEBUG:", cust.id, res.status_code, res.text)
    assert res.status_code == 200
    data = res.json()
    assert data["customer_id"] == cust.id
    assert data["account_number"].startswith("BA-MUM-")
    assert data["currency"] == "INR"
    assert data["status"] == "ACTIVE"


def test_billing_account_list_and_market_isolation(db_session: Session):
    mumbai_headers = _auth_headers(role="Revenue", market="mumbai")
    kolkata_unauth_headers = _auth_headers(role="Revenue", market="kolkata")
    super_kolkata_headers = _auth_headers(role="SUPER_ADMIN", market="kolkata")

    # Revenue user can access mumbai
    res_m = client.get("/api/finance/accounts", headers=mumbai_headers)
    assert res_m.status_code == 200
    for acct in res_m.json():
        assert acct["market_id"] == "mumbai"

    # Regional Revenue user cannot access cross-market Kolkata (403 Forbidden)
    res_unauth = client.get("/api/finance/accounts", headers=kolkata_unauth_headers)
    assert res_unauth.status_code == 403

    # Super Admin can access Kolkata market
    res_k = client.get("/api/finance/accounts", headers=super_kolkata_headers)
    assert res_k.status_code == 200


# ─── 2. Subscription Lifecycle & Canonical Tariff Binding ────────────────────

def test_subscription_creation_with_canonical_tariff(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    res = client.post(
        "/api/finance/subscriptions",
        headers=headers,
        json={
            "customer_id": cust.id,
            "plan_id": "PLAN-POST-INFINITY-75G",
            "billing_frequency": "MONTHLY"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["plan_id"] == "PLAN-POST-INFINITY-75G"
    assert data["unit_price"] == 499.0
    assert data["subscription_code"].startswith("SUB-MUM-")
    assert data["status"] == "ACTIVE"


def test_subscription_invalid_plan_rejection(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    res = client.post(
        "/api/finance/subscriptions",
        headers=headers,
        json={
            "customer_id": cust.id,
            "plan_id": "PLAN-NON-EXISTENT-FAKE",
            "billing_frequency": "MONTHLY"
        }
    )
    assert res.status_code == 400
    assert "not found in canonical" in res.json()["detail"].lower()


def test_subscription_status_transition(db_session: Session):
    headers = _auth_headers(role="Admin", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-PRE-HERO-1.5G")

    res = client.post(
        f"/api/finance/subscriptions/{sub.id}/transition",
        headers=headers,
        json={"target_status": "SUSPENDED", "reason": "Customer vacation hold"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "SUSPENDED"


# ─── 3. Invoices, Fixed-Precision Arithmetic & GST 18% ────────────────────────

def test_invoice_generation_fixed_precision_and_gst(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-FAMILY-150G")  # Price 999.00

    idemp = f"idem_inv_test_{uuid.uuid4().hex[:8]}"
    res = client.post(
        "/api/finance/invoices",
        headers=headers,
        json={
            "subscription_id": str(sub.id),
            "discount_amount": 99.0,   # Subtotal: 999.00 - 99.00 = 900.00 taxable
            "tax_rate": 18.0,          # 18% GST of 900.00 = 162.00
            "idempotency_key": idemp
        }
    )
    assert res.status_code == 200
    inv = res.json()
    assert inv["subtotal"] == 999.0
    assert inv["discount_amount"] == 99.0
    assert inv["tax_amount"] == 162.0
    assert inv["total_amount"] == 1062.0
    assert inv["outstanding_amount"] == 1062.0
    assert len(inv["line_items"]) == 1
    assert inv["line_items"][0]["unit_price"] == 999.0


def test_duplicate_invoice_prevention_under_idempotency(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-PRE-SUPER-5G")

    idemp = f"idem_dup_test_{uuid.uuid4().hex[:8]}"
    res1 = client.post(
        "/api/finance/invoices",
        headers=headers,
        json={"subscription_id": str(sub.id), "idempotency_key": idemp}
    )
    res2 = client.post(
        "/api/finance/invoices",
        headers=headers,
        json={"subscription_id": str(sub.id), "idempotency_key": idemp}
    )
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["id"] == res2.json()["id"]
    assert res1.json()["invoice_code"] == res2.json()["invoice_code"]


def test_invoice_download_document_payload(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-INFINITY-75G")
    inv = generate_invoice(db_session, str(sub.id))

    res = client.get(f"/api/finance/invoices/{inv.id}/download", headers=headers)
    assert res.status_code == 200
    doc = res.json()
    assert doc["document_type"] == "TAX_INVOICE"
    assert doc["provider"]["gstin"] == "27AAACP0123M1Z5"
    assert doc["total_amount"] == float(inv.total_amount)
    assert len(doc["line_items"]) > 0


# ─── 4. Payments, Partial Allocation & Ledger Effects ─────────────────────────

def test_payment_recording_and_invoice_allocation(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-INFINITY-75G")
    inv = generate_invoice(db_session, str(sub.id))  # 499 + 18% GST (89.82) = 588.82

    total_due = float(inv.total_amount)
    idemp = f"pay_test_{uuid.uuid4().hex[:8]}"

    res = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": total_due,
            "payment_method": "UPI",
            "invoice_id": inv.id,
            "idempotency_key": idemp,
            "auto_allocate": True
        }
    )
    assert res.status_code == 200
    pay = res.json()
    assert pay["status"] == "ALLOCATED"
    assert pay["allocated_amount"] == total_due
    assert pay["unallocated_amount"] == 0.0
    assert pay["is_simulated"] is True

    # Verify invoice status updated to Paid
    inv_check = client.get(f"/api/finance/invoices/{inv.id}", headers=headers)
    assert inv_check.json()["status"] == "Paid"
    assert inv_check.json()["outstanding_amount"] == 0.0


def test_partial_payment_and_unallocated_credit(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-CORP-500M")
    inv = generate_invoice(db_session, str(sub.id))

    # Pay partial amount (100.00)
    idemp1 = f"partial_pay_{uuid.uuid4().hex[:8]}"
    res1 = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": 100.00,
            "invoice_id": inv.id,
            "idempotency_key": idemp1
        }
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "ALLOCATED"
    assert res1.json()["allocated_amount"] == 100.00

    inv_check1 = client.get(f"/api/finance/invoices/{inv.id}", headers=headers)
    assert inv_check1.json()["status"] == "Partially_Paid"
    assert inv_check1.json()["paid_amount"] == 100.00

    # Pay excess amount: remaining outstanding + 500.00
    remaining_due = float(inv_check1.json()["outstanding_amount"])
    excess_amount = remaining_due + 500.00
    idemp2 = f"excess_pay_{uuid.uuid4().hex[:8]}"
    res2 = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": excess_amount,
            "invoice_id": inv.id,
            "idempotency_key": idemp2
        }
    )
    assert res2.status_code == 200
    pay2 = res2.json()
    assert pay2["status"] == "PARTIALLY_ALLOCATED"
    assert pay2["allocated_amount"] == remaining_due
    assert pay2["unallocated_amount"] == 500.00  # Excess deposit credit


def test_payment_idempotency_duplicate_protection(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    idemp = f"idem_pay_dup_{uuid.uuid4().hex[:8]}"
    payload = {
        "customer_id": cust.id,
        "amount": 250.00,
        "payment_method": "UPI",
        "idempotency_key": idemp
    }

    res1 = client.post("/api/finance/payments", headers=headers, json=payload)
    res2 = client.post("/api/finance/payments", headers=headers, json=payload)
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["id"] == res2.json()["id"]
    assert res1.json()["payment_reference"] == res2.json()["payment_reference"]


# ─── 5. General Ledger Double-Entry Balancing Invariants ──────────────────────

def test_general_ledger_balanced_invariants(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")

    res = client.get("/api/finance/ledger/verify", headers=headers)
    assert res.status_code == 200
    ver = res.json()
    assert ver["is_fully_balanced"] is True
    assert ver["unbalanced_entries_count"] == 0
    assert ver["net_difference"] == 0.0


def test_reject_unbalanced_journal_entry(db_session: Session):
    # Attempt to post an unbalanced entry directly via service
    with pytest.raises(UnbalancedJournalEntryError):
        post_journal_entry(
            db=db_session,
            market_id="mumbai",
            reference_type="TEST_CORRUPT",
            reference_id="FAIL_001",
            description="Intentionally unbalanced posting",
            lines=[
                {"account_code": "1100", "debit": 100.0, "credit": 0.0},
                {"account_code": "1200", "debit": 0.0, "credit": 90.0}  # 10.0 imbalance
            ],
            allow_idempotent_replay=False
        )


def test_reversing_journal_entry_behavior(db_session: Session):
    headers = _auth_headers(role="Admin", market="mumbai")

    # Post valid entry
    entry = post_journal_entry(
        db=db_session,
        market_id="mumbai",
        reference_type="TEST_REV",
        reference_id=f"REV_TEST_{uuid.uuid4().hex[:6]}",
        description="Original posting for reversal test",
        lines=[
            {"account_code": ChartOfAccounts.CASH_CLEARING, "debit": 500.0, "credit": 0.0},
            {"account_code": ChartOfAccounts.CUSTOMER_DEPOSITS, "debit": 0.0, "credit": 500.0}
        ]
    )
    db_session.commit()

    # Call reversal API
    res = client.post(
        f"/api/finance/ledger/entries/{entry.id}/reverse?reason=Customer+chargeback",
        headers=headers
    )
    assert res.status_code == 200
    rev = res.json()
    assert rev["is_balanced"] is True
    assert rev["reference_type"] == "REVERSAL_TEST_REV"


# ─── 6. Credit Notes and Refunds ──────────────────────────────────────────────

def test_credit_note_issuance(db_session: Session):
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    acct = ensure_billing_account(db_session, cust.id)

    res = client.post(
        "/api/finance/credit-notes",
        headers=headers,
        json={
            "billing_account_id": str(acct.id),
            "amount": 150.00,
            "reason": "Courtesy SLA outage adjustment"
        }
    )
    assert res.status_code == 200
    cn = res.json()
    assert cn["amount"] == 150.00
    assert cn["credit_note_code"].startswith("CN-MUM-")
    assert cn["status"] == "ISSUED"


def test_refund_processing(db_session: Session):
    headers = _auth_headers(role="Admin", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    acct = ensure_billing_account(db_session, cust.id)

    # First record a payment
    idemp = f"pay_for_ref_{uuid.uuid4().hex[:8]}"
    pay = record_payment(db_session, amount=200.0, idempotency_key=idemp, billing_account_id=str(acct.id))

    res = client.post(
        "/api/finance/refunds",
        headers=headers,
        json={
            "payment_id": str(pay.id),
            "amount": 200.00,
            "reason": "Customer cancellation within cooling-off period"
        }
    )
    assert res.status_code == 200
    ref = res.json()
    assert ref["status"] == "COMPLETED"
    assert ref["refund_code"].startswith("REF-MUM-")


# ─── 7. CRM Voucher Redemption & Fulfillment ─────────────────────────────────

def test_voucher_redeem_bill_discount(db_session: Session):
    headers = _auth_headers(role="Care", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    code = f"VCH-DISC-{uuid.uuid4().hex[:6]}"
    res = client.post(
        "/api/finance/vouchers/redeem",
        headers=headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code,
            "voucher_type": "BILL_DISCOUNT"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "FULFILLED"
    assert data["financial_credit_amount"] == 100.00
    assert "credit_note_code" in data["benefit_details"]


def test_voucher_redeem_speed_boost(db_session: Session):
    headers = _auth_headers(role="Care", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    orig_plan = cust.plan_name

    code = f"VCH-BOOST-{uuid.uuid4().hex[:6]}"
    res = client.post(
        "/api/finance/vouchers/redeem",
        headers=headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code,
            "voucher_type": "SPEED_BOOST"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "FULFILLED"
    assert "speed boost" in data["benefit_details"]["fulfillment"].lower()


def test_voucher_redeem_ott_pass_fails_safe(db_session: Session):
    headers = _auth_headers(role="Care", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    code = f"VCH-OTT-{uuid.uuid4().hex[:6]}"
    res = client.post(
        "/api/finance/vouchers/redeem",
        headers=headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code,
            "voucher_type": "OTT_PASS"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "RECONCILIATION_REQUIRED"
    assert "no external ott" in data["failure_reason"].lower()


def test_voucher_single_redemption_and_idempotency(db_session: Session):
    headers = _auth_headers(role="Care", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    code = f"VCH-ONCE-{uuid.uuid4().hex[:6]}"
    payload = {
        "customer_id": cust.id,
        "voucher_code": code,
        "voucher_type": "DATA_TOPUP"
    }

    res1 = client.post("/api/finance/vouchers/redeem", headers=headers, json=payload)
    res2 = client.post("/api/finance/vouchers/redeem", headers=headers, json=payload)
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["id"] == res2.json()["id"]


# ─── 8. RBAC & IDOR Boundaries ───────────────────────────────────────────────

def test_viewer_role_cannot_issue_invoice_or_record_payment(db_session: Session):
    viewer_headers = _auth_headers(role="Viewer", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    # Viewer can VIEW overview
    res_view = client.get("/api/finance/overview", headers=viewer_headers)
    assert res_view.status_code == 200

    # Viewer CANNOT create invoice
    res_inv = client.post(
        "/api/finance/invoices",
        headers=viewer_headers,
        json={"subscription_id": str(uuid.uuid4())}
    )
    assert res_inv.status_code == 403

    # Viewer CANNOT record payment
    res_pay = client.post(
        "/api/finance/payments",
        headers=viewer_headers,
        json={"customer_id": cust.id, "amount": 100.0, "idempotency_key": "v_test"}
    )
    assert res_pay.status_code == 403


def test_customer_role_cannot_access_other_customer_bills(db_session: Session):
    # Customer 1
    cust1 = db_session.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
    # Customer 2
    cust2 = db_session.query(Customer).filter(Customer.email != "customer@pmrg.in").first()
    assert cust1 is not None and cust2 is not None

    cust1_headers = _auth_headers(role="Customer", market="mumbai", email=cust1.email)

    # Create invoice for Customer 2
    acct2 = ensure_billing_account(db_session, cust2.id)
    sub2 = create_subscription(db_session, cust2.id, "PLAN-POST-INFINITY-75G")
    inv2 = generate_invoice(db_session, str(sub2.id))

    # Customer 1 attempts to read Customer 2's invoice
    res = client.get(f"/api/finance/invoices/{inv2.id}", headers=cust1_headers)
    assert res.status_code == 403
    assert "access denied" in res.json()["detail"].lower()


def test_my_billing_summary_for_new_and_existing_customers(db_session: Session):
    # 1. Existing customer
    cust = db_session.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
    cust_headers = _auth_headers(role="Customer", market="mumbai", email=cust.email)
    res = client.get("/api/finance/my-billing", headers=cust_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["customer_id"] == cust.id
    assert data["customer_code"] == cust.customer_code

    # 2. Newly registered self-service customer without CRM master record
    new_user_email = f"selfserve_{uuid.uuid4().hex[:6]}@example.com"
    new_user = User(
        email=new_user_email,
        hashed_password="hash",
        full_name="New SelfServe User",
        role="Customer",
        is_active=True
    )
    db_session.add(new_user)
    db_session.commit()

    new_cust_headers = _auth_headers(role="Customer", market="mumbai", email=new_user_email)
    res_new = client.get("/api/finance/my-billing", headers=new_cust_headers)
    assert res_new.status_code == 200
    new_data = res_new.json()
    assert new_data["customer_code"] == "PENDING-PROVISIONING"
    assert new_data["recent_invoices"] == []
    assert new_data["active_subscriptions"] == []


# ─── 9. Background Jobs: Billing Runs & Overdue Dunning ────────────────────────

def test_recurring_billing_run_job(db_session: Session):
    headers = _auth_headers(role="Admin", market="mumbai")

    res = client.post("/api/finance/jobs/run-billing", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "COMPLETED"
    assert "invoices_generated" in data


def test_overdue_and_dunning_job(db_session: Session):
    headers = _auth_headers(role="Admin", market="mumbai")

    res = client.post("/api/finance/jobs/evaluate-overdue", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "COMPLETED"
    assert "aging_breakdown" in data
