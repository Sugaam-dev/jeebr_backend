"""
Phase 8: System & Network Health Monitoring Integration & Unit Test Suite

Tests:
1. System metrics calculation: CPU %, Memory (used/avail/total GB), Temperature, Uptime.
2. PON utilization & optical health integration.
3. Telemetry freshness calculation (LIVE, STALE, OFFLINE).
4. Deterministic health classification priority (DOWN > CRITICAL > DEGRADED > WARNING > HEALTHY > UNKNOWN).
5. Configurable thresholds API (GET, PUT validation, warning <= critical).
6. Alarm lifecycle: threshold breach -> alarm raised; re-poll -> no duplicate; recovery -> alarm CLEARED.
7. Historical metrics API (1h, 6h, 24h ranges).
8. Safe test scenario simulation (HEALTHY, WARNING, CRITICAL, DOWN).
9. Strict market isolation (Mumbai vs Kolkata).
10. RBAC enforcement (SUPER_ADMIN/Admin vs NOC vs Viewer/Customer).
11. Telemetry source labeling ('Synthetic / Demo Telemetry').
"""

import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import NetworkDevice, DeviceMetricThreshold, NetworkAlarm, User
from app.services.monitoring_provider import get_monitoring_provider

client = TestClient(app)


def _get_token(email: str, password: str = "admin123") -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


@pytest.fixture(scope="module")
def admin_token():
    return _get_token("admin@pmrg.in")


@pytest.fixture(scope="module")
def noc_token():
    return _get_token("noc@pmrg.in")


@pytest.fixture(scope="module")
def viewer_token():
    return _get_token("customer@pmrg.in")


# ============================================================================
# 1. Monitoring Overview & Telemetry Source
# ============================================================================

def test_monitoring_overview(admin_token):
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}
    res = client.get("/api/network/monitoring/overview", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert "telemetry_source" in data
    assert data["telemetry_source"] == "Synthetic / Demo Telemetry"
    assert "devices_by_type" in data
    assert "devices_by_health" in data
    assert "pon_utilization_summary" in data
    assert "active_monitoring_alarms" in data

    # Verify OLT device types present
    assert data["devices_by_type"].get("OLT", 0) > 0


# ============================================================================
# 2. Device Metrics Payload & Freshness
# ============================================================================

def test_device_metrics_payload(admin_token):
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}
    # Query OLT-BND-01
    res = client.get("/api/network/devices/OLT-BND-01/metrics", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["telemetry_source"] == "Synthetic / Demo Telemetry"
    assert data["device_code"] == "OLT-BND-01"
    assert data["device_type"] == "OLT"

    # System metrics
    system = data["system_metrics"]
    assert "cpu_utilization_pct" in system
    assert 0.0 <= system["cpu_utilization_pct"] <= 100.0
    assert "memory_utilization_pct" in system
    assert 0.0 <= system["memory_utilization_pct"] <= 100.0
    assert "memory_total_gb" in system
    assert "memory_used_gb" in system
    assert "memory_available_gb" in system
    assert round(system["memory_used_gb"] + system["memory_available_gb"], 1) == round(system["memory_total_gb"], 1)
    assert "temperature_celsius" in system
    assert "uptime_seconds" in system
    assert "uptime_formatted" in system

    # Freshness
    freshness = data["telemetry_freshness"]
    assert freshness["freshness_status"] in ["LIVE", "STALE", "OFFLINE"]
    assert "seconds_ago" in freshness

    # Overall health classification
    assert data["overall_status"] in ["HEALTHY", "WARNING", "CRITICAL", "DOWN", "DEGRADED", "UNKNOWN"]
    assert isinstance(data["health_reasons"], list)


# ============================================================================
# 3. Deterministic Health Classification Engine via Simulation
# ============================================================================

def test_health_classification_scenarios(admin_token):
    """
    Test deterministic priority: DOWN (6) > CRITICAL (5) > DEGRADED (4) > WARNING (3) > HEALTHY (2) > UNKNOWN (1)
    """
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}

    # 1. DOWN scenario
    res_down = client.post("/api/network/devices/OLT-BKC-01/simulate", json={"scenario": "DOWN"}, headers=headers)
    assert res_down.status_code == 200
    assert res_down.json()["overall_status"] == "DOWN"

    # 2. CRITICAL scenario
    res_crit = client.post("/api/network/devices/OLT-BKC-01/simulate", json={"scenario": "CRITICAL"}, headers=headers)
    assert res_crit.status_code == 200
    assert res_crit.json()["overall_status"] == "CRITICAL"

    # 3. WARNING scenario
    res_warn = client.post("/api/network/devices/OLT-BKC-01/simulate", json={"scenario": "WARNING"}, headers=headers)
    assert res_warn.status_code == 200
    assert res_warn.json()["overall_status"] == "WARNING"

    # 4. HEALTHY recovery
    res_hlth = client.post("/api/network/devices/OLT-BKC-01/simulate", json={"scenario": "HEALTHY"}, headers=headers)
    assert res_hlth.status_code == 200
    assert res_hlth.json()["overall_status"] == "HEALTHY"


