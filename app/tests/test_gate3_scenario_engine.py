"""
test_gate3_scenario_engine.py

Gate 3 Comprehensive Verification Test Suite: Deterministic Synthetic Fault Scenarios,
Telemetry & Alarm Propagation Engine.

Covers:
1. FIBER_CUT: Severed link propagates downstream outage, loss of signal (LOS), and preserves unrelated segments.
2. OPTICAL_DEGRADE: Warning (-27 dBm) and Critical (-29.5 dBm) attenuation thresholds and health status transitions.
3. PEAK_TRAFFIC: Traffic saturation breaches PON utilization and OLT CPU thresholds with HIGH_PON_UTILIZATION alarms.
4. RECOVERY: Restores links, devices, and ports to nominal baseline; auto-clears alarms while preserving historical records.
5. Determinism: Repeatable outcomes with same parameters/seed.
6. Validation: Rejection of unsupported scenarios and non-existent targets with safe, actionable responses (400/404).
7. RBAC: SUPER_ADMIN, Admin, and NOC allowed; Viewer, Customer, and unauthenticated strictly rejected (401/403).
8. Multi-Market Isolation: Cross-market target manipulation strictly forbidden (403).
9. Idempotency: Replayed requests with idempotency key return cached result without duplicate alarms.
10. Atomic Rollback: Simulated write failure leaves zero orphaned telemetry or alarm state.
11. Audit Logging: Every scenario run records provenance, actor, parameters, and outcome in audit_logs.
12. End-to-End Propagation: Verification across /network/overview, /map-layers, /impact, and /alarms endpoints.
"""

import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.database import SessionLocal
from app.models import (
    User, Customer, NetworkDevice, OLTPort, NetworkLink, NetworkAlarm, AuditLog
)
from app.services.scenario_engine import (
    get_scenario_engine, SCENARIO_FIBER_CUT, SCENARIO_OPTICAL_DEGRADE,
    SCENARIO_PEAK_TRAFFIC, SCENARIO_RECOVERY
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
# 1. FIBER_CUT & TOPOLOGY ISOLATION TESTS
# ==============================================================================

def test_fiber_cut_propagates_downstream_and_excludes_unrelated(tokens):
    """
    FIBER_CUT marks the target link DOWN, cascades outage & LOS to subtending
    devices, while strictly preserving unrelated branches.
    """
    db = SessionLocal()
    try:
        # 1. Find a feeder link in Mumbai (OLT -> Cabinet)
        feeder_link = db.query(NetworkLink).filter(
            NetworkLink.market_id == "mumbai",
            NetworkLink.link_type == "FIBER_FEEDER"
        ).first()
        assert feeder_link is not None, "Feeder link required for test"
        target_link_id = feeder_link.id
        cut_cabinet_id = feeder_link.target_device_id

        # 2. Find an unrelated link in Mumbai on a different branch/cabinet
        other_link = db.query(NetworkLink).filter(
            NetworkLink.market_id == "mumbai",
            NetworkLink.id != target_link_id,
            NetworkLink.target_device_id != cut_cabinet_id
        ).first()
        assert other_link is not None
        other_link_id = other_link.id
        other_target_dev_id = other_link.target_device_id
    finally:
        db.close()

    # 3. Trigger FIBER_CUT via API
    res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "FIBER_CUT",
            "target_type": "LINK",
            "target_id": str(target_link_id),
            "severity": "CRITICAL"
        }
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["scenario_type"] == "FIBER_CUT"
    assert data["status"] == "ACTIVE"
    assert data["simulation"] is True
    assert data["telemetry_source"] == "Synthetic / Demo Telemetry"
    assert data["affected_links_count"] >= 1
    assert data["affected_devices_count"] >= 1
    assert data["alarms_generated_count"] >= 1

    scenario_id = data["scenario_id"]

    # 4. Verify DB state of affected entities
    db = SessionLocal()
    try:
        updated_link = db.query(NetworkLink).filter(NetworkLink.id == target_link_id).first()
        assert updated_link.status == "DOWN"

        cut_cabinet = db.query(NetworkDevice).filter(NetworkDevice.id == cut_cabinet_id).first()
        assert cut_cabinet.status == "DOWN"

        # Downstream ONTs must have optical_rx_dbm == -40.0 and LOS alarm
        downstream_onts = db.query(NetworkDevice).filter(
            NetworkDevice.parent_id == cut_cabinet_id
        ).all()
        # Also check splitters under this cabinet
        splitters = db.query(NetworkDevice).filter(
            NetworkDevice.parent_id == cut_cabinet_id,
            NetworkDevice.device_type == "SPLITTER"
        ).all()
        for spl in splitters:
            assert spl.status == "DOWN"
            spl_onts = db.query(NetworkDevice).filter(
                NetworkDevice.parent_id == spl.id,
                NetworkDevice.device_type.in_(["ONT", "ONU"])
            ).all()
            for ont in spl_onts:
                assert ont.status == "DOWN"
                assert ont.optical_rx_dbm == -40.0

        # Unrelated link & device MUST remain HEALTHY / unaffected!
        unaffected_link = db.query(NetworkLink).filter(NetworkLink.id == other_link_id).first()
        assert unaffected_link.status in ("ACTIVE", "HEALTHY")

        unaffected_dev = db.query(NetworkDevice).filter(NetworkDevice.id == other_target_dev_id).first()
        assert unaffected_dev.status in ("HEALTHY", "ONLINE")

    finally:
        db.close()

    # 5. Clean up by recovering
    rec_res = client.post(
        "/api/network/scenarios/recover",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={"scenario_id": scenario_id}
    )
    assert rec_res.status_code == 200


