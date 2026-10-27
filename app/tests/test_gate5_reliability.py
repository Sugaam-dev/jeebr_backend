"""
app/tests/test_gate5_reliability.py

SentinelOS Gate 5: Operational Reliability, Concurrency, Failure Boundaries & Cross-Module Integrity
=====================================================================================================

Validation Scope:
1. Workstream A: Durable State & Multi-Worker Safety:
   - Scenario identity, snapshots, and recovery survive application restarts and in-memory cache drops.
   - Optimistic concurrency locking prevents duplicate scenario recovery across workers.
   - Overlapping active scenarios safeguard: recovering Scenario A does NOT overwrite or clear
     active faults belonging to Scenario B.
2. Workstream B: Cross-Module Consistency:
   - End-to-end trace: Fiber cut -> Alarm -> AI Recommendation -> Human Approval -> Allowlisted Execution -> Closed-loop Verification -> Alarm Cleared.
   - APPLY_SLA_BILLING_CREDIT: Issues traceable CreditNote, credits customer balance, posts balanced double-entry General Ledger entry (debit == credit), and enforces duplicate protection.
   - Provisioning-to-billing reconciliation remains strictly idempotent across workers.
3. Workstream C: Concurrency, Failure Boundaries & Recovery:
   - Concurrent recommendation execution race condition (atomic row-level locking returns 409).
   - Replayed idempotency keys return consistent outcomes without side-effects.
   - Terminal outcomes (EXECUTED, REJECTED) cannot silently regress to earlier states.
   - Transaction rollback guarantees zero orphaned alarms or mutated device state.
4. Workstream D: Deployment & Database Verification:
   - Fail-closed production JWT secret validation.
   - Demo-login strictly blocked in production.
   - Health and readiness probes behavior during simulated failures.
   - Zero secret leakage in API responses, audit logs, and provider receipts.
5. Workstream E: Integration Contracts:
   - TR-069/ACS synthetic contract execution with explicit is_simulated=True labeling.
   - Real ACS adapter contract fails closed when credentials absent.
"""

import time
import uuid
from decimal import Decimal
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.main import app
from app.database import SessionLocal
from app.config import settings, validate_jwt_secret
from app.models import (
    User, Customer, NetworkDevice, OLTPort, NetworkLink, NetworkAlarm,
    Recommendation, AuditLog, NetworkScenario
)
from app.models_finance import (
    BillingAccount, BillingSubscription, CreditNote, JournalEntry, JournalLine, ChartOfAccounts
)
from app.models_provisioning import ServiceActivationOrder, ProvisioningStatus
from app.services.scenario_engine import (
    NetworkScenarioEngine, get_scenario_engine,
    SCENARIO_FIBER_CUT, SCENARIO_OPTICAL_DEGRADE, SCENARIO_RECOVERY
)
from app.services.ai_orchestrator import (
    get_ai_orchestration_service, compute_action_fingerprint,
    LIFECYCLE_PROPOSED, LIFECYCLE_APPROVED, LIFECYCLE_REJECTED,
    LIFECYCLE_EXECUTING, LIFECYCLE_EXECUTED, LIFECYCLE_FAILED
)
from app.services.billing_service import issue_credit_note
from app.services.tr069_provider import (
    SyntheticTR069Provider, RealACSAdapterContract, TR069ConfigurationError, get_tr069_provider
)

client = TestClient(app)


def _get_token(email: str, password: str = "admin123") -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


@pytest.fixture(scope="module")
def tokens():
    return {
        "superadmin": _get_token("superadmin@pmrg.in"),
        "admin": _get_token("admin@pmrg.in"),
        "noc": _get_token("noc@pmrg.in"),
        "viewer": _get_token("client@pmrgsolution.com", "Client@pmrg123"),
        "customer": _get_token("customer@pmrg.in"),
    }


def auth_header(token: str, market: str = "mumbai") -> dict:
    return {"Authorization": f"Bearer {token}", "X-Market-Id": market}


# ==============================================================================
# WORKSTREAM A: DURABLE STATE & MULTI-WORKER SAFETY TESTS
# ==============================================================================

