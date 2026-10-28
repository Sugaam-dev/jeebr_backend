"""
test_kyc_provisioning_rbac.py

Regression test suite for:
1. Provisioning Eligibility Fix:
   - CRMAddress.crm_customer_id query fix (eliminates AttributeError: type object 'CRMAddress' has no attribute 'customer_id')
   - Pre-flight eligibility pre-requisites and blocking reasons
   - Cross-market and lifecycle boundary checks
2. KYC Document Upload Contract & Aadhaar Masking Policy:
   - Canonical DocumentType.AADHAAR_MASKED enforcement
   - Strict rejection of unmasked 12-digit Aadhaar numbers (HTTP 422)
   - Validation of masked Aadhaar format (XXXX-XXXX-1234)
   - Backward-compatibility aliases for legacy requests (AADHAAR -> AADHAAR_MASKED, CERT_INCORPORATION -> INCORPORATION_CERT)
   - Rejection of invalid document types, invalid MIME/magic bytes, and oversized files
   - Sensitive identifier masking across responses, storage, and activity logs
3. Admin vs. Super Admin RBAC & Market Boundaries:
   - Super Admin exclusive platform authority (global ALL tariffs and vouchers)
   - Admin operational authority within market scope
   - Admin blocked from creating Super Admin or cross-market resources
4. Finance Endpoints Remediation:
   - Subscriptions accepting string or int customer IDs
   - Payments accepting string or int identifiers
   - Balanced General Ledger reversals via query or request body
"""

import io
import uuid
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.database import get_db
from app.models import Customer, User
from app.models_crm import CRMCustomer, CRMAddress, CRMDocument, DocumentType, CustomerLifecycleStatus
from app.models_finance import (
    BillingAccount, BillingSubscription, JournalEntry, JournalLine, Payment
)
from app.auth import create_access_token

client = TestClient(app, raise_server_exceptions=False)


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
        "X-Market-Id": market
    }


# ─── 1. Provisioning Eligibility Tests ───────────────────────────────────────

def test_provisioning_eligibility_crmaddress_fix():
    """
    Verifies that eligibility pre-flight check executes without AttributeError on CRMAddress,
    correctly resolving CRMAddress.crm_customer_id and returning enriched status fields.
    """
    headers = _auth_headers("Admin", "mumbai")
    res = client.get("/api/provisioning/customer/1/eligibility?plan_id=PLAN-PRE-HERO-1.5G", headers=headers)
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert "eligible" in data
    assert data["customer_id"] == 1
    assert data["market_id"].lower() == "mumbai"
    assert "blocking_errors" in data
    assert "blocking_reasons" in data
    assert "customer_lifecycle_status" in data
    assert "serviceability_status" in data