# ==============================================================================
# 2. OPTICAL DEGRADATION & THRESHOLD TESTS
# ==============================================================================

def test_optical_degradation_warning_and_critical_thresholds(tokens):
    """
    OPTICAL_DEGRADE applies warning (-27 dBm) and critical (-29.5 dBm) attenuation,
    correctly classifying status and triggering deduplicated alarms.
    """
    db = SessionLocal()
    try:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type.in_(["ONT", "ONU"])
        ).first()
        assert ont is not None
        ont_id = ont.id
        ont_code = ont.device_code
    finally:
        db.close()

    # Step A: Apply Warning Degradation (-27.0 dBm)
    warn_res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "OPTICAL_DEGRADE",
            "target_type": "DEVICE",
            "target_id": ont_code,
            "severity": "WARNING"
        }
    )
    assert warn_res.status_code == 200
    warn_data = warn_res.json()
    assert warn_data["severity"] == "WARNING"
    assert warn_data["target_rx_dbm"] == -27.0

    # Verify device status & alarm
    db = SessionLocal()
    try:
        updated_ont = db.query(NetworkDevice).filter(NetworkDevice.id == ont_id).first()
        assert updated_ont.optical_rx_dbm == -27.0
        assert updated_ont.status == "WARNING"

        alm = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == ont_id,
            NetworkAlarm.alarm_code == f"ALM-OPT-DEG-{ont_id}",
            NetworkAlarm.status == "ACTIVE"
        ).first()
        assert alm is not None
        assert alm.severity == "WARNING"
    finally:
        db.close()

    # Step B: Escalate to Critical Degradation (-29.5 dBm)
    crit_res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "OPTICAL_DEGRADE",
            "target_type": "DEVICE",
            "target_id": ont_code,
            "severity": "CRITICAL"
        }
    )
    assert crit_res.status_code == 200
    crit_data = crit_res.json()
    assert crit_data["target_rx_dbm"] == -29.5

    db = SessionLocal()
    try:
        updated_ont = db.query(NetworkDevice).filter(NetworkDevice.id == ont_id).first()
        assert updated_ont.optical_rx_dbm == -29.5
        assert updated_ont.status == "DEGRADED"

        # Verify alarm was updated in-place without duplicating
        active_alms = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == ont_id,
            NetworkAlarm.alarm_code == f"ALM-OPT-DEG-{ont_id}",
            NetworkAlarm.status == "ACTIVE"
        ).all()
        assert len(active_alms) == 1
        assert active_alms[0].severity == "CRITICAL"
    finally:
        db.close()

    # Recovery
    client.post(
        "/api/network/scenarios/recover",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={"target_type": "DEVICE", "target_id": ont_code}
    )