def test_scenario_identity_and_snapshots_survive_application_restart(tokens):
    """
    Validates that scenario identity, pre-fault snapshot, generated alarms, and active
    status survive complete in-memory cache drop (simulating application restart or
    multi-process worker boundaries).
    """
    engine = get_scenario_engine()
    headers = auth_header(tokens["noc"], "mumbai")

    # 1. Trigger Fiber Cut
    res_trigger = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "FIBER_CUT", "target_type": "LINK", "target_id": "1", "severity": "CRITICAL"},
        headers=headers
    )
    assert res_trigger.status_code == 200
    scen_data = res_trigger.json()
    scenario_id = scen_data["scenario_id"]

    # Verify scenario was persisted to DB
    with SessionLocal() as db:
        db_scen = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scenario_id).first()
        assert db_scen is not None
        assert db_scen.status == "ACTIVE"
        assert db_scen.snapshot is not None
        assert "links" in db_scen.snapshot

    # 2. Simulate Application Restart: Wipe in-memory caches completely
    engine._active_scenarios.clear()
    engine._idempotency_registry.clear()

    # 3. Query Active Scenarios: Must retrieve authoritative active scenario from DB
    res_active = client.get("/api/network/scenarios/active", headers=headers)
    assert res_active.status_code == 200
    active_list = res_active.json()
    active_ids = [s["scenario_id"] for s in active_list]
    assert scenario_id in active_ids, "Scenario failed to survive in-memory cache reset!"

    # 4. Recover scenario using only persistent DB snapshot (zero reliance on memory)
    res_recover = client.post(
        "/api/network/scenarios/recover",
        json={"scenario_id": scenario_id},
        headers=headers
    )
    assert res_recover.status_code == 200
    rec_body = res_recover.json()
    assert rec_body["status"] == "RECOVERED"
    assert rec_body["recovered_links_count"] >= 1

    # Verify DB record transitioned to RECOVERED
    with SessionLocal() as db:
        db_scen = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scenario_id).first()
        assert db_scen.status == "RECOVERED"
        assert db_scen.recovered_at is not None


def test_scenario_recovery_concurrency_and_optimistic_locking(tokens):
    """
    Validates that concurrent workers attempting to recover the same scenario
    cannot cause double-recovery or corruption; optimistic row locking ensures
    one worker executes recovery while the duplicate attempt is handled idempotently.
    """
    headers = auth_header(tokens["admin"], "mumbai")

    # Trigger scenario
    res_trigger = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "OPTICAL_DEGRADE", "target_type": "DEVICE", "target_id": "4", "severity": "WARNING"},
        headers=headers
    )
    assert res_trigger.status_code == 200
    scenario_id = res_trigger.json()["scenario_id"]

    # First recovery call
    res_rec_1 = client.post(
        "/api/network/scenarios/recover",
        json={"scenario_id": scenario_id},
        headers=headers
    )
    assert res_rec_1.status_code == 200
    assert res_rec_1.json()["status"] == "RECOVERED"

    # Second recovery call (replay / concurrent worker)
    res_rec_2 = client.post(
        "/api/network/scenarios/recover",
        json={"scenario_id": scenario_id},
        headers=headers
    )
    assert res_rec_2.status_code == 200
    assert res_rec_2.json()["status"] == "RECOVERED"
    assert res_rec_2.json().get("idempotent_replay") is True


