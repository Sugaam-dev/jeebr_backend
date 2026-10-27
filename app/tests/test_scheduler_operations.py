"""
app/tests/test_scheduler_operations.py

SentinelOS Background Billing Scheduler & Notification Operations Test Suite.
Verifies:
1. Operational overview KPI aggregation and active lock counts.
2. Scheduled job inventory descriptors, lock status, and last run tracking.
3. BillingJobRun history querying, pagination, and status/type filtering.
4. Run detail retrieval with execution metrics, record counts, and duration.
5. Manual allowlisted execution, RBAC enforcement, and AuditLog recording.
6. Concurrency conflict handling (HTTP 409 Conflict) when a distributed lock is held.
7. Safe job reconciliation and stale lock clearing.
8. Durable notification outbox queue inspection with strict PII & OTP redaction.
9. Manual idempotent notification outbox dispatch execution.
"""

import uuid
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import User, Customer, AuditLog
from app.models_finance import BillingJobRun, NotificationOutbox
from app.auth import create_access_token
from app.services.billing_scheduler import (
    SchedulerDistributedLock, execute_durable_billing_job,
    mask_recipient, sanitize_notification_message, sanitize_error_message
)
from app.services.notification_service import enqueue_notification


def get_auth_headers(email: str, role: str, market: str = "mumbai"):
    token = create_access_token({"sub": email, "role": role, "market": market})
    return {"Authorization": f"Bearer {token}", "X-Market": market}


@pytest.fixture
def admin_headers(db_session: Session):
    user = db_session.query(User).filter(User.role.in_(["Admin", "SUPER_ADMIN"])).first()
    if not user:
        user = User(
            email="admin_sched_test@sentinel.com",
            full_name="Scheduler Admin",
            role="Admin",
            hashed_password="hashed_test_pass"
        )
        db_session.add(user)
        db_session.commit()
    return get_auth_headers(user.email, user.role, "mumbai")


@pytest.fixture
def viewer_headers(db_session: Session):
    user = db_session.query(User).filter(User.role == "Viewer").first()
    if not user:
        user = User(
            email="viewer_sched_test@sentinel.com",
            full_name="Scheduler Viewer",
            role="Viewer",
            hashed_password="hashed_test_pass"
        )
        db_session.add(user)
        db_session.commit()
    return get_auth_headers(user.email, user.role, "mumbai")


# ---------------------------------------------------------------------------
# 1. PII and Sanitization Unit Tests
# ---------------------------------------------------------------------------

def test_pii_and_otp_sanitization_helpers():
    # Recipient masking
    assert mask_recipient("9876543210") == "98******3210"
    assert mask_recipient("+919876543210") == "91******3210"
    assert mask_recipient("operator@telecom.com") == "o******r@telecom.com"
    assert mask_recipient("") == "***"

    # Message OTP/token masking
    raw_sms = "Your SentinelOS OTP is 849201 for payment confirmation."
    sanitized = sanitize_notification_message(raw_sms)
    assert "849201" not in sanitized
    assert "OTP: [REDACTED]" in sanitized

    raw_token = "Use secret token-XYZ123 to authenticate"
    sanitized_tok = sanitize_notification_message(raw_token)
    assert "XYZ123" not in sanitized_tok

    # Error message credentials masking
    raw_err = "Failed connecting postgresql://dbuser:superSecretPass123@db.supabase.com:5432/postgres"
    sanitized_err = sanitize_error_message(raw_err)
    assert "superSecretPass123" not in sanitized_err
    assert "[REDACTED_CREDENTIALS]" in sanitized_err


# ---------------------------------------------------------------------------
# 2. Scheduler Overview & Jobs Catalog Endpoints
# ---------------------------------------------------------------------------