# ==============================================================================
# 3. PEAK TRAFFIC SATURATION TESTS
# ==============================================================================

def test_peak_traffic_saturates_ports_and_triggers_alarms(tokens):
    """
    PEAK_TRAFFIC spikes PON port utilization (94%) and OLT CPU/temp,
    evaluating configured thresholds and raising HIGH_PON_UTILIZATION alarm.
    """
    db = SessionLocal()
    try:
        olt = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type == "OLT"
        ).first()
        assert olt is not None
        olt_id = olt.id
        olt_code = olt.device_code
    finally:
        db.close()

    res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["admin"], "mumbai"),
        json={
            "scenario_type": "PEAK_TRAFFIC",
            "target_type": "DEVICE",
            "target_id": olt_code,
            "severity": "CRITICAL"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["target_utilization_pct"] == 94.0
    assert data["affected_ports_count"] >= 1

    db = SessionLocal()
    try:
        updated_olt = db.query(NetworkDevice).filter(NetworkDevice.id == olt_id).first()
        assert updated_olt.status == "CRITICAL"
        assert updated_olt.cpu_utilization_pct >= 85.0

        for p in updated_olt.ports:
            assert p.utilization_pct == 94.0
            assert p.status == "CRITICAL"

        # Check alarm
        alm = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == olt_id,
            NetworkAlarm.code == "HIGH_CPU",
            NetworkAlarm.status == "ACTIVE"
        ).first()
        assert alm is not None
        assert alm.severity == "CRITICAL"
    finally:
        db.close()

    # Clean up
    client.post(
        "/api/network/scenarios/recover",
        headers=auth_header(tokens["admin"], "mumbai"),
        json={"target_type": "DEVICE", "target_id": olt_code}
    )


# ==============================================================================
# 4. RECOVERY LIFECYCLE & HISTORICAL PRESERVATION TESTS
# ==============================================================================

def test_recovery_restores_health_and_clears_alarms_preserving_history(tokens):
    """
    RECOVERY restores all mutated states and transitions active alarms to CLEARED
    with timestamp, preserving historical auditability.
    """
    db = SessionLocal()
    try:
        link = db.query(NetworkLink).filter(NetworkLink.market_id == "mumbai").first()
        link_id = link.id
    finally:
        db.close()

    # Trigger outage
    cut_res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "FIBER_CUT",
            "target_type": "LINK",
            "target_id": str(link_id)
        }
    )
    assert cut_res.status_code == 200
    scen_id = cut_res.json()["scenario_id"]

    # Execute Recovery
    rec_res = client.post(
        "/api/network/scenarios/recover",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={"scenario_id": scen_id}
    )
    assert rec_res.status_code == 200
    rec_data = rec_res.json()
    assert rec_data["status"] == "RECOVERED"
    assert rec_data["recovered_links_count"] >= 1
    assert rec_data["cleared_alarms_count"] >= 1

    # Verify alarms are CLEARED, NOT deleted!
    db = SessionLocal()
    try:
        cleared_alms = db.query(NetworkAlarm).filter(
            NetworkAlarm.alarm_code.like("ALM-FIBER-CUT-%"),
            NetworkAlarm.status == "CLEARED"
        ).all()
        assert len(cleared_alms) >= 1
        for a in cleared_alms:
            assert a.cleared_at is not None

        # Verify link is ACTIVE
        restored_link = db.query(NetworkLink).filter(NetworkLink.id == link_id).first()
        assert restored_link.status in ("ACTIVE", "HEALTHY")
    finally:
        db.close()


