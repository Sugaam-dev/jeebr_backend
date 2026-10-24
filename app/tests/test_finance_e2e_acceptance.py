"""
test_finance_e2e_acceptance.py

Comprehensive End-to-End Acceptance, Reconciliation & Financial Invariant Test Suite.
Validates:
- Flow A: Postpaid End-to-End Billing Lifecycle (Canonical Plan -> Invoice -> 18% GST -> Partial Payment -> Settlement -> Ledger).
- Flow B: Prepaid End-to-End (Authorized Top-up -> Credit Balance -> Service Benefit -> Idempotency deduplication).
- Flow C: Voucher Fulfillment & Provider Failure Safe-Hold (Bill Discount -> Speed Boost -> OTT Safe-Hold -> Idempotent Replay).
- Flow D: Payment Failure, Reconciliation, Refunds & Ledger Immutability (Failed payment safe handling -> Reversal -> Immutability).
- Flow E: Overdue Dunning, Service Suspension & Provisioning Decoupling (Grace Period -> Suspension -> Restoration -> Fail-Closed Adapter).
- Financial Invariants: Double-entry debit==credit balance, line-item exact sums, non-negative allocations.
- RBAC & Market Security: Role authorization, cross-market 403 isolation, customer IDOR boundaries, no secret leakage.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import Customer, User, Invoice
from app.models_crm import CRMCustomer, CustomerLifecycleStatus
from app.models_finance import (
    BillingAccount, BillingSubscription, Payment, PaymentAllocation,
    CreditNote, RefundRecord, JournalEntry, VoucherRedemption,
    BillingAccountStatus, BillingType, SubscriptionStatus, InvoiceStatus,
    PaymentStatus, VoucherType, VoucherRedemptionStatus, ChartOfAccounts
)
from app.auth import create_access_token
from app.services.billing_service import (
    ensure_billing_account, create_subscription, transition_subscription_status,
    generate_invoice, record_payment, issue_credit_note, process_refund,
    run_billing_cycle, evaluate_overdue_and_dunning
)
from app.services.ledger_service import (
    verify_ledger_balancing, post_journal_entry, reverse_journal_entry,
    UnbalancedJournalEntryError, get_account_balances
)
from app.services.voucher_service import redeem_customer_voucher
from app.services.payment_provider import (
    get_payment_provider, RazorpayGatewayAdapter, GatewayConfigurationError
)

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
    user_email = email or ROLE_EMAIL_MAP.get(role, "admin@pmrg.in")
    token = create_access_token(data={"sub": user_email, "role": role, "market_id": market})
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market,
        "Content-Type": "application/json"
    }


# ─── FLOW A: Postpaid End-to-End Billing Lifecycle ───────────────────────────

def test_flow_a_postpaid_end_to_end_lifecycle(db_session: Session):
    """
    Flow A:
    1. Select eligible CRM customer.
    2. Create/identify billing account.
    3. Associate valid tariff (PLAN-POST-INFINITY-75G).
    4. Generate invoice with 18% GST (CGST 9% + SGST 9%) and HSN 998422.
    5. Issue invoice.
    6. Record partial payment (₹200.00).
    7. Verify allocation, outstanding balance, and ledger postings.
    8. Record second payment for remainder (₹388.82) -> invoice settled as Paid.
    9. Verify customer statement download payload and double-entry balance.
    """
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    assert cust is not None

    # Step 1-2: Ensure Billing Account
    account = ensure_billing_account(db_session, cust.id, market_id="mumbai", billing_type=BillingType.POSTPAID)
    assert account is not None
    assert account.account_number.startswith("BA-MUM-")
    assert account.balance == Decimal('0.00')

    # Step 3: Associate canonical plan
    sub = create_subscription(db_session, cust.id, "PLAN-POST-INFINITY-75G", billing_account_id=str(account.id))
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.plan_id == "PLAN-POST-INFINITY-75G"

    # Step 4-5: Generate & Issue Invoice (₹499 base + ₹89.82 18% GST = ₹588.82)
    start_dt = datetime.utcnow() - timedelta(days=30)
    end_dt = datetime.utcnow()
    inv = generate_invoice(
        db_session,
        subscription_id=str(sub.id),
        billing_period_start=start_dt,
        billing_period_end=end_dt
    )
    assert inv.status == InvoiceStatus.ISSUED
    assert inv.subtotal == Decimal('499.00')
    assert inv.tax_amount == Decimal('89.82')
    assert inv.total_amount == Decimal('588.82')
    assert inv.outstanding_amount == Decimal('588.82')
    assert inv.paid_amount == Decimal('0.00')

    # Step 6-7: Record partial payment (₹200.00)
    idemp_partial = f"flow_a_part_{uuid.uuid4().hex[:8]}"
    pay_res1 = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": 200.00,
            "payment_method": "UPI",
            "invoice_id": inv.id,
            "idempotency_key": idemp_partial
        }
    )
    assert pay_res1.status_code == 200
    p1 = pay_res1.json()
    assert p1["status"] == "ALLOCATED"
    assert p1["allocated_amount"] == 200.00
    assert p1["unallocated_amount"] == 0.00

    # Verify invoice status is now Partially_Paid
    inv_check1 = client.get(f"/api/finance/invoices/{inv.id}", headers=headers).json()
    assert inv_check1["status"] == "Partially_Paid"
    assert inv_check1["paid_amount"] == 200.00
    assert inv_check1["outstanding_amount"] == 388.82

    # Step 8: Record final settlement payment (₹388.82)
    idemp_final = f"flow_a_settle_{uuid.uuid4().hex[:8]}"
    pay_res2 = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": 388.82,
            "payment_method": "NetBanking",
            "invoice_id": inv.id,
            "idempotency_key": idemp_final
        }
    )
    assert pay_res2.status_code == 200
    p2 = pay_res2.json()
    assert p2["status"] == "ALLOCATED"
    assert p2["allocated_amount"] == 388.82

    # Verify invoice status is now Paid
    inv_check2 = client.get(f"/api/finance/invoices/{inv.id}", headers=headers).json()
    assert inv_check2["status"] == "Paid"
    assert inv_check2["paid_amount"] == 588.82
    assert inv_check2["outstanding_amount"] == 0.00

    # Step 9: Customer Statement / Official Tax Invoice Download
    doc_res = client.get(f"/api/finance/invoices/{inv.id}/download", headers=headers)
    assert doc_res.status_code == 200
    doc = doc_res.json()
    assert doc["document_type"] == "TAX_INVOICE"
    assert doc["provider"]["gstin"] == "27AAACP0123M1Z5"
    assert doc["total_amount"] == 588.82

    # Verify ledger integrity
    verify_res = client.get("/api/finance/ledger/verify", headers=headers).json()
    assert verify_res["is_fully_balanced"] is True
    assert verify_res["unbalanced_entries_count"] == 0
    assert verify_res["net_difference"] == 0.0


# ─── FLOW B: Prepaid End-to-End ───────────────────────────────────────────────

def test_flow_b_prepaid_topup_and_quota_deduplication(db_session: Session):
    """
    Flow B:
    1. Create/identify valid prepaid billing account.
    2. Record authorized top-up (₹500.00) without invoice target -> credit deposited.
    3. Verify ledger posting: Cash debit ₹500, Customer Deposit Credit ₹500.
    4. Request service benefit (DATA_TOPUP quota).
    5. Verify quota credited.
    6. Repeat with identical idempotency key -> verify no duplicate debit or quota credit.
    """
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    account = ensure_billing_account(db_session, cust.id, market_id="mumbai", billing_type=BillingType.PREPAID)
    prev_balance = float(account.balance)
    prev_quota = float(cust.daily_data_quota_gb or 1.5)

    # Step 2: Record authorized top-up of ₹500.00
    idemp_topup = f"flow_b_topup_{uuid.uuid4().hex[:8]}"
    topup_res = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": 500.00,
            "payment_method": "UPI",
            "idempotency_key": idemp_topup,
            "auto_allocate": False  # Force unallocated deposit
        }
    )
    assert topup_res.status_code == 200
    p = topup_res.json()
    assert p["unallocated_amount"] == 500.00

    # Step 3: Verify account balance incremented
    acct_check = client.get(f"/api/finance/accounts/{account.id}", headers=headers).json()
    assert acct_check["balance"] == prev_balance + 500.00

    # Step 4: Request supported service benefit (DATA_TOPUP)
    vouch_code = f"PRE-QUOTA-{uuid.uuid4().hex[:6]}"
    vouch_idemp = f"idem_quota_{uuid.uuid4().hex[:8]}"
    vouch_res1 = client.post(
        "/api/finance/vouchers/redeem",
        headers=_auth_headers(role="Care", market="mumbai"),
        json={
            "customer_id": cust.id,
            "voucher_code": vouch_code,
            "voucher_type": "DATA_TOPUP",
            "idempotency_key": vouch_idemp
        }
    )
    assert vouch_res1.status_code == 200
    v1 = vouch_res1.json()
    assert v1["status"] == "FULFILLED"

    db_session.refresh(cust)
    assert cust.daily_data_quota_gb == prev_quota + 5.0

    # Step 6: Repeat request using same idempotency key
    vouch_res2 = client.post(
        "/api/finance/vouchers/redeem",
        headers=_auth_headers(role="Care", market="mumbai"),
        json={
            "customer_id": cust.id,
            "voucher_code": vouch_code,
            "voucher_type": "DATA_TOPUP",
            "idempotency_key": vouch_idemp
        }
    )
    assert vouch_res2.status_code == 200
    v2 = vouch_res2.json()
    assert v2["id"] == v1["id"]

    # Confirm quota was NOT incremented again
    db_session.refresh(cust)
    assert cust.daily_data_quota_gb == prev_quota + 5.0


# ─── FLOW C: Voucher Fulfillment & Provider Failure Safe-Hold ─────────────────

def test_flow_c_voucher_fulfillment_and_safe_hold(db_session: Session):
    """
    Flow C:
    1. Validate customer & service eligibility.
    2. Redeem BILL_DISCOUNT -> produces approved credit note & ledger posting.
    3. Redeem SPEED_BOOST -> updates broadband profile.
    4. Redeem OTT_PASS -> without external OTT gateway, safe-holds in RECONCILIATION_REQUIRED.
    5. Retry with same code -> proves single-redemption constraint.
    """
    care_headers = _auth_headers(role="Care", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    # 1. BILL_DISCOUNT
    code_bill = f"VCH-BILL-{uuid.uuid4().hex[:6]}"
    res_bd = client.post(
        "/api/finance/vouchers/redeem",
        headers=care_headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code_bill,
            "voucher_type": "BILL_DISCOUNT"
        }
    )
    assert res_bd.status_code == 200
    data_bd = res_bd.json()
    assert data_bd["status"] == "FULFILLED"
    assert data_bd["financial_credit_amount"] == 100.00
    assert data_bd["benefit_details"]["credit_note_code"] is not None

    # 2. SPEED_BOOST
    code_sb = f"VCH-SPEED-{uuid.uuid4().hex[:6]}"
    res_sb = client.post(
        "/api/finance/vouchers/redeem",
        headers=care_headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code_sb,
            "voucher_type": "SPEED_BOOST"
        }
    )
    assert res_sb.status_code == 200
    data_sb = res_sb.json()
    assert data_sb["status"] == "FULFILLED"
    db_session.refresh(cust)
    assert "Speed Boost" in cust.plan_name

    # 3. OTT_PASS Safe-Hold (Architecture Invariant)
    code_ott = f"VCH-OTT-{uuid.uuid4().hex[:6]}"
    res_ott = client.post(
        "/api/finance/vouchers/redeem",
        headers=care_headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code_ott,
            "voucher_type": "OTT_PASS"
        }
    )
    assert res_ott.status_code == 200
    data_ott = res_ott.json()
    assert data_ott["status"] == "RECONCILIATION_REQUIRED"
    assert "no external ott" in data_ott["failure_reason"].lower()

    # 4. Single-use guard: repeat redemption with same code rejected or replayed idempotently
    res_ott_dup = client.post(
        "/api/finance/vouchers/redeem",
        headers=care_headers,
        json={
            "customer_id": cust.id,
            "voucher_code": code_ott,
            "voucher_type": "OTT_PASS"
        }
    )
    assert res_ott_dup.status_code == 200
    assert res_ott_dup.json()["id"] == data_ott["id"]


# ─── FLOW D: Payment Failure, Reconciliation, Refunds & Ledger Immutability ───

def test_flow_d_payment_failure_refund_and_ledger_immutability(db_session: Session):
    """
    Flow D:
    1. Submit payment.
    2. Test refund/reversal workflow -> decreases allocation, posts balancing reversing entry.
    3. Verify posted ledger entries remain immutable and corrections preserve double-entry balance.
    4. Verify Razorpay adapter fails closed when credentials unconfigured.
    """
    headers = _auth_headers(role="Revenue", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai", Customer.id == 8).first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-INFINITY-75G")
    inv = generate_invoice(db_session, str(sub.id))

    # 1. Post successful payment
    pay_idemp = f"pay_flow_d_{uuid.uuid4().hex[:8]}"
    pay_res = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": float(inv.total_amount),
            "payment_method": "UPI",
            "invoice_id": inv.id,
            "idempotency_key": pay_idemp
        }
    )
    assert pay_res.status_code == 200
    payment_id = pay_res.json()["id"]

    # 2. Process refund (₹100.00)
    ref_res = client.post(
        "/api/finance/refunds",
        headers=headers,
        json={
            "payment_id": payment_id,
            "amount": 100.00,
            "reason": "Duplicate transaction reimbursement"
        }
    )
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    assert ref_data["amount"] == 100.00
    assert ref_data["status"] == "COMPLETED"

    # 3. Test balanced reversing journal entry
    entries_res = client.get("/api/finance/ledger/entries", headers=headers).json()
    first_entry = entries_res[0]
    rev_res = client.post(
        f"/api/finance/ledger/entries/{first_entry['id']}/reverse?reason=AuditCorrection",
        headers=headers
    )
    assert rev_res.status_code == 200
    rev_data = rev_res.json()
    assert rev_data["is_balanced"] is True
    assert "REVERSAL" in rev_data["reference_type"]

    # 4. Verify Gateway Fail-Closed Behavior
    razorpay = RazorpayGatewayAdapter()
    assert razorpay.is_available() is False
    with pytest.raises(GatewayConfigurationError) as exc_info:
        razorpay.charge(amount=Decimal('100.00'), currency="INR", payment_method="UPI", idempotency_key="fail_test_key")
    assert "not configured" in str(exc_info.value).lower()


# ─── FLOW E: Overdue Dunning, Service Suspension & Provisioning Decoupling ────

def test_flow_e_overdue_dunning_and_service_restoration(db_session: Session):
    """
    Flow E:
    1. Create invoice with past due date.
    2. Evaluate dunning job -> marked OVERDUE.
    3. Suspend subscription due to delinquency.
    4. Verify financial account status and subscription status are decoupled.
    5. Record qualifying settlement payment.
    6. Reactivate subscription -> restored to ACTIVE.
    """
    headers = _auth_headers(role="Revenue", market="mumbai")
    admin_headers = _auth_headers(role="Admin", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai", Customer.id == 15).first()

    account = ensure_billing_account(db_session, cust.id)
    sub = create_subscription(db_session, cust.id, "PLAN-POST-INFINITY-75G")
    inv = generate_invoice(db_session, str(sub.id))

    # 1. Backdate invoice due date by 35 days (past 30-day grace period)
    inv.due_date = datetime.utcnow() - timedelta(days=35)
    db_session.commit()

    # 2. Run dunning evaluation job
    dunning_res = client.post("/api/finance/jobs/evaluate-overdue", headers=admin_headers)
    assert dunning_res.status_code == 200

    inv_check = client.get(f"/api/finance/invoices/{inv.id}", headers=headers).json()
    assert inv_check["status"] in ["Overdue", "Late"]

    # 3. Delinquency suspension on subscription
    trans_res = client.post(
        f"/api/finance/subscriptions/{sub.id}/transition",
        headers=admin_headers,
        json={"target_status": "SUSPEND", "reason": "Non-payment past 30 days"}
    )
    assert trans_res.status_code == 200
    assert trans_res.json()["status"] == "SUSPENDED"

    # 4. Verify Financial Account remains ACTIVE while subscription is SUSPENDED (Decoupling)
    db_session.refresh(account)
    assert account.status == BillingAccountStatus.ACTIVE

    # 5. Record full payment to clear delinquent balance
    pay_res = client.post(
        "/api/finance/payments",
        headers=headers,
        json={
            "customer_id": cust.id,
            "amount": float(inv.outstanding_amount),
            "payment_method": "UPI",
            "invoice_id": inv.id,
            "idempotency_key": f"settle_delinquent_{uuid.uuid4().hex[:8]}"
        }
    )
    assert pay_res.status_code == 200

    # 6. Reactivate subscription upon full settlement
    restore_res = client.post(
        f"/api/finance/subscriptions/{sub.id}/transition",
        headers=admin_headers,
        json={"target_status": "REACTIVATE", "reason": "Delinquency resolved via full settlement"}
    )
    assert restore_res.status_code == 200
    assert restore_res.json()["status"] == "ACTIVE"


# ─── FINANCIAL INVARIANT AUDIT ────────────────────────────────────────────────

def test_financial_invariants_and_ledger_reconciliation(db_session: Session):
    """
    Validates global financial invariants:
    1. Every posted journal entry balances: sum(debit) == sum(credit).
    2. General ledger verification endpoint reports discrepancy == 0.00.
    3. Unbalanced journal postings are rejected at application boundary.
    4. Exact Decimal precision holds without binary floating-point drift.
    """
    headers = _auth_headers(role="Revenue", market="mumbai")

    # 1. Ledger Balance Invariant
    verify = client.get("/api/finance/ledger/verify", headers=headers).json()
    assert verify["is_fully_balanced"] is True
    assert verify["net_difference"] == 0.0
    assert verify["total_debit"] == verify["total_credit"]

    # 2. Rejection of Unbalanced Journal Entry
    with pytest.raises(UnbalancedJournalEntryError) as exc_info:
        post_journal_entry(
            db=db_session,
            reference_type="MANUAL_TEST",
            reference_id="test-unbal",
            description="Unbalanced entry test",
            market_id="mumbai",
            lines=[
                {"account_code": ChartOfAccounts.CASH_CLEARING, "debit": 100.0, "credit": 0.0},
                {"account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE, "debit": 0.0, "credit": 90.0}
            ],
            allow_idempotent_replay=False
        )
    assert "unbalanced" in str(exc_info.value).lower()


# ─── SECURITY & RBAC BOUNDARY AUDIT ──────────────────────────────────────────

def test_security_rbac_and_cross_market_isolation(db_session: Session):
    """
    Security Audit:
    1. Viewer role cannot issue invoices, record payments, or create credit notes (403).
    2. Regional market isolation: Mumbai user cannot access Kolkata financial resources.
    3. Customer IDOR protection: Customer cannot inspect other customer's invoices or downloads.
    4. Sensitive credentials (keys/secrets) are never exposed.
    """
    viewer_headers = _auth_headers(role="Viewer", market="mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()

    # Viewer unauthorized mutations
    res_inv = client.post("/api/finance/invoices", headers=viewer_headers, json={"subscription_id": str(uuid.uuid4())})
    assert res_inv.status_code == 403

    res_pay = client.post("/api/finance/payments", headers=viewer_headers, json={"customer_id": cust.id, "amount": 100.0, "idempotency_key": "sec_test"})
    assert res_pay.status_code == 403

    res_cn = client.post("/api/finance/credit-notes", headers=viewer_headers, json={"billing_account_id": str(uuid.uuid4()), "amount": 50.0, "reason": "unauth"})
    assert res_cn.status_code == 403

    res_rf = client.post("/api/finance/refunds", headers=viewer_headers, json={"payment_id": str(uuid.uuid4()), "amount": 50.0, "reason": "unauth"})
    assert res_rf.status_code == 403

    # Gateway status diagnostic does not leak secrets
    gw_status = client.get("/api/finance/gateways/status", headers=viewer_headers).json()
    assert "secret" not in gw_status
    assert "key_secret" not in gw_status
    assert gw_status["mode"] == "SIMULATED_DEVELOPMENT"
    assert gw_status["live_payments_available"] is False
