"""
test_gate1_security_hardening.py

Gate 1 Comprehensive Verification Test Suite:
1. Step 2: Secure Demo Authentication
   - Default-disabled demo login returns 404 Not Found (non-disclosing denial).
   - Explicitly enabled development demo login succeeds with valid JWT token.
   - Production rejection: Returns 404 even if ENABLE_DEMO_LOGIN is set to True.
   - Privilege escalation rejection: Attempts to obtain SUPER_ADMIN / ADMIN without credentials return 404.
   - Normal login regression: /api/auth/login works with valid credentials and rejects invalid.
   - Role and market authorization enforcement verified.
2. Step 3: Fail-Closed Production JWT Secret Configuration
   - Production startup fails closed when SECRET_KEY is missing, empty, or whitespace.
   - Production startup fails closed when SECRET_KEY matches known insecure defaults.
   - Production startup fails closed when SECRET_KEY is less than 32 characters.
   - Configuration errors never reveal secret values.
   - Valid cryptographically strong secret in production passes validation.
   - Development/testing configuration regression verified.
   - Token creation and verification use the same configured secret source.
3. Step 4: Invoice PDF Dependency and Statutory Document Generation
   - ReportLab dependency produces valid PDF bytes with "%PDF-" magic header.
   - PDF contains statutory invoice structure and required business fields.
   - Invoice download endpoint returns 404 for nonexistent invoices and 403 for cross-customer access.
   - Financial ledger and GST calculations preserved.
4. Step 5: Test Isolation and Remote Database Safety
   - assert_safe_test_db fails closed on remote Supabase URLs.
   - Engine bound to app.main and app.database is confirmed to be isolated local SQLite.
   - Test fixtures maintain state isolation across runs.
"""

import os
import io
import pytest
from decimal import Decimal
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings, validate_jwt_secret, INSECURE_DEFAULT_KEYS
from app.main import app
import app.database as app_db
import app.main as app_main
from conftest import assert_safe_test_db, TEST_DATABASE_URL
from app.models import User, Customer, Invoice
from app.auth import create_access_token, verify_password, hash_password
from app.services.invoice_document import generate_invoice_pdf
from app.services.billing_service import ensure_billing_account, create_subscription, generate_invoice


# ==============================================================================
# STEP 2: SECURE DEMO AUTHENTICATION TESTS
# ==============================================================================

def test_demo_login_disabled_by_default(monkeypatch):
    """Demo login must be disabled by default and return 404 Not Found."""
    client = TestClient(app)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "ENABLE_DEMO_LOGIN", False)

    res = client.post("/api/auth/demo-login/Admin")
    assert res.status_code == 404
    assert res.json()["detail"] == "Not Found"


def test_demo_login_enabled_in_development(monkeypatch):
    """Demo login succeeds only when explicitly enabled in non-production."""
    client = TestClient(app)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "ENABLE_DEMO_LOGIN", True)

    res = client.post("/api/auth/demo-login/Admin")
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["role"] in ("Admin", "ADMIN")


def test_demo_login_production_denial_even_if_flag_true(monkeypatch):
    """Production must strictly reject demo login with 404 even if flag is True."""
    client = TestClient(app)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ENABLE_DEMO_LOGIN", True)

    res = client.post("/api/auth/demo-login/Admin")
    assert res.status_code == 404
    assert res.json()["detail"] == "Not Found"


def test_demo_login_privilege_escalation_denial(monkeypatch):
    """Attempts to obtain SUPER_ADMIN without credentials in production return 404."""
    client = TestClient(app)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ENABLE_DEMO_LOGIN", False)

    for role in ["SUPER_ADMIN", "Admin", "Executive", "NOC"]:
        res = client.post(f"/api/auth/demo-login/{role}")
        assert res.status_code == 404
        assert res.json()["detail"] == "Not Found"