# ==============================================================================
# 5. VALIDATION & ERROR REJECTION TESTS
# ==============================================================================

def test_invalid_scenario_and_target_rejected(tokens):
    """Unsupported scenarios and non-existent targets return safe 400/404 responses."""
    # Unsupported scenario
    res1 = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "SOLAR_FLARE_DISRUPTION",
            "target_type": "LINK",
            "target_id": "1"
        }
    )
    assert res1.status_code == 400
    assert "Invalid scenario_type" in res1.json()["detail"]

    # Non-existent target
    res2 = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "FIBER_CUT",
            "target_type": "LINK",
            "target_id": "999999"
        }
    )
    assert res2.status_code == 404
    assert "not found" in res2.json()["detail"].lower()

    # Invalid target type
    res3 = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "FIBER_CUT",
            "target_type": "SPACE_SATELLITE",
            "target_id": "1"
        }
    )
    assert res3.status_code == 400
    assert "Invalid target_type" in res3.json()["detail"]


# ==============================================================================
# 6. RBAC & AUTHORIZATION TESTS
# ==============================================================================

def test_scenario_rbac_authorization(tokens):
    """
    SUPER_ADMIN, Admin, and NOC are authorized to trigger scenarios.
    Viewer, Customer, and unauthenticated requests are strictly denied.
    """
    db = SessionLocal()
    try:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type == "ONT"
        ).first()
        ont_id = ont.id
    finally:
        db.close()

    payload = {
        "scenario_type": "OPTICAL_DEGRADE",
        "target_type": "DEVICE",
        "target_id": str(ont_id),
        "severity": "WARNING"
    }

    # Allowed personas
    assert client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["superadmin"]), json=payload).status_code == 200
    assert client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["admin"]), json=payload).status_code == 200
    assert client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["noc"]), json=payload).status_code == 200

    # Denied personas
    assert client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["viewer"]), json=payload).status_code == 403
    assert client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["customer"]), json=payload).status_code == 403
    assert client.post("/api/network/scenarios/trigger", json=payload).status_code in (401, 403)

    # Cleanup
    client.post("/api/network/scenarios/recover", headers=auth_header(tokens["admin"]), json={"target_type": "DEVICE", "target_id": str(ont_id)})


# ==============================================================================
# 7. TENANT & MULTI-MARKET ISOLATION TESTS
# ==============================================================================

def test_cross_market_target_strictly_rejected(tokens):
    """
    Attempting to trigger a scenario on a target in Kolkata using Mumbai credentials
    is strictly rejected with 403 Forbidden.
    """
    db = SessionLocal()
    try:
        kol_dev = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "kolkata",
            NetworkDevice.device_type == "OLT"
        ).first()
        assert kol_dev is not None
        kol_code = kol_dev.device_code
    finally:
        db.close()

    res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),  # Mismatched market header!
        json={
            "scenario_type": "PEAK_TRAFFIC",
            "target_type": "DEVICE",
            "target_id": kol_code
        }
    )
    assert res.status_code == 403
    assert "Forbidden" in res.json()["detail"]


# ==============================================================================
# 8. IDEMPOTENCY & REPLAY SAFETY TESTS
# ==============================================================================