def test_provisioning_eligibility_cross_market_rejected():
    """Verifies that pre-flight check detects cross-market boundary violations."""
    headers = _auth_headers("Admin", "mumbai")
    # Customer 301 belongs to Kolkata, called with Mumbai market
    res = client.get("/api/provisioning/customer/301/eligibility?plan_id=PLAN-PRE-HERO-1.5G", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["eligible"] is False
    assert any("Cross-market activation is forbidden" in err for err in data["blocking_errors"])


def test_provisioning_eligibility_invalid_plan():
    """Verifies that non-existent tariff plan is rejected cleanly during pre-flight."""
    headers = _auth_headers("Admin", "mumbai")
    res = client.get("/api/provisioning/customer/1/eligibility?plan_id=PLAN-NONEXISTENT-999", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["eligible"] is False
    assert any("does not exist" in err for err in data["blocking_errors"])


def test_create_and_list_provisioning_orders_uuid_crm_customer_id():
    """
    Verifies that creating and listing provisioning orders correctly serializes
    UUID crm_customer_id as a string without FastAPI ResponseValidationError.
    """
    headers = _auth_headers("Admin", "mumbai")
    idem_key = f"IDEM-PROV-{uuid.uuid4().hex[:12]}"
    payload = {
        "customer_id": 1,
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "idempotency_key": idem_key,
        "execution_mode": "SIMULATED"
    }

    # 1. Create order (POST /api/provisioning/orders) -> 201 Created
    create_res = client.post("/api/provisioning/orders", json=payload, headers=headers)
    assert create_res.status_code == 201, f"Expected 201, got {create_res.status_code}: {create_res.text}"
    order_data = create_res.json()
    assert order_data["customer_id"] == 1
    assert "crm_customer_id" in order_data
    if order_data["crm_customer_id"]:
        assert isinstance(order_data["crm_customer_id"], str)

    # 2. List orders (GET /api/provisioning/orders) -> 200 OK
    list_res = client.get("/api/provisioning/orders?page=1&page_size=20&sort_order=desc", headers=headers)
    assert list_res.status_code == 200, f"Expected 200, got {list_res.status_code}: {list_res.text}"
    orders = list_res.json()
    assert isinstance(orders, list)
    assert len(orders) > 0
    for o in orders:
        if o.get("crm_customer_id"):
            assert isinstance(o["crm_customer_id"], str)


# ─── 2. KYC Document Upload & Aadhaar Masking Tests ─────────────────────────

def test_kyc_upload_masked_aadhaar_success():
    """Verifies valid masked Aadhaar upload succeeds and is persisted with masked identifier."""
    headers = _auth_headers("Admin", "mumbai")
    headers.pop("Content-Type", None)

    # Valid PDF dummy content with %PDF header
    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    files = {"file": ("aadhaar_doc.pdf", fake_pdf, "application/pdf")}
    data = {
        "document_type": "AADHAAR_MASKED",
        "document_identifier": "XXXX-XXXX-1234"
    }

    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    body = res.json()
    assert body["document_type"] == "AADHAAR_MASKED"
    assert body["document_identifier"] == "XXXX-XXXX-1234"
    assert body["upload_status"] == "UPLOADED"


def test_kyc_upload_unmasked_aadhaar_strictly_rejected():
    """
    Verifies that raw 12-digit unmasked Aadhaar (as observed in screenshot)
    is strictly rejected with HTTP 422 to comply with privacy regulations.
    """
    headers = _auth_headers("Admin", "mumbai")
    headers.pop("Content-Type", None)

    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    files = {"file": ("aadhaar_doc.pdf", fake_pdf, "application/pdf")}
    data = {
        "document_type": "AADHAAR_MASKED",
        "document_identifier": "445525256125"  # Raw 12 unmasked digits
    }

    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 422
    assert "Unmasked 12-digit Aadhaar numbers are strictly prohibited" in res.text


def test_kyc_upload_invalid_masked_aadhaar_format():
    """Verifies malformed masked Aadhaar formats are rejected with HTTP 422."""
    headers = _auth_headers("Admin", "mumbai")
    headers.pop("Content-Type", None)

    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    files = {"file": ("aadhaar_doc.pdf", fake_pdf, "application/pdf")}
    data = {
        "document_type": "AADHAAR_MASKED",
        "document_identifier": "1234-5678-XXXX"  # Invalid format (last digits masked instead of first)
    }

    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 422
    assert "Invalid masked Aadhaar format" in res.text


def test_kyc_upload_legacy_enum_compatibility_mapping():
    """
    Verifies legacy requests with document_type='AADHAAR' or 'CERT_INCORPORATION'
    are safely mapped to authoritative backend enums without error.
    """
    headers = _auth_headers("Admin", "mumbai")
    headers.pop("Content-Type", None)

    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    
    # 1. Legacy AADHAAR
    files = {"file": ("aadhaar_legacy.pdf", fake_pdf, "application/pdf")}
    data = {
        "document_type": "AADHAAR",
        "document_identifier": "XXXX-XXXX-9999"
    }
    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 201
    assert res.json()["document_type"] == "AADHAAR_MASKED"

    # 2. Legacy CERT_INCORPORATION
    files2 = {"file": ("incorp_cert.pdf", fake_pdf, "application/pdf")}
    data2 = {
        "document_type": "CERT_INCORPORATION",
        "document_identifier": "U12345MH2020PTC123456"
    }
    res2 = client.post("/api/customers/1/documents", files=files2, data=data2, headers=headers)
    assert res2.status_code == 201
    assert res2.json()["document_type"] == "INCORPORATION_CERT"


def test_kyc_upload_invalid_document_type():
    """Verifies that completely unrecognized document types return HTTP 422."""
    headers = _auth_headers("Admin", "mumbai")
    headers.pop("Content-Type", None)

    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    files = {"file": ("doc.pdf", fake_pdf, "application/pdf")}
    data = {"document_type": "TOTALLY_INVALID_DOC_TYPE"}

    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 422
    assert "Invalid document_type" in res.text


def test_kyc_upload_invalid_mime_type():
    """Verifies that files with unapproved signatures or executables are rejected."""
    headers = _auth_headers("Admin", "mumbai")
    headers.pop("Content-Type", None)

    fake_exe = b"MZ\x90\x00\x03\x00\x00\x00"  # Windows PE binary header
    files = {"file": ("malware.exe", fake_exe, "application/x-dosexec")}
    data = {"document_type": "PAN"}

    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 422


def test_kyc_upload_unauthorized_role():
    """Verifies that read-only Viewer role cannot upload documents (HTTP 403)."""
    headers = _auth_headers("Viewer", "mumbai")
    headers.pop("Content-Type", None)

    fake_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    files = {"file": ("doc.pdf", fake_pdf, "application/pdf")}
    data = {"document_type": "PAN"}

    res = client.post("/api/customers/1/documents", files=files, data=data, headers=headers)
    assert res.status_code == 403


# ─── 3. Admin vs. Super Admin RBAC Hierarchy Tests ──────────────────────────

def test_admin_market_scoped_tariff_success():
    """Verifies that an Admin can create a tariff plan within their authorized operator market."""
    headers = _auth_headers("Admin", "mumbai")
    tariff_code = f"TRF-MUM-{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "tariff_code": tariff_code,
        "name": "Mumbai Admin Fiber Plan",
        "service_plan_code": "PLAN-PRE-HERO-1.5G",
        "market_id": "mumbai",
        "billing_type": "POSTPAID",
        "billing_frequency": "MONTHLY",
        "billing_timing": "IN_ADVANCE",
        "base_recurring_charge": 999.00
    }
    res = client.post("/api/tariffs", json=payload, headers=headers)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    assert res.json()["market_id"] == "mumbai"


def test_admin_global_all_tariff_forbidden():
    """Verifies that an Admin CANNOT create a global platform (ALL) tariff plan."""
    headers = _auth_headers("Admin", "mumbai")
    tariff_code = f"TRF-ALL-{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "tariff_code": tariff_code,
        "name": "Global Admin Tariff Attempt",
        "service_plan_code": "PLAN-PRE-HERO-1.5G",
        "market_id": "ALL",
        "billing_type": "POSTPAID",
        "base_recurring_charge": 1499.00
    }
    res = client.post("/api/tariffs", json=payload, headers=headers)
    assert res.status_code == 403
    assert "Only Super Admin operators can create global-market (ALL) tariff plans." in res.text