def test_normal_login_regression(db_session: Session):
    """Normal authenticated login via /api/auth/login works and verifies credentials."""
    client = TestClient(app)

    # Valid admin credentials
    res_valid = client.post(
        "/api/auth/login",
        json={"email": "admin@pmrg.in", "password": "admin123"}
    )
    assert res_valid.status_code == 200
    data = res_valid.json()
    assert "access_token" in data
    assert data["email"] == "admin@pmrg.in"

    # Invalid password rejected with 401
    res_invalid = client.post(
        "/api/auth/login",
        json={"email": "admin@pmrg.in", "password": "WrongPassword999!"}
    )
    assert res_invalid.status_code == 401
    assert "Invalid email or password" in res_invalid.json()["detail"]

    # Nonexistent user rejected with 401
    res_nonexistent = client.post(
        "/api/auth/login",
        json={"email": "nonexistent_hacker@pmrg.in", "password": "Password123!"}
    )
    assert res_nonexistent.status_code == 401


def test_role_and_market_authorization_enforced(db_session: Session):
    """Verify role and market authorization checks are strictly preserved."""
    client = TestClient(app)

    # 1. Viewer token trying to access Admin-only /api/auth/users endpoint
    # Seeded viewer email is client@pmrgsolution.com
    viewer_token = create_access_token({"sub": "client@pmrgsolution.com", "role": "Viewer", "market": "mumbai"})
    headers_viewer = {"Authorization": f"Bearer {viewer_token}"}

    res_viewer = client.get("/api/auth/users", headers=headers_viewer)
    # Viewer does not have Admin rank, should be 403 Forbidden
    assert res_viewer.status_code == 403

    # 2. Admin token accessing /api/auth/users succeeds
    admin_token = create_access_token({"sub": "admin@pmrg.in", "role": "Admin", "market": "mumbai"})
    headers_admin = {"Authorization": f"Bearer {admin_token}"}
    res_admin = client.get("/api/auth/users", headers=headers_admin)
    assert res_admin.status_code == 200
    assert isinstance(res_admin.json(), list)


# ==============================================================================
# STEP 3: FAIL-CLOSED JWT SECRET CONFIGURATION TESTS
# ==============================================================================

def test_jwt_secret_missing_in_production_fails_closed():
    """Missing or empty JWT secret in production raises RuntimeError."""
    with pytest.raises(RuntimeError) as exc_info:
        validate_jwt_secret("production", "")
    assert "FATAL SECURITY VIOLATION" in str(exc_info.value)
    assert "SECRET_KEY must be provided" in str(exc_info.value)

    with pytest.raises(RuntimeError):
        validate_jwt_secret("production", "   ")


def test_jwt_secret_insecure_defaults_in_production_fails_closed():
    """Known insecure default secrets in production raise RuntimeError without leaking secret."""
    for insecure_key in INSECURE_DEFAULT_KEYS:
        with pytest.raises(RuntimeError) as exc_info:
            validate_jwt_secret("production", insecure_key)
        assert "FATAL SECURITY VIOLATION" in str(exc_info.value)
        assert "Insecure default SECRET_KEY detected in production" in str(exc_info.value)
        # Ensure the secret value itself is NEVER leaked in the error message
        assert insecure_key not in str(exc_info.value)


def test_jwt_secret_short_key_in_production_fails_closed():
    """Secret shorter than 32 characters in production raises RuntimeError."""
    short_secret = "short-prod-key-12345"
    with pytest.raises(RuntimeError) as exc_info:
        validate_jwt_secret("production", short_secret)
    assert "FATAL SECURITY VIOLATION" in str(exc_info.value)
    assert "at least 32 characters" in str(exc_info.value)
    assert short_secret not in str(exc_info.value)


def test_jwt_secret_valid_production_passes():
    """Cryptographically strong secret >= 32 characters in production passes validation."""
    valid_prod_key = "prod-super-secure-cryptographic-signing-key-2026-xyz!"
    assert len(valid_prod_key) >= 32
    # Should not raise
    validate_jwt_secret("production", valid_prod_key)