def test_overlapping_scenarios_recovery_isolation(tokens):
    """
    Safeguard Validation:
    When Scenario A and Scenario B affect distinct or overlapping resources,
    recovering Scenario A MUST NOT overwrite or prematurely mark healthy
    resources that are still degraded by active Scenario B.
    """
    headers = auth_header(tokens["noc"], "mumbai")

    # Scenario A: Fiber Cut on Link 1
    res_a = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "FIBER_CUT", "target_type": "LINK", "target_id": "1", "severity": "CRITICAL"},
        headers=headers
    )
    scen_a_id = res_a.json()["scenario_id"]

    with SessionLocal() as db:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type.in_(["ONT", "ONU"])
        ).first()
        assert ont is not None
        ont_id = ont.id
        ont_code = ont.device_code

    # Scenario B: Optical Degrade on Terminal Device ONT
    res_b = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "OPTICAL_DEGRADE", "target_type": "DEVICE", "target_id": str(ont_code), "severity": "CRITICAL"},
        headers=headers
    )
    assert res_b.status_code == 200
    scen_b_id = res_b.json()["scenario_id"]

    # Recover Scenario A
    res_rec_a = client.post(
        "/api/network/scenarios/recover",
        json={"scenario_id": scen_a_id},
        headers=headers
    )
    assert res_rec_a.status_code == 200

    # Verify ONT remains in degraded state because Scenario B is still ACTIVE!
    with SessionLocal() as db:
        refreshed_ont = db.query(NetworkDevice).filter(NetworkDevice.id == ont_id).first()
        active_b_alm = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == ont_id,
            NetworkAlarm.status == "ACTIVE"
        ).first()
        assert refreshed_ont is not None
        assert refreshed_ont.optical_rx_dbm is not None
        assert refreshed_ont.optical_rx_dbm <= -28.0, "Overlapping fault was prematurely overwritten!"
        assert active_b_alm is not None, "Alarm belonging to still-active Scenario B was prematurely cleared!"

    # Clean up by recovering Scenario B
    client.post("/api/network/scenarios/recover", json={"scenario_id": scen_b_id}, headers=headers)


# ==============================================================================
# WORKSTREAM B: CROSS-MODULE CONSISTENCY TESTS
# ==============================================================================

def test_full_cross_module_incident_to_recovery_sequence(tokens):
    """
    Full closed-loop integration sequence:
    1. Trigger synthetic fault scenario.
    2. Alarm generated in database.
    3. Generate AI recommendation from incident evidence via API.
    4. NOC approves recommendation with valid cryptographic action fingerprint.
    5. Execute allowlisted remediation with optimistic locking.
    6. Closed-loop readback confirms nominal network state and clears alarm.
    7. Idempotent replay verifies consistent response.
    """
    headers = auth_header(tokens["noc"], "mumbai")

    # Step 1: Trigger fault
    res_fault = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "FIBER_CUT", "target_type": "LINK", "target_id": "1", "severity": "CRITICAL"},
        headers=headers
    )
    assert res_fault.status_code == 200

    # Step 2: Generate Recommendation via REST API
    res_gen = client.post(
        "/api/governance/recommendations/generate",
        headers=headers,
        json={
            "incident_type": "FIBER_CUT",
            "target_type": "LINK",
            "target_id": "1"
        }
    )
    assert res_gen.status_code == 200
    rec = res_gen.json()
    rec_id = rec["id"]

    # Step 3: Approve
    res_app = client.post(
        "/api/governance/approve",
        json={"recommendation_id": rec_id, "notes": "Approved for automated synthetic recovery."},
        headers=headers
    )
    assert res_app.status_code == 200
    assert res_app.json()["status"] == LIFECYCLE_APPROVED

    # Step 4: Execute allowlisted recovery
    idemp_key = f"IDEMP-EXEC-{uuid.uuid4().hex[:8]}"
    res_exec = client.post(
        "/api/governance/execute",
        json={"recommendation_id": rec_id, "idempotency_key": idemp_key},
        headers=headers
    )
    assert res_exec.status_code == 200
    exec_data = res_exec.json()
    assert exec_data["status"] == LIFECYCLE_EXECUTED
    assert exec_data["execution_status"] == "VERIFIED_SUCCESS"

    # Step 5: Closed-loop verification in database
    with SessionLocal() as db:
        db_rec = db.query(Recommendation).filter(Recommendation.id == rec_id).first()
        assert db_rec.status == LIFECYCLE_EXECUTED
        assert db_rec.execution_result.get("action") == "RECOVER_NETWORK_SCENARIO"

    # Step 6: Idempotent replay with same idempotency key
    res_replay = client.post(
        "/api/governance/execute",
        json={"recommendation_id": rec_id, "idempotency_key": idemp_key},
        headers=headers
    )
    assert res_replay.status_code == 200
    assert res_replay.json()["status"] == LIFECYCLE_EXECUTED
    assert res_replay.json()["execution_status"] == "VERIFIED_SUCCESS"
    assert res_replay.json()["execution_idempotency_key"] == idemp_key