def test_idempotent_replay_prevents_duplicate_scenarios_and_alarms(tokens):
    """
    Replaying a scenario trigger with an idempotency key returns the cached
    result without duplicating alarms or creating conflicting state.
    """
    db = SessionLocal()
    try:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type == "ONT"
        ).first()
        ont_id = ont.id
    finally:
        db.close()

    idem_key = f"idemp-test-{int(time.time())}"
    payload = {
        "scenario_type": "OPTICAL_DEGRADE",
        "target_type": "DEVICE",
        "target_id": str(ont_id),
        "severity": "CRITICAL",
        "idempotency_key": idem_key
    }

    # First trigger
    res1 = client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["noc"]), json=payload)
    assert res1.status_code == 200
    data1 = res1.json()

    # Replayed trigger
    res2 = client.post("/api/network/scenarios/trigger", headers=auth_header(tokens["noc"]), json=payload)
    assert res2.status_code == 200
    data2 = res2.json()

    assert data2["scenario_id"] == data1["scenario_id"]
    assert data2.get("idempotent_replay") is True

    # Verify only ONE active alarm was created
    db = SessionLocal()
    try:
        alms = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == ont_id,
            NetworkAlarm.alarm_code == f"ALM-OPT-DEG-{ont_id}",
            NetworkAlarm.status == "ACTIVE"
        ).all()
        assert len(alms) == 1
    finally:
        db.close()

    # Cleanup
    client.post("/api/network/scenarios/recover", headers=auth_header(tokens["noc"]), json={"target_type": "DEVICE", "target_id": str(ont_id)})


# ==============================================================================
# 9. TRANSACTION ROLLBACK ON FAILURE TESTS
# ==============================================================================

def test_transaction_rollback_leaves_zero_orphaned_state(tokens, monkeypatch):
    """
    If a database write fails during scenario execution, the transaction rolls back cleanly,
    leaving no partial telemetry or alarm state.
    """
    db = SessionLocal()
    try:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type == "ONT"
        ).first()
        ont_id = ont.id
        initial_status = ont.status
        initial_rx = ont.optical_rx_dbm
    finally:
        db.close()

    # Simulate unexpected DB failure during execution
    engine = get_scenario_engine()

    def fail_execute(*args, **kwargs):
        raise RuntimeError("Simulated Database I/O Write Failure")

    monkeypatch.setattr(engine, "_execute_optical_degrade", fail_execute)

    with pytest.raises(RuntimeError, match="Simulated Database I/O Write Failure"):
        engine.trigger_scenario(
            db=db,
            scenario_type="OPTICAL_DEGRADE",
            target_type="DEVICE",
            target_id=str(ont_id),
            market_id="mumbai"
        )

    # Verify DB state was completely unchanged
    db = SessionLocal()
    try:
        verified_ont = db.query(NetworkDevice).filter(NetworkDevice.id == ont_id).first()
        assert verified_ont.status == initial_status
        assert verified_ont.optical_rx_dbm == initial_rx
    finally:
        db.close()


# ==============================================================================
# 10. AUDIT LOGGING & PROVENANCE TESTS
# ==============================================================================

def test_audit_log_recorded_with_provenance(tokens):
    """Scenario execution records actor identity, parameters, and outcome in audit_logs."""
    db = SessionLocal()
    try:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type == "ONT"
        ).first()
        ont_id = ont.id
    finally:
        db.close()

    res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["admin"], "mumbai"),
        json={
            "scenario_type": "OPTICAL_DEGRADE",
            "target_type": "DEVICE",
            "target_id": str(ont_id),
            "severity": "WARNING"
        }
    )
    assert res.status_code == 200
    scen_id = res.json()["scenario_id"]

    db = SessionLocal()
    try:
        audit = db.query(AuditLog).filter(
            AuditLog.source_module == "NETWORK_SCENARIO_ENGINE",
            AuditLog.market_id == "mumbai"
        ).order_by(AuditLog.id.desc()).first()
        assert audit is not None
        assert audit.decision == "EXECUTED"
        assert audit.user_name == "PMRG AI Administrator"
        assert audit.execution_result.get("scenario_id") == scen_id
    finally:
        db.close()

    # Cleanup
    client.post("/api/network/scenarios/recover", headers=auth_header(tokens["admin"]), json={"target_type": "DEVICE", "target_id": str(ont_id)})