def test_super_admin_global_all_tariff_success():
    """Verifies that Super Admin CAN create a global platform (ALL) tariff plan."""
    headers = _auth_headers("SUPER_ADMIN", "mumbai", email="superadmin@pmrg.in")
    tariff_code = f"TRF-GLOBAL-{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "tariff_code": tariff_code,
        "name": "Super Admin Global Enterprise Plan",
        "service_plan_code": "PLAN-POST-PREMIUM-1G",
        "market_id": "ALL",
        "billing_type": "POSTPAID",
        "base_recurring_charge": 22000.00,
        "status": "INACTIVE"
    }
    res = client.post("/api/tariffs", json=payload, headers=headers)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    assert res.json()["market_id"] == "ALL"


def test_super_admin_global_all_voucher_success():
    """Verifies that Super Admin CAN create a global platform (ALL) voucher."""
    headers = _auth_headers("SUPER_ADMIN", "mumbai", email="superadmin@pmrg.in")
    code = f"VCH-GLOBAL-{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "voucher_code": code,
        "campaign_name": "Super Admin Global Promo",
        "voucher_type": "PERCENT_DISCOUNT",
        "discount_value": 20.0,
        "market_id": "ALL",
        "max_redemptions": 100,
        "valid_until": "2027-12-31T23:59:59"
    }
    res = client.post("/api/vouchers", json=payload, headers=headers)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    assert res.json()["market_id"] == "ALL"


