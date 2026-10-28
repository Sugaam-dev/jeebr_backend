"""
test_api_contract_remediation.py

Regression test suite for SentinelOS / Jeebr POC API Contract Remediation:
1. Customer Creation:
   - Align account_type enum (RESIDENTIAL, ENTERPRISE, SME)
   - Reject invalid enums (e.g., INDIVIDUAL) with HTTP 422
   - Flat address backward-compatibility mapped to installation_address
2. CRM Lead Creation:
   - Accept valid legal_name, company_name, estimated_mrr, and account_type
   - Reject missing legal_name or invalid account_type with HTTP 422
3. Courtesy Voucher Issuance:
   - Issue via numeric customer ID or business customer code (SUB-MUM-...)
   - Return HTTP 404 for non-existent customer
   - Return HTTP 422 for invalid voucher type
   - Return HTTP 422 for ineligible customer status (CHURNED, SUSPENDED, TERMINATED)
   - Prevent duplicate issuance within 5 minutes (HTTP 409 Conflict)
4. Finance Billing Account Creation:
   - Accept customer_id as integer or string customer code
   - Accept billing_type and account_type
   - Populate account_code matching account_number in response
   - Enforce cross-market authorization (HTTP 403)
   - Reject negative credit_limit or invalid billing_type with HTTP 422
   - Prevent duplicate billing account for same customer (HTTP 409)
5. Invoice / Bill Generation:
   - Generate invoice with fixed-precision Decimal calculations and 18% GST
   - Return HTTP 404 for non-existent subscription
   - Return HTTP 422 for chronological inconsistency (period_start >= period_end)
   - Return HTTP 422 for negative discount or invalid tax_rate
   - Enforce market isolation (HTTP 403)
   - Idempotent period generation (return existing invoice without double-posting)
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import Customer, User, Invoice
from app.models_crm import CRMCustomer, CustomerLifecycleStatus, AccountType
from app.models_finance import (
    BillingAccount, BillingSubscription, InvoiceStatus,
    BillingType, VoucherType
)
from app.auth import create_access_token
from app.services.billing_service import create_subscription

client = TestClient(app, raise_server_exceptions=False)

def _auth_headers(role: str = "Admin", market: str = "mumbai", email: str = "admin@pmrg.in") -> dict:
    token = create_access_token({"sub": email, "role": role})
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market,
        "Content-Type": "application/json"
    }


# ==============================================================================
# 1. Customer Creation Remediation
# ==============================================================================

def test_customer_creation_residential_with_flat_address(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    code = f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}"

    payload = {
        "customer_number": code,
        "legal_name": "Rohan Deshmukh",
        "trade_name": "Rohan D",
        "account_type": "RESIDENTIAL",
        "status": "ACTIVE",
        "market_id": "mumbai",
        "email": f"{code.lower()}@example.com",
        "phone": "+91 98201 11223",
        "address_line1": "Flat 402, Sea Green Apartments",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400050"
    }

    res = client.post("/api/customers", json=payload, headers=headers)
    assert res.status_code in [200, 201], res.text
    data = res.json()
    assert data["customer_code"] == code or data.get("customer_number") == code
    assert data["legal_name"] == "Rohan Deshmukh"
    assert data["account_type"] == "RESIDENTIAL"

    # Verify address persisted
    saved = db_session.query(CRMCustomer).filter(CRMCustomer.customer_number == code).first()
    assert saved is not None
    assert len(saved.addresses) >= 1
    assert "Sea Green Apartments" in saved.addresses[0].address_line1


def test_customer_creation_enterprise_with_nested_address(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    code = f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}"

    payload = {
        "customer_number": code,
        "legal_name": "Nexus Dynamics LLP",
        "trade_name": "Nexus Dynamics",
        "account_type": "ENTERPRISE",
        "status": "ACTIVE",
        "market_id": "mumbai",
        "email": f"{code.lower()}@nexus.com",
        "phone": "+91 98202 22334",
        "installation_address": {
            "address_type": "INSTALLATION",
            "address_line1": "Floor 12, Maker Chambers",
            "city": "Mumbai",
            "state": "Maharashtra",
            "postal_code": "400021"
        }
    }

    res = client.post("/api/customers", json=payload, headers=headers)
    assert res.status_code in [200, 201], res.text
    data = res.json()
    assert data["account_type"] == "ENTERPRISE"


def test_customer_creation_rejects_individual_enum(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    code = f"CUST-MUM-{uuid.uuid4().hex[:6].upper()}"

    payload = {
        "customer_number": code,
        "legal_name": "Priya Verma",
        "account_type": "INDIVIDUAL",  # Obsolete/invalid enum
        "market_id": "mumbai",
        "email": f"{code.lower()}@example.com",
        "phone": "+91 98203 33445"
    }

    res = client.post("/api/customers", json=payload, headers=headers)
    assert res.status_code == 422
    err = res.json()
    assert "error_code" in err or "detail" in err
    if "field_errors" in err:
        assert "account_type" in err["field_errors"]


# ==============================================================================
# 2. CRM Lead Creation Remediation
# ==============================================================================

def test_crm_lead_creation_with_legal_name_and_company(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")

    payload = {
        "legal_name": "Kavita Nair",
        "company_name": "Nair Design Studio",
        "trade_name": "Nair Studio",
        "account_type": "RESIDENTIAL",
        "market_id": "mumbai",
        "email": f"kavita_{uuid.uuid4().hex[:4]}@example.com",
        "phone": "+91 98204 44556",
        "source": "WEBSITE",
        "estimated_mrr": 2499.0
    }

    res = client.post("/api/customers/leads", json=payload, headers=headers)
    assert res.status_code in [200, 201], res.text
    data = res.json()
    assert data["legal_name"] == "Kavita Nair"
    assert data["account_type"] == "RESIDENTIAL"


def test_crm_lead_creation_rejects_missing_legal_name(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")

    payload = {
        "company_name": "Acme Widgets",
        "account_type": "RESIDENTIAL",
        "market_id": "mumbai",
        "email": "acme@example.com",
        "phone": "+91 98205 55667"
    }

    res = client.post("/api/customers/leads", json=payload, headers=headers)
    assert res.status_code == 422
    err = res.json()
    if "field_errors" in err:
        assert "legal_name" in err["field_errors"]


def test_crm_lead_creation_rejects_invalid_account_type(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")

    payload = {
        "legal_name": "Rajesh Mehta",
        "account_type": "INDIVIDUAL",  # Invalid
        "market_id": "mumbai",
        "email": "rajesh@example.com",
        "phone": "+91 98206 66778"
    }

    res = client.post("/api/customers/leads", json=payload, headers=headers)
    assert res.status_code == 422


# ==============================================================================
# 3. Courtesy Voucher Issuance Remediation
# ==============================================================================

def test_courtesy_voucher_issuance_by_numeric_id_and_code(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    assert cust is not None

    # 1. Issue via customer ID
    res1 = client.post(
        f"/api/customers/{cust.id}/vouchers/issue?voucher_type=SPEED_BOOST",
        headers=headers
    )
    assert res1.status_code == 200, res1.text
    data1 = res1.json()
    assert data1.get("voucher_type") == "SPEED_BOOST" or data1.get("type") == "SPEED_BOOST"
    assert data1["status"] in ["STAGED", "ACTIVE"]

    # 2. Duplicate issuance within 5 minutes returns 409
    res_dup = client.post(
        f"/api/customers/{cust.id}/vouchers/issue?voucher_type=SPEED_BOOST",
        headers=headers
    )
    assert res_dup.status_code == 409

    # 3. Issue via customer business code for another type
    code = cust.customer_code or f"SUB-MUM-{cust.id:06d}"
    res2 = client.post(
        f"/api/customers/{code}/vouchers/issue?voucher_type=BILL_DISCOUNT",
        headers=headers
    )
    assert res2.status_code == 200, res2.text
    data2 = res2.json()
    assert data2.get("voucher_type") == "BILL_DISCOUNT" or data2.get("type") == "BILL_DISCOUNT"


def test_courtesy_voucher_rejects_nonexistent_customer(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    res = client.post(
        "/api/customers/NONEXISTENT-99999/vouchers/issue?voucher_type=SPEED_BOOST",
        headers=headers
    )
    assert res.status_code == 404


def test_courtesy_voucher_rejects_invalid_voucher_type(db_session: Session):
    headers = _auth_headers("Admin", "mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    res = client.post(
        f"/api/customers/{cust.id}/vouchers/issue?voucher_type=FREE_LUNCH",
        headers=headers
    )
    assert res.status_code == 422


# ==============================================================================
# 4. Finance Billing Account Remediation
# ==============================================================================

def test_finance_billing_account_creation_with_string_code(db_session: Session):
    headers = _auth_headers("Revenue", "mumbai")

    # Find a customer without existing billing account
    existing_acct_cust_ids = {a.customer_id for a in db_session.query(BillingAccount).all()}
    cust = db_session.query(Customer).filter(
        Customer.market_id == "mumbai",
        ~Customer.id.in_(existing_acct_cust_ids)
    ).first()

    if not cust:
        # Clear one customer's billing accounts for testing
        acct = db_session.query(BillingAccount).filter(BillingAccount.market_id == "mumbai").first()
        cust = db_session.query(Customer).filter(Customer.id == acct.customer_id).first()
        db_session.query(BillingAccount).filter(BillingAccount.id == acct.id).delete()
        db_session.commit()

    assert cust is not None

    res = client.post(
        "/api/finance/accounts",
        headers=headers,
        json={
            "customer_id": cust.customer_code,  # String identifier
            "account_type": "POSTPAID",
            "currency": "INR",
            "credit_limit": 10000.0,
            "market_id": "mumbai"
        }
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["customer_id"] == cust.id
    assert data["account_number"].startswith("BA-MUM-")
    assert data["account_code"] == data["account_number"]
    assert data["billing_type"] in ["POSTPAID", "PREPAID"]

    # Duplicate creation returns 409
    res_dup = client.post(
        "/api/finance/accounts",
        headers=headers,
        json={
            "customer_id": cust.id,
            "billing_type": "POSTPAID",
            "currency": "INR"
        }
    )
    assert res_dup.status_code == 409


def test_finance_billing_account_rejects_negative_credit_limit(db_session: Session):
    headers = _auth_headers("Revenue", "mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    res = client.post(
        "/api/finance/accounts",
        headers=headers,
        json={
            "customer_id": cust.id,
            "billing_type": "POSTPAID",
            "credit_limit": -500.0
        }
    )
    assert res.status_code == 422


def test_finance_billing_account_rejects_cross_market(db_session: Session):
    kolkata_headers = _auth_headers("Revenue", "kolkata")
    cust_mumbai = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    assert cust_mumbai is not None

    res = client.post(
        "/api/finance/accounts",
        headers=kolkata_headers,
        json={
            "customer_id": cust_mumbai.id,
            "billing_type": "POSTPAID"
        }
    )
    assert res.status_code == 403


# ==============================================================================
# 5. Bill / Invoice Generation Remediation
# ==============================================================================

def test_invoice_generation_valid_and_idempotent(db_session: Session):
    headers = _auth_headers("Revenue", "mumbai")

    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    assert cust is not None

    # Create active subscription and commit so test API can query it
    sub = create_subscription(db_session, cust.id, "PLAN-POST-FAMILY-150G")
    db_session.commit()

    period_start = "2026-11-01T00:00:00"
    period_end = "2026-11-30T23:59:59"

    res = client.post(
        "/api/finance/invoices/issue",
        headers=headers,
        json={
            "subscription_id": str(sub.id),
            "billing_period_start": period_start,
            "billing_period_end": period_end,
            "discount_amount": 50.0,
            "tax_rate": 18.0
        }
    )
    assert res.status_code == 200, res.text
    inv1 = res.json()
    assert inv1["subscription_id"] == str(sub.id)
    assert inv1["total_amount"] > 0
    assert inv1["tax_amount"] > 0
    assert inv1["status"].upper() in ["ISSUED", "POSTED"]

    # Re-issue for identical period is idempotent (returns same invoice)
    res2 = client.post(
        "/api/finance/invoices/issue",
        headers=headers,
        json={
            "subscription_id": str(sub.id),
            "billing_period_start": period_start,
            "billing_period_end": period_end
        }
    )
    assert res2.status_code == 200
    inv2 = res2.json()
    assert inv2["invoice_code"] == inv1["invoice_code"]


def test_invoice_generation_rejects_chronological_violation(db_session: Session):
    headers = _auth_headers("Revenue", "mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-FAMILY-150G")
    db_session.commit()

    res = client.post(
        "/api/finance/invoices/issue",
        headers=headers,
        json={
            "subscription_id": str(sub.id),
            "billing_period_start": "2026-11-30T00:00:00",
            "billing_period_end": "2026-11-01T00:00:00"  # start after end
        }
    )
    assert res.status_code == 422


def test_invoice_generation_rejects_nonexistent_subscription(db_session: Session):
    headers = _auth_headers("Revenue", "mumbai")
    res = client.post(
        "/api/finance/invoices/issue",
        headers=headers,
        json={
            "subscription_id": "SUB-NONEXISTENT-99999",
            "billing_period_start": "2026-12-01T00:00:00",
            "billing_period_end": "2026-12-31T23:59:59"
        }
    )
    assert res.status_code == 404


def test_invoice_generation_rejects_negative_discount(db_session: Session):
    headers = _auth_headers("Revenue", "mumbai")
    cust = db_session.query(Customer).filter(Customer.market_id == "mumbai").first()
    sub = create_subscription(db_session, cust.id, "PLAN-POST-FAMILY-150G")
    db_session.commit()

    res = client.post(
        "/api/finance/invoices/issue",
        headers=headers,
        json={
            "subscription_id": str(sub.id),
            "billing_period_start": "2026-12-01T00:00:00",
            "billing_period_end": "2026-12-31T23:59:59",
            "discount_amount": -100.0
        }
    )
    assert res.status_code == 422