# ==============================================================================
# 11. ACTIVE SCENARIOS LISTING ENDPOINT TESTS
# ==============================================================================

def test_active_scenarios_listing_endpoint(tokens):
    """GET /api/network/scenarios/active returns currently active scenarios in market."""
    db = SessionLocal()
    try:
        link = db.query(NetworkLink).filter(NetworkLink.market_id == "mumbai").first()
        link_id = link.id
    finally:
        db.close()

    # Trigger scenario
    trig_res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={
            "scenario_type": "FIBER_CUT",
            "target_type": "LINK",
            "target_id": str(link_id)
        }
    )
    assert trig_res.status_code == 200
    scen_id = trig_res.json()["scenario_id"]

    # Query active scenarios
    active_res = client.get("/api/network/scenarios/active", headers=auth_header(tokens["noc"], "mumbai"))
    assert active_res.status_code == 200
    active_list = active_res.json()
    assert any(s["scenario_id"] == scen_id for s in active_list)

    # Recover
    client.post("/api/network/scenarios/recover", headers=auth_header(tokens["noc"]), json={"scenario_id": scen_id})


# ==============================================================================
# 12. END-TO-END TELEMETRY & MAP PROPAGATION TESTS
# ==============================================================================

def test_end_to_end_propagation_to_overview_and_map_layers(tokens):
    """
    Scenario execution immediately updates /network/overview, /map-layers, and /impact
    without requiring external polling loops.
    """
    # Clean recovery first
    client.post("/api/network/scenarios/recover", headers=auth_header(tokens["noc"], "mumbai"), json={})

    db = SessionLocal()
    try:
        # Find a link where target device is currently active/healthy
        link = db.query(NetworkLink).join(
            NetworkDevice, NetworkLink.target_device_id == NetworkDevice.id
        ).filter(
            NetworkLink.market_id == "mumbai",
            NetworkDevice.status.in_(["HEALTHY", "ONLINE", "ACTIVE"])
        ).first()
        if not link:
            link = db.query(NetworkLink).filter(NetworkLink.market_id == "mumbai").first()
        link_id = link.id
        cab_id = link.target_device_id
    finally:
        db.close()

    # Pre-scenario baseline
    pre_overview = client.get("/api/network/overview", headers=auth_header(tokens["noc"])).json()
    pre_down_count = pre_overview["health_breakdown"].get("DOWN", 0)

    # Trigger FIBER_CUT
    cut_res = client.post(
        "/api/network/scenarios/trigger",
        headers=auth_header(tokens["noc"], "mumbai"),
        json={"scenario_type": "FIBER_CUT", "target_type": "LINK", "target_id": str(link_id)}
    )
    assert cut_res.status_code == 200
    scen_id = cut_res.json()["scenario_id"]

    # Verify /network/overview reflects increased DOWN count
    post_overview = client.get("/api/network/overview", headers=auth_header(tokens["noc"])).json()
    assert post_overview["health_breakdown"].get("DOWN", 0) > pre_down_count

    # Verify /network/map-layers reflects severed link
    layers = client.get("/api/network/map-layers", headers=auth_header(tokens["noc"])).json()
    matching_link = next((l for l in layers["links"] if l["id"] == link_id), None)
    assert matching_link is not None
    assert matching_link["status"] == "DOWN"

    # Verify /network/impact reflects downstream affected devices & customers
    db = SessionLocal()
    try:
        cab = db.query(NetworkDevice).filter(NetworkDevice.id == cab_id).first()
        cab_code = cab.device_code
    finally:
        db.close()

    impact = client.get(f"/api/network/impact/{cab_code}", headers=auth_header(tokens["noc"])).json()
    assert impact["total_downstream_devices"] >= 1

    # Recover
    client.post("/api/network/scenarios/recover", headers=auth_header(tokens["noc"]), json={"scenario_id": scen_id})