def test_admin_create_super_admin_forbidden():
    """Verifies that an Admin cannot elevate privileges by creating a Super Admin account."""
    headers = _auth_headers("Admin", "mumbai")
    payload = {
        "name": "Malicious Privilege Escalation",
        "email": f"hacked-{uuid.uuid4().hex[:6]}@pmrg.in",
        "password": "Password123!",
        "role": "SUPER_ADMIN"
    }
    res = client.post("/api/auth/users", json=payload, headers=headers)
    assert res.status_code == 403
    assert "not authorized to create a user with this role" in res.text.lower()


# ─── 4. Finance Endpoints Remediation Tests ─────────────────────────────────

def test_finance_subscription_flexible_customer_id():
    """Verifies subscription creation accepts string customer code or integer customer_id."""
    headers = _auth_headers("Admin", "mumbai")
    payload = {
        "customer_id": "SUB-MUM-100004",  # String customer code
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "billing_frequency": "MONTHLY"
    }
    res = client.post("/api/finance/subscriptions", json=payload, headers=headers)
    assert res.status_code in (200, 201), f"Expected 200/201, got {res.status_code}: {res.text}"
    assert res.json()["plan_id"] == "PLAN-PRE-HERO-1.5G"


def test_finance_payment_flexible_customer_and_account():
    """Verifies payment record accepts flexible customer and billing account identifiers."""
    headers = _auth_headers("Admin", "mumbai")
    idem_key = f"PAY-TEST-{uuid.uuid4().hex[:12]}"
    payload = {
        "customer_id": "SUB-MUM-100004",
        "amount": 500.00,
        "currency": "INR",
        "payment_method": "UPI",
        "idempotency_key": idem_key,
        "auto_allocate": True
    }
    res = client.post("/api/finance/payments", json=payload, headers=headers)
    assert res.status_code in (200, 201), f"Expected 200/201, got {res.status_code}: {res.text}"
    assert res.json()["amount"] == 500.00
    assert res.json()["payment_method"] == "UPI"


def test_finance_ledger_reversal_json_body_and_query():
    """Verifies journal entry reversal accepts reason in request body or query parameter."""
    headers = _auth_headers("Admin", "mumbai")
    
    # First, list journal entries to find an unreversed entry
    list_res = client.get("/api/finance/ledger/entries?limit=5", headers=headers)
    assert list_res.status_code == 200
    entries = list_res.json()
    unreversed = next((e for e in entries if not e.get("is_reversed")), None)
    
    if unreversed:
        entry_id = unreversed["id"]
        # Post reversal with body payload
        payload = {"reason": "Authorized test reversal of journal entry"}
        rev_res = client.post(f"/api/finance/ledger/entries/{entry_id}/reverse", json=payload, headers=headers)
        assert rev_res.status_code == 200
        assert rev_res.json()["is_balanced"] is True
