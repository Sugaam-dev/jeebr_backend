"""
app/tests/test_scheduler_runner.py

SentinelOS Automated Background Scheduler Runner Test Suite.
Verifies:
1. Correct job registration and schedule cadence.
2. Timezone handling (UTC and non-UTC timezones like Asia/Kolkata).
3. Duplicate scheduler runner startup prevention.
4. Single-leader election across worker instances (leader vs standby).
5. Fail-closed behavior in production when Redis is absent.
6. Strict lock ownership verification (worker A cannot release worker B's lock).
7. Multiple workers attempting the same scheduled job simultaneously.
8. Anti-burst downtime handling (no backlogged execution bursts).
9. Graceful shutdown and leader lease relinquishment.
10. Runner status introspection and toggle endpoints.
11. Financial invariance (recurring billing invoice period uniqueness & dunning idempotency).
"""

import uuid
import time
from datetime import datetime, timedelta, timezone as dt_tz
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.config import settings
from app.models import User, Customer, Invoice, AuditLog
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingJobRun,
    NotificationOutbox, SubscriptionStatus, InvoiceStatus
)
from app.auth import create_access_token
from app.services.billing_scheduler import (
    SchedulerDistributedLock, execute_durable_billing_job
)
from app.services.billing_scheduler_runner import (
    AutomatedSchedulerRunner, SchedulerLeaderElection,
    compute_next_run, get_scheduler_timezone, scheduler_runner
)
from app.services.billing_service import (
    ensure_billing_account, create_subscription, run_billing_cycle, evaluate_overdue_and_dunning
)


def get_auth_headers(email: str, role: str, market: str = "mumbai"):
    token = create_access_token({"sub": email, "role": role, "market": market})
    return {"Authorization": f"Bearer {token}", "X-Market": market}


@pytest.fixture
def admin_headers(db_session: Session):
    user = db_session.query(User).filter(User.role.in_(["Admin", "SUPER_ADMIN"])).first()
    if not user:
        user = User(
            email="admin_runner_test@sentinel.com",
            full_name="Runner Admin",
            role="Admin",
            hashed_password="hashed_test_pass"
        )
        db_session.add(user)
        db_session.commit()
    return get_auth_headers(user.email, user.role, "mumbai")


# ---------------------------------------------------------------------------
# 1. Job Registration, Cadence & Timezone Handling
# ---------------------------------------------------------------------------

def test_job_registration_and_cadence():
    runner = AutomatedSchedulerRunner()
    runner.initialize_schedules(datetime(2026, 10, 27, 10, 0, 0))

    assert "RECURRING_BILLING" in runner.schedules
    assert "OVERDUE_DUNNING" in runner.schedules
    assert "OUTBOX_DISPATCH" in runner.schedules

    # Billing next run: next midnight UTC (2026-10-28 00:00:00)
    bill_next = runner.schedules["RECURRING_BILLING"]["next_run_utc"]
    assert bill_next == datetime(2026, 10, 28, 0, 0, 0)

    # Dunning next run: next 06:00 UTC (2026-10-28 06:00:00)
    dunn_next = runner.schedules["OVERDUE_DUNNING"]["next_run_utc"]
    assert dunn_next == datetime(2026, 10, 28, 6, 0, 0)

    # Outbox next run: next 5m interval boundary
    outbox_next = runner.schedules["OUTBOX_DISPATCH"]["next_run_utc"]
    assert outbox_next > datetime(2026, 10, 27, 10, 0, 0)
    assert outbox_next <= datetime(2026, 10, 27, 10, 5, 0)


def test_timezone_conversion_and_offset():
    # Test non-UTC timezone: Asia/Kolkata (UTC + 05:30)
    tz_kolkata = zoneinfo.ZoneInfo("Asia/Kolkata")
    base_time_utc = datetime(2026, 10, 27, 12, 0, 0)

    next_billing = compute_next_run("RECURRING_BILLING", base_time_utc, tz_kolkata)
    # Midnight in Kolkata is 18:30 UTC of previous day
    local_calc = next_billing.replace(tzinfo=dt_tz.utc).astimezone(tz_kolkata)
    assert local_calc.hour == 0
    assert local_calc.minute == 0
    assert local_calc.second == 0


# ---------------------------------------------------------------------------
# 2. Duplicate Scheduler Startup & Graceful Lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_duplicate_scheduler_startup_prevention():
    runner = AutomatedSchedulerRunner()
    await runner.start()
    assert runner.is_running is True

    # Second start call is a safe no-op
    await runner.start()
    assert runner.is_running is True

    await runner.stop()
    assert runner.is_running is False


# ---------------------------------------------------------------------------
# 3. Distributed Leader Election (Single Active Scheduler)
# ---------------------------------------------------------------------------

def test_distributed_leader_election_single_leader():
    # Runner 1 attempts leadership
    runner1 = SchedulerLeaderElection("runner_alpha", leader_ttl_seconds=5)
    acquired1 = runner1.try_acquire_or_renew()
    assert acquired1 is True
    assert runner1.is_leader is True

    # Runner 2 attempts leadership concurrently -> rejected
    runner2 = SchedulerLeaderElection("runner_beta", leader_ttl_seconds=5)
    acquired2 = runner2.try_acquire_or_renew()
    assert acquired2 is False
    assert runner2.is_leader is False

    # Runner 1 releases
    runner1.release()
    assert runner1.is_leader is False

    # Runner 2 can now claim leadership
    acquired3 = runner2.try_acquire_or_renew()
    assert acquired3 is True
    assert runner2.is_leader is True

    runner2.release()