# ============================================================================
# 4. Configurable Thresholds & RBAC
# ============================================================================

def test_list_thresholds(admin_token):
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}
    res = client.get("/api/network/monitoring/thresholds", headers=headers)
    assert res.status_code == 200, res.text
    thresholds = res.json()
    assert len(thresholds) > 0

    metric_types = [t["metric_type"] for t in thresholds]
    assert "CPU_UTILIZATION" in metric_types
    assert "MEMORY_UTILIZATION" in metric_types
    assert "TEMPERATURE" in metric_types
    assert "PON_UTILIZATION" in metric_types


def test_update_threshold_validation_and_rbac(admin_token, noc_token, viewer_token):
    admin_headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}
    noc_headers = {"Authorization": f"Bearer {noc_token}", "X-Market-Id": "mumbai"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}", "X-Market-Id": "mumbai"}

    # Find a threshold ID
    get_res = client.get("/api/network/monitoring/thresholds?device_type=OLT", headers=admin_headers)
    assert get_res.status_code == 200
    thresh_list = get_res.json()
    assert len(thresh_list) > 0
    target_thresh = thresh_list[0]
    thresh_id = target_thresh["id"]

    # 1. NOC cannot update thresholds (RBAC check)
    noc_res = client.put(
        f"/api/network/monitoring/thresholds/{thresh_id}",
        json={"warning_threshold": 70.0, "critical_threshold": 85.0},
        headers=noc_headers
    )
    assert noc_res.status_code == 403

    # 2. Viewer cannot update thresholds
    viewer_res = client.put(
        f"/api/network/monitoring/thresholds/{thresh_id}",
        json={"warning_threshold": 70.0, "critical_threshold": 85.0},
        headers=viewer_headers
    )
    assert viewer_res.status_code == 403

    # 3. Invalid threshold: warning > critical (validation check)
    invalid_res = client.put(
        f"/api/network/monitoring/thresholds/{thresh_id}",
        json={"warning_threshold": 95.0, "critical_threshold": 80.0},
        headers=admin_headers
    )
    assert invalid_res.status_code == 400

    # 4. Valid update by Admin
    orig_warn = target_thresh["warning_threshold"]
    orig_crit = target_thresh["critical_threshold"]

    new_warn = 72.5
    new_crit = 88.5
    update_res = client.put(
        f"/api/network/monitoring/thresholds/{thresh_id}",
        json={"warning_threshold": new_warn, "critical_threshold": new_crit},
        headers=admin_headers
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["warning_threshold"] == new_warn
    assert updated["critical_threshold"] == new_crit

    # Revert to original
    client.put(
        f"/api/network/monitoring/thresholds/{thresh_id}",
        json={"warning_threshold": orig_warn, "critical_threshold": orig_crit},
        headers=admin_headers
    )


# ============================================================================
# 5. Monitoring Alarm Lifecycle & Auto-Recovery
# ============================================================================

def test_alarm_lifecycle_and_auto_recovery(admin_token):
    """
    Test that elevated metrics trigger alarms, re-polls do not spam duplicates,
    and returning to HEALTHY auto-clears the alarm.
    """
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}

    # 1. Simulate CRITICAL on OLT-BND-01
    sim_res = client.post(
        "/api/network/devices/OLT-BND-01/simulate",
        json={"scenario": "CRITICAL"},
        headers=headers
    )
    assert sim_res.status_code == 200, sim_res.text
    sim_data = sim_res.json()
    assert sim_data["overall_status"] == "CRITICAL"
    assert sim_data["system_metrics"]["cpu_utilization_pct"] >= 90.0

    # Verify alarm is present
    alarm_res = client.get("/api/network/monitoring/alarms?status=ACTIVE", headers=headers)
    assert alarm_res.status_code == 200
    alarms = alarm_res.json()
    olt_alarms = [a for a in alarms if a["device_code"] == "OLT-BND-01" and a["alarm_type"] == "HIGH_CPU"]
    assert len(olt_alarms) >= 1
    assert olt_alarms[0]["severity"] == "CRITICAL"

    # 2. Re-poll device metrics (idempotent / no duplicate alarms)
    re_poll = client.get("/api/network/devices/OLT-BND-01/metrics", headers=headers)
    assert re_poll.status_code == 200

    alarm_res2 = client.get("/api/network/monitoring/alarms?status=ACTIVE", headers=headers)
    olt_alarms2 = [a for a in alarm_res2.json() if a["device_code"] == "OLT-BND-01" and a["alarm_type"] == "HIGH_CPU"]
    assert len(olt_alarms2) == len(olt_alarms), "Re-poll created duplicate alarm!"

    # 3. Simulate HEALTHY recovery
    recover_res = client.post(
        "/api/network/devices/OLT-BND-01/simulate",
        json={"scenario": "HEALTHY"},
        headers=headers
    )
    assert recover_res.status_code == 200
    rec_data = recover_res.json()
    assert rec_data["overall_status"] == "HEALTHY"

    # Verify active HIGH_CPU alarm was CLEARED
    alarm_res3 = client.get("/api/network/monitoring/alarms?status=ACTIVE", headers=headers)
    active_olt_alarms = [a for a in alarm_res3.json() if a["device_code"] == "OLT-BND-01" and a["alarm_type"] == "HIGH_CPU"]
    assert len(active_olt_alarms) == 0, "HIGH_CPU alarm was not auto-cleared upon metric recovery!"


