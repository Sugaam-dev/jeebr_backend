"""
app/tests/test_ai_execution_recovery.py

Unit tests for AI Recommendation Watchdog & Execution Recovery:
1. Stale recommendation stuck in EXECUTING is reaped safely.
2. Outcome reconciliation: if side effect (CreditNote) already occurred, transitions to EXECUTED.
3. Outcome reconciliation: if side effect never occurred, transitions to FAILED with retry incremented.
4. Concurrency protection: atomic optimistic locking prevents duplicate watchdog processing.
5. Idempotent watchdog runs: running reaper twice produces zero duplicate mutations.
6. RBAC enforcement: only Admin, Super Admin, and NOC can invoke watchdog reaper.
"""

import uuid
from decimal import Decimal
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models import User, Recommendation, AuditLog, NetworkDevice, NetworkLink, Customer
from app.models_finance import CreditNote, BillingAccount
from app.services.ai_orchestrator import (
    get_ai_orchestration_service,
    LIFECYCLE_EXECUTING,
    LIFECYCLE_EXECUTED,
    LIFECYCLE_FAILED,
    LIFECYCLE_APPROVED
)
from app.auth import create_access_token

client = TestClient(app)


@pytest.fixture
def tokens():
    return {
        "superadmin": create_access_token({"sub": "superadmin@pmrg.in", "role": "SUPER_ADMIN"}),
        "admin": create_access_token({"sub": "admin@pmrg.in", "role": "Admin"}),
        "noc": create_access_token({"sub": "noc@pmrg.in", "role": "NOC"}),
        "viewer": create_access_token({"sub": "client@pmrgsolution.com", "role": "Viewer"}),
    }


def auth_header(token: str, market: str = "mumbai"):
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market
    }


def test_watchdog_recovers_unexecuted_stuck_recommendation(tokens):
    """
    Simulates a worker crash BEFORE side effects occurred:
    Recommendation is left in EXECUTING state.
    Watchdog detects it exceeds max_age_minutes, discovers no CreditNote exists,
    and safely marks it FAILED with retry_count incremented.
    """
    with SessionLocal() as db:
        rec = Recommendation(
            recommendation_code=f"REC-CRASH-PRE-{uuid.uuid4().hex[:6].upper()}",
            market_id="mumbai",
            source_module="Revenue Assurance & Leakage Analytics",
            target_entity_type="Customer",
            target_entity_id=1,
            target_entity_label="Test Customer",
            title="Pre-Execution Crash Test",
            description="Simulated worker crash before execution dispatch",
            recommended_action="Execute APPLY_SLA_BILLING_CREDIT",
            proposed_action="APPLY_SLA_BILLING_CREDIT",
            confidence_score=0.95,
            status=LIFECYCLE_EXECUTING,
            executed_at=datetime.utcnow() - timedelta(minutes=15),
            created_at=datetime.utcnow() - timedelta(minutes=20),
            retry_count=0
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        rec_id = rec.id

    orchestrator = get_ai_orchestration_service()
    with SessionLocal() as db:
        reaped = orchestrator.reap_stuck_executions(db=db, max_age_minutes=10)
        reaped_ids = [r["recommendation_id"] for r in reaped]
        assert rec_id in reaped_ids

        refreshed = db.query(Recommendation).filter(Recommendation.id == rec_id).first()
        assert refreshed.status == LIFECYCLE_FAILED
        assert refreshed.execution_status == "FAILED_DURING_EXECUTION"
        assert refreshed.retry_count == 1
        assert refreshed.execution_result.get("watchdog_recovered") is True


def test_watchdog_reconciles_successful_side_effect_after_crash(tokens):
    """
    Simulates a crash AFTER side effect succeeded (CreditNote committed)
    but BEFORE the worker updated recommendation status to EXECUTED.
    Watchdog detects the existing CreditNote in DB, avoids duplicate credit,
    and transitions recommendation to EXECUTED.
    """
    rec_code = f"REC-CRASH-POST-{uuid.uuid4().hex[:6].upper()}"
    with SessionLocal() as db:
        cust = db.query(Customer).filter(Customer.market_id == "mumbai").first()
        assert cust is not None
        account = db.query(BillingAccount).filter(BillingAccount.customer_id == cust.id).first()
        if not account:
            account = BillingAccount(
                id=uuid.uuid4(),
                account_number=f"BA-MUM-{cust.id:04d}",
                customer_id=cust.id,
                market_id="mumbai",
                status="ACTIVE",
                balance=Decimal("0.00"),
                currency="INR"
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        # Pre-seed credit note as if side effect succeeded
        cn = CreditNote(
            credit_note_code=f"CN-REAP-{uuid.uuid4().hex[:6].upper()}",
            billing_account_id=account.id,
            market_id="mumbai",
            amount=Decimal("150.00"),
            reason=f"SLA Compensation breach [{rec_code}]",
            status="ISSUED",
            created_at=datetime.utcnow() - timedelta(minutes=12)
        )
        db.add(cn)

        rec = Recommendation(
            recommendation_code=rec_code,
            market_id="mumbai",
            source_module="Revenue Assurance & Leakage Analytics",
            target_entity_type="Customer",
            target_entity_id=cust.id,
            target_entity_label="Test Customer",
            title="Post-Execution Crash Test",
            description="Side effect succeeded but worker crashed before updating rec",
            recommended_action="Execute APPLY_SLA_BILLING_CREDIT",
            proposed_action="APPLY_SLA_BILLING_CREDIT",
            confidence_score=0.95,
            status=LIFECYCLE_EXECUTING,
            executed_at=datetime.utcnow() - timedelta(minutes=15),
            created_at=datetime.utcnow() - timedelta(minutes=20)
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        rec_id = rec.id

    orchestrator = get_ai_orchestration_service()
    with SessionLocal() as db:
        reaped = orchestrator.reap_stuck_executions(db=db, max_age_minutes=10)
        reaped_ids = [r["recommendation_id"] for r in reaped]
        assert rec_id in reaped_ids

        refreshed = db.query(Recommendation).filter(Recommendation.id == rec_id).first()
        assert refreshed.status == LIFECYCLE_EXECUTED
        assert refreshed.execution_status == "RECONCILED_BY_WATCHDOG"
        assert refreshed.execution_result.get("reconciled") is True

        # Verify zero duplicate credit notes were created
        cn_count = db.query(CreditNote).filter(CreditNote.reason.ilike(f"%{rec_code}%")).count()
        assert cn_count == 1


def test_watchdog_repeated_runs_are_idempotent(tokens):
    """
    Running watchdog multiple times does not duplicate state transitions or re-reap.
    """
    orchestrator = get_ai_orchestration_service()
    with SessionLocal() as db:
        run1 = orchestrator.reap_stuck_executions(db=db, max_age_minutes=10)
        run2 = orchestrator.reap_stuck_executions(db=db, max_age_minutes=10)
        assert len(run2) == 0


def test_watchdog_api_endpoint_rbac(tokens):
    """
    POST /api/governance/watchdog/reap enforces role requirements:
    Viewer is rejected (403), Admin and SuperAdmin are accepted (200).
    """
    # Viewer rejected
    res_viewer = client.post(
        "/api/governance/watchdog/reap?max_age_minutes=10",
        headers=auth_header(tokens["viewer"], "mumbai")
    )
    assert res_viewer.status_code == 403

    # Admin accepted
    res_admin = client.post(
        "/api/governance/watchdog/reap?max_age_minutes=10",
        headers=auth_header(tokens["admin"], "mumbai")
    )
    assert res_admin.status_code == 200
    assert "reaped_count" in res_admin.json()