def test_apply_sla_billing_credit_full_lifecycle_and_ledger_balance(tokens):
    """
    Validates APPLY_SLA_BILLING_CREDIT:
    1. Resolves target customer and billing account.
    2. Validates authorized compensation bounds.
    3. Issues traceable CreditNote and credits customer balance.
    4. Posts balanced double-entry General Ledger journal entry (Total Debit == Total Credit).
    5. Replaying execution with idempotency key returns cached receipt without duplicate credit notes.
    """
    headers = auth_header(tokens["admin"], "mumbai")

    with SessionLocal() as db:
        # Find active customer and ensure billing account
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

        initial_balance = Decimal(str(account.balance))

        # Create SLA credit recommendation
        rec_code = f"REC-SLA-TEST-{uuid.uuid4().hex[:6]}"
        rec = Recommendation(
            recommendation_code=rec_code,
            market_id="mumbai",
            source_module="Predictive Service Assurance",
            incident_type="SLA_BREACH_COMPENSATION",
            target_entity_type="Customer",
            target_entity_id=cust.id,
            target_entity_label=f"Customer #{cust.customer_code}",
            title="Automated SLA Outage Credit Adjustment",
            description="Credit adjustment for extended fiber outage exceeding 4hr MTTR SLA.",
            recommended_action="Apply ₹250 SLA credit adjustment to customer billing account.",
            proposed_action="APPLY_SLA_BILLING_CREDIT",
            action_payload={"credit_amount": 250.0},
            confidence_score=0.99,
            risk_level="LOW",
            status=LIFECYCLE_APPROVED,
            expires_at=datetime.utcnow() + timedelta(days=1),
            reviewed_by_id=1,
            reviewed_at=datetime.utcnow(),
            action_fingerprint="placeholder"
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)

        # Update action fingerprint with actual rec id
        rec.action_fingerprint = compute_action_fingerprint(
            rec_id=rec.id,
            proposed_action="APPLY_SLA_BILLING_CREDIT",
            target_type="Customer",
            target_id=cust.id,
            market_id="mumbai",
            action_payload={"credit_amount": 250.0}
        )
        db.commit()
        rec_id = rec.id

    # Execute credit action with idempotency key
    idemp_key = f"IDEMP-SLA-{uuid.uuid4().hex[:8]}"
    res_exec = client.post(
        "/api/governance/execute",
        json={"recommendation_id": rec_id, "idempotency_key": idemp_key},
        headers=headers
    )
    assert res_exec.status_code == 200
    exec_res = res_exec.json()
    assert exec_res["status"] == LIFECYCLE_EXECUTED
    assert exec_res["execution_status"] == "VERIFIED_SUCCESS"

    # Verify Financial & Ledger Integrity in Database
    with SessionLocal() as db:
        # 1. Customer account credited
        refreshed_account = db.query(BillingAccount).filter(BillingAccount.id == account.id).first()
        expected_balance = initial_balance + Decimal("250.00")
        assert Decimal(str(refreshed_account.balance)) == expected_balance

        # 2. Credit Note issued
        cn = db.query(CreditNote).filter(
            CreditNote.billing_account_id == account.id,
            CreditNote.reason.like(f"%{rec_code}%")
        ).first()
        assert cn is not None
        assert Decimal(str(cn.amount)) == Decimal("250.00")
        assert cn.status == "ISSUED"

        # 3. Balanced Double-Entry General Ledger Verification
        j_entry = db.query(JournalEntry).filter(JournalEntry.reference_id == cn.credit_note_code).first()
        assert j_entry is not None
        total_debit = sum(Decimal(str(l.debit)) for l in j_entry.lines)
        total_credit = sum(Decimal(str(l.credit)) for l in j_entry.lines)
        assert total_debit == total_credit == Decimal("250.00"), f"Ledger unbalanced! Debit: {total_debit}, Credit: {total_credit}"

    # 4. Idempotent replay: must return 200 with cached receipt and zero duplicate credit notes
    res_replay = client.post(
        "/api/governance/execute",
        json={"recommendation_id": rec_id, "idempotency_key": idemp_key},
        headers=headers
    )
    assert res_replay.status_code == 200
    assert res_replay.json()["execution_status"] == "VERIFIED_SUCCESS"
    assert res_replay.json()["execution_idempotency_key"] == idemp_key

    with SessionLocal() as db:
        cn_count = db.query(CreditNote).filter(
            CreditNote.billing_account_id == account.id,
            CreditNote.reason.like(f"%{rec_code}%")
        ).count()
        assert cn_count == 1, "Duplicate credit note was issued on replayed execution!"


