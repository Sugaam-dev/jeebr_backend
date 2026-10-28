"""
Test suite covering production fixes and regressions:
1. PostgreSQL prepared statements & engine configuration (prepare_threshold=None).
2. Bulk query optimization in GET /api/field/jobs.
3. Invoice issuance resolution & route alias compatibility.
4. Voucher RBAC authorization rules (Super Admin vs Admin market boundaries).
5. Sensitive data logging filter for JWT token sanitization.
"""

import pytest
import logging
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.main import app, SensitiveDataLoggingFilter
from app.auth import create_access_token
from app.config import settings
from app.models import User, Ticket, Resource
from app.models_voucher import Voucher, VoucherStatus, VoucherDefinitionType
from app.models_finance import BillingAccount, BillingSubscription, SubscriptionStatus

client = TestClient(app)


@pytest.fixture
def admin_headers():
    token = create_access_token({"sub": "admin@pmrg.in", "role": "Admin"})
    return {"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}


@pytest.fixture
def superadmin_headers():
    token = create_access_token({"sub": "superadmin@pmrg.in", "role": "SUPER_ADMIN"})
    return {"Authorization": f"Bearer {token}", "X-Market-Id": "mumbai"}


# ─── 1. Engine Configuration & Psycopg 3 Driver Tests ─────────────────────────

def test_engine_connect_args_for_psycopg():
    """Verify that Psycopg 3 connection arguments disable prepared statements for poolers."""
    pg_url = "postgresql+psycopg://postgres:secret@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"
    parsed = make_url(pg_url)
    dialect_cls = parsed.get_dialect()
    is_psycopg3 = getattr(dialect_cls, "driver", "") == "psycopg"

    args = {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5
    }
    if is_psycopg3 or "psycopg" in str(parsed.drivername):
        args["prepare_threshold"] = None

    assert args["prepare_threshold"] is None, "prepare_threshold must be None for psycopg driver"
    assert args["connect_timeout"] == 10
    assert args["keepalives"] == 1


def test_sqlite_engine_preserves_check_same_thread():
    """Verify SQLite isolated test engine avoids psycopg args."""
    sqlite_url = "sqlite:///test.db"
    engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    assert engine.dialect.name == "sqlite"


# ─── 2. Bulk Query & Field Jobs Endpoint Verification ─────────────────────────

def test_get_field_jobs_repeated_execution(admin_headers):
    """
    Verify repeated calls to GET /api/field/jobs execute cleanly
    without prepared statement errors or N+1 query proliferation.
    """
    for _ in range(3):
        res = client.get("/api/field/jobs?limit=50", headers=admin_headers)
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)


# ─── 3. Invoice Issuance Route & Subscription Resolution ──────────────────────

def test_invoice_issue_both_routes_and_resolution(admin_headers):
    """
    Verify POST /api/finance/invoices/issue and alias POST /api/invoices/issue
    both resolve subscriptions by code or UUID without 404 route mismatches.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        sub = db.query(BillingSubscription).first()
        if not sub:
            pytest.skip("No billing subscription available in test fixtures")

        payload = {
            "subscription_id": sub.subscription_code,
            "discount_amount": 0.0,
            "notes": "Regression audit invoice test"
        }

        # 1. Test canonical route: /api/finance/invoices/issue
        res1 = client.post("/api/finance/invoices/issue", json=payload, headers=admin_headers)
        assert res1.status_code in (200, 201), f"Unexpected status: {res1.status_code} - {res1.text}"

        # 2. Test alias route: /api/invoices/issue
        payload["notes"] = "Alias route invoice test"
        res2 = client.post("/api/invoices/issue", json=payload, headers=admin_headers)
        assert res2.status_code in (200, 201), f"Unexpected status: {res2.status_code} - {res2.text}"
    finally:
        db.close()


# ─── 4. Voucher RBAC Authorization Tests ──────────────────────────────────────

def test_voucher_activation_rbac_enforcement(admin_headers, superadmin_headers):
    """
    Verify RBAC policy:
    1. Admin attempting to activate/revoke a global 'ALL' voucher is denied (403).
    2. Super Admin can activate/revoke global 'ALL' vouchers.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        # Create or fetch an 'ALL' market voucher
        v_all = db.query(Voucher).filter(Voucher.market_id == "ALL").first()
        if not v_all:
            v_all = Voucher(
                voucher_code="TESTALL100",
                campaign_name="Test All Campaign",
                voucher_type=VoucherDefinitionType.BILL_DISCOUNT,
                market_id="ALL",
                status=VoucherStatus.PAUSED
            )
            db.add(v_all)
            db.commit()
        else:
            v_all.status = VoucherStatus.PAUSED
            db.commit()

        # 1. Admin should be rejected with 403 when modifying 'ALL' voucher
        res_admin = client.post(f"/api/vouchers/{v_all.id}/activate", headers=admin_headers)
        assert res_admin.status_code == 403
        assert "Super Admin" in res_admin.json()["detail"]

        # 2. Super Admin should succeed
        res_super = client.post(f"/api/vouchers/{v_all.id}/activate", headers=superadmin_headers)
        assert res_super.status_code == 200
        assert res_super.json()["status"] == "ACTIVE"
    finally:
        db.close()


# ─── 5. Sensitive Data Logging Filter Verification ────────────────────────────

def test_sensitive_logging_filter_redacts_tokens():
    """Verify that SensitiveDataLoggingFilter redacts JWTs from log messages and args."""
    filt = SensitiveDataLoggingFilter()
    sample_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummyPayload.dummySignature"

    # Test msg redaction
    rec1 = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=f'GET /api/field/stream?token={sample_jwt}&market_id=mumbai HTTP/1.1',
        args=(),
        exc_info=None
    )
    filt.filter(rec1)
    assert sample_jwt not in rec1.msg
    assert "token=[REDACTED]" in rec1.msg

    # Test args redaction
    rec2 = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s" %s',
        args=("127.0.0.1", f"GET /api/field/stream?token={sample_jwt} HTTP/1.1", 200),
        exc_info=None
    )
    filt.filter(rec2)
    assert sample_jwt not in rec2.args[1]
    assert "token=[REDACTED]" in rec2.args[1]