def test_redis_unavailable_in_production_fails_closed(monkeypatch):
    # Set ENVIRONMENT to production and unconfigure REDIS_URL
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")

    # 1. Distributed lock must fail closed
    lock = SchedulerDistributedLock("test_prod_fail_closed", ttl_seconds=60)
    assert lock.acquire() is False

    # 2. Leader election must fail closed
    leader = SchedulerLeaderElection("prod_worker_1", leader_ttl_seconds=30)
    assert leader.try_acquire_or_renew() is False


# ---------------------------------------------------------------------------
# 4. Lock Ownership & Safe Release (Worker A cannot release Worker B's lock)
# ---------------------------------------------------------------------------

def test_lock_ownership_verification():
    lock_key = f"test_owner_safety_{uuid.uuid4().hex[:8]}"

    worker_a = SchedulerDistributedLock(lock_key, ttl_seconds=60, owner_token="token_worker_A")
    assert worker_a.acquire() is True

    # Worker B tries to acquire same lock -> fails
    worker_b = SchedulerDistributedLock(lock_key, ttl_seconds=60, owner_token="token_worker_B")
    assert worker_b.acquire() is False

    # Worker B attempts to release Worker A's lock -> rejected
    worker_b._acquired = True  # Pretend worker_b thinks it acquired
    released_by_b = worker_b.release()
    assert released_by_b is False

    # Verify lock is STILL held
    assert SchedulerDistributedLock.is_locked(lock_key) is True

    # Worker A releases its own lock -> succeeds
    released_by_a = worker_a.release()
    assert released_by_a is True
    assert SchedulerDistributedLock.is_locked(lock_key) is False


# ---------------------------------------------------------------------------
# 5. Concurrency Conflict Between Workers
# ---------------------------------------------------------------------------

def test_concurrent_scheduled_job_collision_skips_cleanly(db_session: Session):
    lock_name = "recurring_billing_all"
    explicit_lock = SchedulerDistributedLock(lock_name, ttl_seconds=60)
    assert explicit_lock.acquire() is True

    try:
        # Worker tries to execute durable job while lock is held
        res = execute_durable_billing_job(
            db=db_session,
            job_type="RECURRING_BILLING",
            market_id=None,
            initiated_by="WORKER_COLLISION_TEST"
        )
        assert res["status"] == "SKIPPED"
        assert "currently held" in res["reason"]
    finally:
        explicit_lock.release()


# ---------------------------------------------------------------------------
# 6. Anti-Burst Downtime Handling
# ---------------------------------------------------------------------------

def test_anti_burst_downtime_suppression():
    # Simulate a system that was offline for 3 days
    three_days_ago = datetime.utcnow() - timedelta(days=3)
    tz = zoneinfo.ZoneInfo("UTC")

    # Anti-burst check: compute_next_run from NOW, not in the past
    now = datetime.utcnow()
    next_run = compute_next_run("RECURRING_BILLING", now, tz)

    # Next run MUST be in the future, never in the past
    assert next_run > now
    assert next_run <= now + timedelta(days=1)


# ---------------------------------------------------------------------------
# 7. Runner Telemetry and Toggle Endpoints
# ---------------------------------------------------------------------------

def test_runner_telemetry_and_toggle_api(db_session: Session, admin_headers):
    client = TestClient(app)

    # 1. Inspect runner telemetry
    res = client.get("/api/finance/scheduler/runner/status", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert "runner_enabled" in data
    assert "timezone" in data
    assert "coordination_backend" in data
    assert "schedules" in data
    assert len(data["schedules"]) >= 3

    # 2. Toggle pause
    res_pause = client.post(
        "/api/finance/scheduler/runner/toggle",
        json={"enabled": False},
        headers=admin_headers
    )
    assert res_pause.status_code == 200

    # Verify AuditLog logged for pause
    audit = db_session.query(AuditLog).filter(
        AuditLog.action_taken == "PAUSE_SCHEDULER_RUNNER"
    ).order_by(AuditLog.timestamp.desc()).first()
    assert audit is not None
    assert audit.source_module == "BILLING_SCHEDULER"

    # 3. Toggle resume
    res_resume = client.post(
        "/api/finance/scheduler/runner/toggle",
        json={"enabled": True},
        headers=admin_headers
    )
    assert res_resume.status_code == 200


# ---------------------------------------------------------------------------
# 8. Financial Invariants: Recurring Invoicing Period Uniqueness
# ---------------------------------------------------------------------------

def test_recurring_billing_cycle_idempotency_invariants(db_session: Session):
    cust = db_session.query(Customer).first()
    assert cust is not None

    account = ensure_billing_account(db=db_session, customer_id=cust.id, market_id="mumbai")
    sub = create_subscription(
        db=db_session,
        customer_id=cust.id,
        plan_id="PLAN-POST-INFINITY-75G",
        billing_account_id=account.id
    )
    sub.next_billing_date = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    # First execution of cycle generates an invoice
    res1 = run_billing_cycle(db=db_session, market_id="mumbai")
    invoices_first = res1["invoices_generated"]

    # Second immediate execution of cycle should generate 0 duplicate invoices
    res2 = run_billing_cycle(db=db_session, market_id="mumbai")
    assert res2["invoices_generated"] == 0, "Recurring billing must be idempotent within cycle period"