# ============================================================================
# 6. Historical Metrics Endpoints (1h, 6h, 24h)
# ============================================================================

def test_device_metric_history(admin_token):
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}

    for tr in ["1h", "6h", "24h"]:
        res = client.get(f"/api/network/devices/OLT-BND-01/metrics/history?time_range={tr}", headers=headers)
        assert res.status_code == 200, res.text
        hist = res.json()

        assert hist["device_code"] == "OLT-BND-01"
        assert hist["time_range"] == tr
        assert hist["telemetry_source"] == "Synthetic / Demo Telemetry"
        assert len(hist["points"]) > 0

        # Validate point structure
        first_pt = hist["points"][0]
        assert "timestamp" in first_pt
        assert "cpu_utilization_pct" in first_pt
        assert "memory_utilization_pct" in first_pt
        assert "temperature_celsius" in first_pt
        assert "pon_utilization_pct" in first_pt


# ============================================================================
# 7. Safe Simulation Scenarios
# ============================================================================

def test_simulation_scenarios(admin_token):
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}

    for scenario in ["HEALTHY", "WARNING", "CRITICAL", "DOWN"]:
        res = client.post(
            "/api/network/devices/OLT-AND-03/simulate",
            json={"scenario": scenario},
            headers=headers
        )
        assert res.status_code == 200, f"Simulation {scenario} failed: {res.text}"
        data = res.json()
        assert data["overall_status"] == scenario

    # Clean up back to HEALTHY
    client.post(
        "/api/network/devices/OLT-AND-03/simulate",
        json={"scenario": "HEALTHY"},
        headers=headers
    )


# ============================================================================
# 8. Strict Market Isolation
# ============================================================================

def test_cross_market_isolation(admin_token):
    """
    OLT-SL-01 is in 'kolkata' market.
    Querying from 'mumbai' session header must return 403 Forbidden.
    """
    mumbai_headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}

    res = client.get("/api/network/devices/OLT-SL-01/metrics", headers=mumbai_headers)
    assert res.status_code == 403, f"Expected 403 cross-market forbidden, got {res.status_code}"
    assert "another market" in res.text or "Forbidden" in res.text

    # With kolkata header, it succeeds
    kolkata_headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "kolkata"}
    res_kol = client.get("/api/network/devices/OLT-SL-01/metrics", headers=kolkata_headers)
    assert res_kol.status_code == 200


# ============================================================================
# 9. RBAC Enforcement on All Monitoring Endpoints
# ============================================================================

def test_monitoring_rbac(noc_token, viewer_token):
    noc_headers = {"Authorization": f"Bearer {noc_token}", "X-Market-Id": "mumbai"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}", "X-Market-Id": "mumbai"}

    # NOC can read metrics & overview
    res_noc_overview = client.get("/api/network/monitoring/overview", headers=noc_headers)
    assert res_noc_overview.status_code == 200

    res_noc_metrics = client.get("/api/network/devices/OLT-BND-01/metrics", headers=noc_headers)
    assert res_noc_metrics.status_code == 200

    # NOC cannot simulate scenarios
    res_noc_sim = client.post(
        "/api/network/devices/OLT-BND-01/simulate",
        json={"scenario": "WARNING"},
        headers=noc_headers
    )
    assert res_noc_sim.status_code == 403

    # Customer/Viewer cannot access monitoring
    res_viewer_overview = client.get("/api/network/monitoring/overview", headers=viewer_headers)
    assert res_viewer_overview.status_code == 403

    res_viewer_metrics = client.get("/api/network/devices/OLT-BND-01/metrics", headers=viewer_headers)
    assert res_viewer_metrics.status_code == 403