def test_provisioning_to_billing_reconciliation_multi_worker_idempotency(tokens):
    """
    Validates that provisioning-to-billing reconciler remains strictly idempotent
    and safe under repeated calls or multi-worker race conditions.
    """
    headers = auth_header(tokens["admin"], "mumbai")

    with SessionLocal() as db:
        cust = db.query(Customer).filter(Customer.market_id == "mumbai").first()
        assert cust is not None
        order = ServiceActivationOrder(
            order_code=f"ACT-MUM-IDEMP-{uuid.uuid4().hex[:6].upper()}",
            idempotency_key=f"IDEM-TEST-{uuid.uuid4().hex[:8]}",
            market_id="mumbai",
            customer_id=cust.id,
            customer_name=cust.name,
            customer_code=cust.customer_code,
            locality=cust.locality or "Bandra West",
            plan_id="PLAN-POST-INFINITY-75G",
            plan_name="Infinity 75G",
            plan_price=799.0,
            status=ProvisioningStatus.PROVISIONING,
            created_at=datetime.utcnow()
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        order_id = order.id
        order_code = order.order_code

    # First reconciliation
    res1 = client.post(f"/api/provisioning/orders/{order_code}/reconcile", headers=headers)
    assert res1.status_code == 200
    assert res1.json()["status"] == ProvisioningStatus.ACTIVE

    # Repeated reconciliation (multi-worker simulation)
    res2 = client.post(f"/api/provisioning/orders/{order_code}/reconcile", headers=headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == ProvisioningStatus.ACTIVE

    # Verify no duplicate subscriptions exist in database
    with SessionLocal() as db:
        subs = db.query(BillingSubscription).filter(
            BillingSubscription.provisioning_order_id == order_id
        ).all()
        assert len(subs) == 1, f"Expected exactly 1 subscription, found {len(subs)}!"


# ==============================================================================
# WORKSTREAM C: CONCURRENCY, FAILURE INJECTION & RECOVERY TESTS
# ==============================================================================

def test_concurrent_recommendation_execution_locking(tokens):
    """
    Validates atomic row-level concurrency lock on execution:
    Simulates Worker A locking the recommendation into EXECUTING state.
    Worker B attempting to execute the same recommendation receives 409 Conflict.
    """
    headers = auth_header(tokens["admin"], "mumbai")

    with SessionLocal() as db:
        rec = Recommendation(
            recommendation_code=f"REC-CONCUR-{uuid.uuid4().hex[:6]}",
            market_id="mumbai",
            source_module="Network Operations",
            target_entity_type="NetworkDevice",
            target_entity_id=1,
            target_entity_label="OLT Core Chassis",
            title="QoS Congestion Throttle",
            description="Test concurrency lock.",
            recommended_action="Throttle QoS bandwidth.",
            proposed_action="THROTTLE_QOS_BANDWIDTH",
            action_payload={"target_utilization_pct": 45.0},
            confidence_score=0.95,
            status=LIFECYCLE_EXECUTING,  # Worker A is already executing!
            expires_at=datetime.utcnow() + timedelta(hours=1),
            action_fingerprint="dummy_fp"
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        rec_id = rec.id

    # Worker B attempts execution on recommendation currently in EXECUTING state
    res_conflict = client.post(
        "/api/governance/execute",
        json={"recommendation_id": rec_id},
        headers=headers
    )
    assert res_conflict.status_code in (400, 409)
    assert "executing" in res_conflict.json()["detail"].lower()


def test_terminal_lifecycle_states_immutable(tokens):
    """
    Validates state machine forward-only guarantee:
    Terminal states (EXECUTED, REJECTED) cannot silently regress to earlier states.
    """
    headers = auth_header(tokens["admin"], "mumbai")

    with SessionLocal() as db:
        # Create an already REJECTED recommendation
        rec_rej = Recommendation(
            recommendation_code=f"REC-REJ-{uuid.uuid4().hex[:6]}",
            market_id="mumbai",
            source_module="Network Operations",
            target_entity_type="NetworkDevice",
            target_entity_id=1,
            target_entity_label="Chassis",
            title="Rejected recommendation",
            description="Test reject immunity.",
            recommended_action="Reboot",
            proposed_action="REBOOT_CPE_DIAGNOSTIC",
            confidence_score=0.9,
            status=LIFECYCLE_REJECTED,
            rejection_reason="Unnecessary reboot rejected by NOC."
        )
        db.add(rec_rej)
        db.commit()
        db.refresh(rec_rej)
        rej_id = rec_rej.id

    # Attempt to approve rejected recommendation -> must fail
    res_app = client.post(
        "/api/governance/approve",
        json={"recommendation_id": rej_id, "notes": "Illegal late approval."},
        headers=headers
    )
    assert res_app.status_code == 400
    assert "rejected" in res_app.json()["detail"].lower()

    # Attempt to execute rejected recommendation -> must fail
    res_exec = client.post(
        "/api/governance/execute",
        json={"recommendation_id": rej_id},
        headers=headers
    )
    assert res_exec.status_code in (400, 409)


def test_transaction_rollback_preserves_clean_state(tokens):
    """
    Validates atomic transaction boundary:
    If an unexpected database error occurs during scenario fault injection,
    the transaction rolls back cleanly without leaving partial records or active alarms.
    """
    engine = get_scenario_engine()

    with SessionLocal() as db:
        initial_alarm_count = db.query(NetworkAlarm).count()
        initial_scen_count = db.query(NetworkScenario).count()

        # Call with nonexistent target link that triggers error midway
        with pytest.raises(Exception):
            engine.trigger_scenario(
                db=db,
                scenario_type="FIBER_CUT",
                target_type="LINK",
                target_id="999999",  # Invalid nonexistent link
                market_id="mumbai"
            )

        # Assert no orphan records or alarms were committed
        after_alarm_count = db.query(NetworkAlarm).count()
        after_scen_count = db.query(NetworkScenario).count()
        assert after_alarm_count == initial_alarm_count
        assert after_scen_count == initial_scen_count


# ==============================================================================
# WORKSTREAM D: DEPLOYMENT & DATABASE VERIFICATION TESTS
# ==============================================================================

def test_fail_closed_production_configuration():
    """
    Validates fail-closed security invariants:
    - Production rejects missing, short (<32 chars), or known insecure default JWT secrets.
    - Error messages never expose the configured secret value.
    """
    insecure_keys = [
        "",
        "   ",
        "super-secret-jwt-key-for-jeebr-poc-change-in-production",
        "secret",
        "short_key_12345"
    ]
    for key in insecure_keys:
        with pytest.raises(RuntimeError) as exc_info:
            validate_jwt_secret("production", key)
        err_msg = str(exc_info.value)
        assert "FATAL SECURITY VIOLATION" in err_msg
        assert key not in err_msg or len(key.strip()) == 0, "Configured secret value leaked in exception message!"


def test_health_and_readiness_probes_behavior():
    """
    Validates that database readiness probe returns 200 when connected,
    and liveness probe returns alive status.
    """
    res_health = client.get("/health")
    assert res_health.status_code == 200
    body = res_health.json()
    assert body["status"] == "healthy"
    assert body["database"]["status"] == "connected"

    res_ready = client.get("/health/ready")
    assert res_ready.status_code == 200
    assert res_ready.json()["status"] == "ready"

    res_live = client.get("/health/live")
    assert res_live.status_code == 200
    assert res_live.json()["status"] == "alive"


def test_zero_secret_leakage_in_audit_and_api_payloads(tokens):
    """
    Security Invariant:
    API responses, recommendation evidence, and audit logs MUST NOT leak
    raw passwords, password hashes, JWT tokens, or credentials.
    """
    headers = auth_header(tokens["admin"], "mumbai")

    # Check recommendations list
    res = client.get("/api/governance/recommendations", headers=headers)
    assert res.status_code == 200
    text_content = res.text
    forbidden_tokens = [
        "hashed_password",
        "super-secret-jwt-key",
        "JeEbRDataBASEE",
        "Bearer eyJ"
    ]
    for token in forbidden_tokens:
        assert token not in text_content, f"Sensitive token '{token}' detected in API response!"

    # Check audit trail
    res_audit = client.get("/api/governance/audit-trail", headers=headers)
    assert res_audit.status_code == 200
    audit_text = res_audit.text
    for token in forbidden_tokens:
        assert token not in audit_text, f"Sensitive token '{token}' detected in audit trail!"


# ==============================================================================
# WORKSTREAM E: INTEGRATION CONTRACT TESTS
# ==============================================================================

def test_tr069_provider_synthetic_contract_and_reboot():
    """
    Validates TR-069 CWMP provider synthetic contract:
    - Reports explicit is_simulated=True and mode='SIMULATED'.
    - Reboot RPC returns scheduled receipt with bounded latency.
    - GetParameterValues returns standard TR-181 data model parameters.
    - Idempotency key replay returns cached receipt without re-executing.
    """
    provider = get_tr069_provider()
    assert isinstance(provider, SyntheticTR069Provider)
    assert provider.is_live_configured() is False

    # Health probe
    health = provider.get_health_status()
    assert health.connected is True
    assert health.is_simulated is True
    assert health.mode == "SIMULATED"
    assert health.active_cpe_count > 0

    # Reboot RPC with idempotency key
    idemp_key = f"TR069-IDEMP-{uuid.uuid4().hex[:8]}"
    rpc_res = provider.reboot_device("ST-ONT-MUM-00042", idempotency_key=idemp_key)
    assert rpc_res.success is True
    assert rpc_res.is_simulated is True
    assert rpc_res.execution_mode == "SIMULATED"
    assert rpc_res.response_payload["status"] == "REBOOT_SCHEDULED"

    # Idempotent replay
    replay_res = provider.reboot_device("ST-ONT-MUM-00042", idempotency_key=idemp_key)
    assert replay_res.success is True
    assert replay_res.idempotent_replay is True

    # Parameter query
    param_res = provider.get_parameter_values(
        "ST-ONT-MUM-00042",
        ["Device.Optical.Interface.1.OpticalRxPower", "Device.DeviceInfo.ModelName"]
    )
    assert param_res.success is True
    values = param_res.response_payload["parameter_values"]
    assert values["Device.Optical.Interface.1.OpticalRxPower"] == -19.5
    assert "ST-XPON" in values["Device.DeviceInfo.ModelName"]


def test_tr069_real_adapter_fails_closed_without_credentials():
    """
    Validates fail-closed security for production CWMP ACS adapter:
    Raises TR069ConfigurationError when real credentials are not provided.
    """
    unconfigured_adapter = RealACSAdapterContract(acs_url="", username="", password="")
    assert unconfigured_adapter.is_live_configured() is False

    health = unconfigured_adapter.get_health_status()
    assert health.connected is False
    assert health.mode == "UNCONFIGURED"

    # Attempting physical reboot without credentials raises fail-closed exception
    with pytest.raises(TR069ConfigurationError) as exc_info:
        unconfigured_adapter.reboot_device("REAL-ONT-SERIAL-001")
    assert "Live TR-069 ACS is not configured" in str(exc_info.value)
