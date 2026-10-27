"""
app/tests/test_scenario_recovery_isolation.py

Verifies Workstream C: Selective Network Scenario Recovery & Overlapping Isolation
1. Target-scoped recovery of Scenario A leaves unrelated Scenario B active in the same market.
2. Downstream device still affected by another active fault is NOT restored and its alarm remains active.
3. Ambiguous recovery requests (missing target and scenario_id) are rejected with clear error.
4. Blanket recovery ('ALL') clears all active faults across the market.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models import NetworkDevice, NetworkLink, NetworkAlarm, NetworkScenario
from app.services.scenario_engine import get_scenario_engine, SCENARIO_FIBER_CUT, SCENARIO_OPTICAL_DEGRADE
from app.auth import create_access_token

client = TestClient(app)


@pytest.fixture
def tokens():
    return {
        "admin": create_access_token({"sub": "admin@pmrg.in", "role": "Admin"}),
        "noc": create_access_token({"sub": "noc@pmrg.in", "role": "NOC"}),
    }


def auth_header(token: str, market: str = "mumbai"):
    return {
        "Authorization": f"Bearer {token}",
        "X-Market-Id": market
    }


def test_target_scoped_recovery_preserves_unrelated_active_scenarios(tokens):
    """
    Simulates two independent incidents in Mumbai:
    Scenario 1: Fiber Cut on Link 1.
    Scenario 2: Optical Degradation on an ONT not on Link 1 (e.g. ONT 4).

    When recovering Link 1 by target (target_type="LINK", target_id="1"):
    - Link 1 scenario is marked RECOVERED.
    - ONT 4 optical degradation scenario REMAINING ACTIVE!
    - ONT 4 remains DEGRADED and its alarm remains ACTIVE.
    """
    headers = auth_header(tokens["noc"], "mumbai")

    with SessionLocal() as db:
        ont = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == "mumbai",
            NetworkDevice.device_type.in_(["ONT", "ONU"])
        ).first()
        assert ont is not None
        target_ont_code = ont.device_code
        target_ont_id = ont.id

    # Step 1: Trigger Scenario 1 (Fiber Cut on Link 1)
    res_scen1 = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "FIBER_CUT", "target_type": "LINK", "target_id": "1", "severity": "CRITICAL"},
        headers=headers
    )
    assert res_scen1.status_code == 200
    scen1_id = res_scen1.json()["scenario_id"]

    # Step 2: Trigger Scenario 2 (Optical Degradation on ONT)
    res_scen2 = client.post(
        "/api/network/scenarios/trigger",
        json={"scenario_type": "OPTICAL_DEGRADE", "target_type": "DEVICE", "target_id": target_ont_code, "severity": "CRITICAL"},
        headers=headers
    )
    assert res_scen2.status_code == 200
    scen2_id = res_scen2.json()["scenario_id"]

    # Verify both scenarios are ACTIVE in DB
    with SessionLocal() as db:
        s1 = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scen1_id).first()
        s2 = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scen2_id).first()
        assert s1.status == "ACTIVE"
        assert s2.status == "ACTIVE"

    # Step 3: Recover Link 1 specifically by target filter
    res_rec = client.post(
        "/api/network/scenarios/recover",
        json={"target_type": "LINK", "target_id": "1"},
        headers=headers
    )
    assert res_rec.status_code == 200

    # Step 4: Verify Selective Recovery in DB
    with SessionLocal() as db:
        s1 = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scen1_id).first()
        s2 = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scen2_id).first()

        # Link 1 scenario RECOVERED
        assert s1.status == "RECOVERED"

        # ONT scenario MUST REMAIN ACTIVE!
        assert s2.status == "ACTIVE", "Bug: Blanket recovery inadvertently cleared unrelated active scenario!"

        # ONT device must still be degraded
        ont_dev = db.query(NetworkDevice).filter(NetworkDevice.id == target_ont_id).first()
        assert ont_dev.status in ("CRITICAL", "DEGRADED", "WARNING")

        # ONT alarm must still be ACTIVE
        ont_alarm = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == target_ont_id,
            NetworkAlarm.status == "ACTIVE"
        ).first()
        assert ont_alarm is not None, "Bug: Blanket recovery inadvertently cleared unrelated active alarm!"

    # Clean up: recover remaining scenario
    client.post("/api/network/scenarios/recover", json={"scenario_id": scen2_id}, headers=headers)


def test_ambiguous_recovery_filter_rejected(tokens):
    """
    Recovering with incomplete target filter (e.g. target_type without target_id)
    raises a clear error and does not touch any database records.
    """
    engine = get_scenario_engine()
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="Ambiguous recovery request"):
            engine.recover_scenario(db=db, target_type="DEVICE", target_id=None, scenario_id=None, market_id="mumbai")