def test_scheduler_overview_endpoint(db_session: Session, admin_headers):
    client = TestClient(app)

    # Seed a job run
    run = BillingJobRun(
        job_type="RECURRING_BILLING",
        market_id="mumbai",
        status="COMPLETED",
        start_time=datetime.utcnow() - timedelta(minutes=5),
        finish_time=datetime.utcnow() - timedelta(minutes=4),
        records_processed=10,
        records_succeeded=10
    )
    db_session.add(run)
    db_session.commit()

    res = client.get("/api/finance/scheduler/overview", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total_runs"] >= 1
    assert data["successful_runs"] >= 1
    assert "jobs" in data
    assert len(data["jobs"]) == 3
    job_types = [j["job_type"] for j in data["jobs"]]
    assert "RECURRING_BILLING" in job_types
    assert "OVERDUE_DUNNING" in job_types
    assert "OUTBOX_DISPATCH" in job_types


def test_list_scheduled_jobs_inventory(db_session: Session, admin_headers):
    client = TestClient(app)

    res = client.get("/api/finance/scheduler/jobs", headers=admin_headers)
    assert res.status_code == 200
    jobs = res.json()
    assert len(jobs) == 3
    for j in jobs:
        assert "job_type" in j
        assert "name" in j
        assert "description" in j
        assert "cadence" in j
        assert "is_locked" in j
        assert "allowed_roles" in j


# ---------------------------------------------------------------------------
# 3. Execution History & Detail Endpoints
# ---------------------------------------------------------------------------

def test_job_execution_history_pagination_and_filtering(db_session: Session, admin_headers):
    client = TestClient(app)

    # Seed unique runs
    unique_tag = uuid.uuid4().hex[:6]
    run1 = BillingJobRun(
        job_type="RECURRING_BILLING",
        market_id="mumbai",
        status="COMPLETED",
        start_time=datetime.utcnow(),
        finish_time=datetime.utcnow(),
        records_processed=5,
        summary_metadata={"tag": unique_tag}
    )
    run2 = BillingJobRun(
        job_type="OVERDUE_DUNNING",
        market_id="mumbai",
        status="FAILED",
        start_time=datetime.utcnow(),
        finish_time=datetime.utcnow(),
        error_details="Connection timeout",
        summary_metadata={"tag": unique_tag}
    )
    db_session.add_all([run1, run2])
    db_session.commit()

    # Query without filter
    res = client.get("/api/finance/scheduler/runs?page=1&page_size=10", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2

    # Filter by status=FAILED
    res_failed = client.get("/api/finance/scheduler/runs?status=FAILED", headers=admin_headers)
    assert res_failed.status_code == 200
    failed_items = res_failed.json()["items"]
    assert all(item["status"] == "FAILED" for item in failed_items)

    # Filter by job_type=RECURRING_BILLING
    res_billing = client.get("/api/finance/scheduler/runs?job_type=RECURRING_BILLING", headers=admin_headers)
    assert res_billing.status_code == 200
    billing_items = res_billing.json()["items"]
    assert all(item["job_type"] == "RECURRING_BILLING" for item in billing_items)


def test_job_execution_detail_and_404(db_session: Session, admin_headers):
    client = TestClient(app)

    run = BillingJobRun(
        job_type="RECURRING_BILLING",
        market_id="mumbai",
        status="COMPLETED",
        start_time=datetime.utcnow() - timedelta(seconds=12),
        finish_time=datetime.utcnow(),
        records_processed=42,
        records_succeeded=42,
        records_failed=0,
        summary_metadata={"total_billed": 52500.0}
    )
    db_session.add(run)
    db_session.commit()

    # Valid ID
    res = client.get(f"/api/finance/scheduler/runs/{run.id}", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == str(run.id)
    assert data["job_type"] == "RECURRING_BILLING"
    assert data["duration_seconds"] is not None
    assert data["records_processed"] == 42
    assert data["summary_metadata"]["total_billed"] == 52500.0

    # Non-existent ID returns 404
    bad_id = str(uuid.uuid4())
    res_404 = client.get(f"/api/finance/scheduler/runs/{bad_id}", headers=admin_headers)
    assert res_404.status_code == 404


# ---------------------------------------------------------------------------
# 4. Manual Execution, RBAC, and Concurrency Lock Conflict
# ---------------------------------------------------------------------------

def test_manual_job_execution_allowlist_and_rbac(db_session: Session, admin_headers, viewer_headers):
    client = TestClient(app)

    # 1. Non-allowlisted job returns 400 Bad Request
    res_bad = client.post(
        "/api/finance/scheduler/jobs/UNSUPPORTED_JOB/run",
        json={"market_id": "mumbai"},
        headers=admin_headers
    )
    assert res_bad.status_code == 400
    assert "not allowlisted" in res_bad.json()["detail"]

    # 2. Viewer role returns 403 Forbidden
    res_forbidden = client.post(
        "/api/finance/scheduler/jobs/RECURRING_BILLING/run",
        json={"market_id": "mumbai"},
        headers=viewer_headers
    )
    assert res_forbidden.status_code == 403

    # 3. Authorized Admin triggers job successfully
    res_ok = client.post(
        "/api/finance/scheduler/jobs/OUTBOX_DISPATCH/run",
        json={"market_id": "mumbai"},
        headers=admin_headers
    )
    assert res_ok.status_code == 200
    assert res_ok.json()["status"] == "COMPLETED"

    # Verify AuditLog created
    audit = db_session.query(AuditLog).filter(
        AuditLog.action_taken.like("%OUTBOX_DISPATCH%")
    ).order_by(AuditLog.timestamp.desc()).first()
    assert audit is not None
    assert audit.source_module == "BILLING_SCHEDULER"
    assert audit.decision == "EXECUTED"


def test_manual_job_concurrency_lock_conflict_409(db_session: Session, admin_headers):
    client = TestClient(app)

    # Manually hold the distributed lock for OVERDUE_DUNNING
    lock = SchedulerDistributedLock("overdue_dunning_mumbai", ttl_seconds=60)
    assert lock.acquire() is True

    try:
        # Trigger same job via API -> should return 409 Conflict
        res = client.post(
            "/api/finance/scheduler/jobs/OVERDUE_DUNNING/run",
            json={"market_id": "mumbai"},
            headers=admin_headers
        )
        assert res.status_code == 409
        assert "locked" in res.json()["detail"].lower() or "held" in res.json()["detail"].lower()
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 5. Stalled/Failed Job Reconciliation
# ---------------------------------------------------------------------------

def test_job_reconciliation_and_lock_clearing(db_session: Session, admin_headers):
    client = TestClient(app)

    # Simulate a stuck RUNNING job with active lock
    lock = SchedulerDistributedLock("recurring_billing_delhi", ttl_seconds=300)
    assert lock.acquire() is True

    stuck_run = BillingJobRun(
        job_type="RECURRING_BILLING",
        market_id="delhi",
        status="RUNNING",
        start_time=datetime.utcnow() - timedelta(hours=1),
        attempt_count=1
    )
    db_session.add(stuck_run)
    db_session.commit()

    # Reconcile the stalled run
    res = client.post(
        f"/api/finance/scheduler/runs/{stuck_run.id}/reconcile",
        json={"action": "RECONCILE", "reason": "Worker process rebooted"},
        headers=admin_headers
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "RECONCILED"
    assert data["finish_time"] is not None

    # Verify lock was forcibly cleared
    assert SchedulerDistributedLock.is_locked("recurring_billing_delhi") is False

    # Attempting to reconcile a completed/reconciled run returns 400
    res_repeat = client.post(
        f"/api/finance/scheduler/runs/{stuck_run.id}/reconcile",
        json={"action": "RECONCILE", "reason": "Repeat attempt"},
        headers=admin_headers
    )
    assert res_repeat.status_code == 400


# ---------------------------------------------------------------------------
# 6. Notification Outbox Visibility and Processing
# ---------------------------------------------------------------------------

def test_notification_outbox_listing_and_pii_redaction(db_session: Session, admin_headers):
    client = TestClient(app)

    # Enqueue a notification with sensitive OTP
    raw_recipient = "9876543210"
    raw_message = "Your verification OTP is 739102. Valid for 5 minutes."
    notif = enqueue_notification(
        db=db_session,
        market_id="mumbai",
        recipient=raw_recipient,
        channel="SMS",
        event_type="OTP",
        message=raw_message,
        idempotency_key=f"test_pii_{uuid.uuid4().hex}"
    )
    db_session.commit()

    # Query outbox
    res = client.get("/api/finance/notifications/outbox", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert "summary_by_status" in data

    # Find the enqueued item
    matched = [item for item in data["items"] if item["id"] == str(notif.id)]
    assert len(matched) == 1
    item = matched[0]

    # Verify masking
    assert item["recipient_masked"] == "98******3210"
    assert "739102" not in item["message_preview"]
    assert "OTP: [REDACTED]" in item["message_preview"]
    assert item["is_simulated"] is True


def test_notification_outbox_manual_process(db_session: Session, admin_headers):
    client = TestClient(app)

    res = client.post("/api/finance/notifications/outbox/process", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "COMPLETED"
    assert data["job_type"] == "OUTBOX_DISPATCH"