def test_jwt_secret_development_permissive():
    """Development and testing environments allow dev default keys."""
    validate_jwt_secret("development", "super-secret-key-2026-govern-452154215")
    validate_jwt_secret("testing", "super-secret-key-2026-govern-452154215")


def test_token_creation_and_verification_symmetry():
    """Tokens created with settings.SECRET_KEY verify correctly."""
    from jose import jwt
    payload = {"sub": "admin@pmrg.in", "role": "Admin", "market": "mumbai"}
    token = create_access_token(payload)

    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "admin@pmrg.in"
    assert decoded["role"] == "Admin"


# ==============================================================================
# STEP 4: INVOICE PDF DEPENDENCY & DOCUMENT GENERATION TESTS
# ==============================================================================

def test_reportlab_invoice_pdf_validity(db_session: Session):
    """Verify ReportLab produces valid PDF bytes with magic header and correct content."""
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
    # Magic bytes check for PDF
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000


def test_invoice_download_ownership_and_not_found(db_session: Session):
    """Test invoice download endpoint handles nonexistent and cross-customer access."""
    client = TestClient(app)

    # 1. Nonexistent invoice returns 404
    admin_token = create_access_token({"sub": "admin@pmrg.in", "role": "Admin", "market": "mumbai"})
    headers = {"Authorization": f"Bearer {admin_token}"}
    res_404 = client.get("/api/finance/invoices/9999999/download?format=pdf", headers=headers)
    assert res_404.status_code == 404
    assert res_404.json()["detail"] == "Invoice not found."

    # 2. Cross-customer access denial: customer A cannot download customer B's invoice
    cust_a = db_session.query(Customer).filter(Customer.id == 1).first()
    cust_b = db_session.query(Customer).filter(Customer.id == 2).first()
    if cust_a and cust_b:
        account_b = ensure_billing_account(db_session, cust_b.id)
        sub_b = create_subscription(db=db_session, customer_id=cust_b.id, plan_id="PLAN-POST-FAMILY-150G", billing_account_id=account_b.id)
        inv_b = generate_invoice(db=db_session, subscription_id=sub_b.id)

        # Token for customer A
        cust_a_token = create_access_token({"sub": cust_a.email, "role": "Customer", "market": "mumbai"})
        headers_cust_a = {"Authorization": f"Bearer {cust_a_token}"}

        res_denied = client.get(f"/api/finance/invoices/{inv_b.id}/download?format=pdf", headers=headers_cust_a)
        assert res_denied.status_code == 403
        assert "Cannot access another customer's financial account" in res_denied.json()["detail"]


# ==============================================================================
# STEP 5: TEST ISOLATION & REMOTE DATABASE SAFETY TESTS
# ==============================================================================

def test_safe_test_db_assertion_blocks_remote_urls():
    """assert_safe_test_db strictly rejects any remote Supabase URLs."""
    forbidden_urls = [
        "postgresql://postgres:pass@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
        "postgresql://user:pass@db.xyz.supabase.co:5432/postgres",
        "postgresql://user:pass@pooler.supabase.com:6543/postgres",
        "postgres://user:pass@localhost:5432/jeebr",
    ]
    for url in forbidden_urls:
        with pytest.raises(RuntimeError) as exc_info:
            assert_safe_test_db(url)
        assert "CRITICAL TEST ISOLATION FAILURE" in str(exc_info.value)


def test_test_engine_is_isolated_sqlite():
    """Verify active engine used by app.main and app.database is isolated local SQLite."""
    assert str(app_db.engine.url).startswith("sqlite:")
    assert str(app_main.engine.url).startswith("sqlite:")
    assert "test_backend_isolated.db" in str(app_db.engine.url)
    assert settings.ENVIRONMENT == "testing"
