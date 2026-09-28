import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models import User, Customer, Ticket, Node
from app.services.olt import (
    get_olt_adapter,
    SyntheticOLTAdapter,
    MockSNMPOLTAdapter,
    OLTStatus,
    AlarmSeverity,
    ONTStatus
)

client = TestClient(app)


def _get_token(email: str, password: str = "admin123") -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


# --- 1. OLT Adapter & Normalization Tests (Feature 2) ---

@pytest.mark.anyio
async def test_olt_synthetic_adapter_full_snapshot():
    """Verify SyntheticOLTAdapter provides all 5 normalized telemetry stages."""
    adapter = SyntheticOLTAdapter(device_id="OLT-MUM-001", management_ip="10.120.1.1")
    snapshot = await adapter.collect_full_snapshot()

    # Stage 1: Device Identity & Reachability
    assert snapshot.olt.id == "OLT-MUM-001"
    assert snapshot.olt.status in (OLTStatus.ONLINE, OLTStatus.DEGRADED)
    assert snapshot.olt.management_ip == "10.120.1.1"
    assert snapshot.olt.pon_ports_count >= 8

    # Stage 2: Hardware System Metrics
    assert 0.0 <= snapshot.system_metrics.cpu_utilization_pct <= 100.0
    assert 0.0 <= snapshot.system_metrics.memory_utilization_pct <= 100.0
    assert snapshot.system_metrics.temperature_celsius > 0.0

    # Stage 3: PON Interface Telemetry
    assert len(snapshot.pon_ports) >= 4
    for port in snapshot.pon_ports:
        assert port.olt_id == "OLT-MUM-001"
        assert port.admin_status == "UP"
        assert port.technology in ("GPON", "XGS-PON")

    # Stage 4: ONT Subscriber Optical Levels
    assert len(snapshot.onts) > 0
    for ont in snapshot.onts:
        assert ont.status in (ONTStatus.ONLINE, ONTStatus.DEGRADED, ONTStatus.LOS)
        assert ont.signal.rx_power_dbm < 0.0  # optical Rx power is negative dBm
        assert ont.signal.tx_power_dbm > 0.0

    # Stage 5: Alarm Telemetry
    assert isinstance(snapshot.active_alarms, list)


def test_olt_adapter_factory_switching():
    """Verify OLT factory dynamically switches provider implementations via configuration."""
    # Synthetic default
    syn_adapter = get_olt_adapter(provider_override="synthetic")
    assert isinstance(syn_adapter, SyntheticOLTAdapter)

    # SNMP mock
    snmp_adapter = get_olt_adapter(provider_override="snmp_mock")
    assert isinstance(snmp_adapter, MockSNMPOLTAdapter)


@pytest.mark.anyio
async def test_mock_snmp_granular_failure_modes():
    """
    Verify OLT adapter distinguishes granular hardware failure modes (Section 27):
    ONLINE vs TIMEOUT vs AUTH_FAILURE vs UNREACHABLE.
    """
    # 1. Timeout simulation
    timeout_adapter = MockSNMPOLTAdapter(
        device_id="OLT-TEST-001",
        management_ip="192.168.10.1",
        simulate_failure_mode="TIMEOUT"
    )
    with pytest.raises(TimeoutError):
        await timeout_adapter.connect()
    health_timeout = await timeout_adapter.health_check()
    assert health_timeout["status"] == OLTStatus.TIMEOUT.value
    assert health_timeout["reachable"] is False

    # 2. Authentication failure simulation
    auth_adapter = MockSNMPOLTAdapter(
        device_id="OLT-TEST-002",
        management_ip="192.168.10.2",
        simulate_failure_mode="AUTH_FAILURE"
    )
    with pytest.raises(PermissionError):
        await auth_adapter.connect()
    health_auth = await auth_adapter.health_check()
    assert health_auth["status"] == OLTStatus.AUTH_FAILURE.value

    # 3. Host unreachable simulation
    unreach_adapter = MockSNMPOLTAdapter(
        device_id="OLT-TEST-003",
        management_ip="192.168.10.3",
        simulate_failure_mode="UNREACHABLE"
    )
    with pytest.raises(ConnectionRefusedError):
        await unreach_adapter.connect()
    health_unreach = await unreach_adapter.health_check()
    assert health_unreach["status"] == OLTStatus.UNREACHABLE.value


# --- 2. OLT REST API & RBAC Tests ---

def test_olt_rest_api_lifecycle():
    """Verify OLT REST endpoints deliver normalized telemetry to NOC dashboard."""
    noc_token = _get_token("noc@pmrg.in")
    headers = {"Authorization": f"Bearer {noc_token}"}

    # 1. Health check
    res_health = client.get("/api/olt/health?device_id=OLT-MUM-001", headers=headers)
    assert res_health.status_code == 200
    assert res_health.json()["reachable"] is True

    # 2. Normalized full telemetry snapshot
    res_telemetry = client.get("/api/olt/devices/OLT-MUM-001/telemetry", headers=headers)
    assert res_telemetry.status_code == 200
    data = res_telemetry.json()
    assert "olt" in data
    assert "system_metrics" in data
    assert "pon_ports" in data
    assert "onts" in data
    assert data["olt"]["id"] == "OLT-MUM-001"

    # 3. System metrics
    res_metrics = client.get("/api/olt/devices/OLT-MUM-001/system-metrics", headers=headers)
    assert res_metrics.status_code == 200
    assert "cpu_utilization_pct" in res_metrics.json()

    # 4. PON ports
    res_ports = client.get("/api/olt/devices/OLT-MUM-001/pon-ports", headers=headers)
    assert res_ports.status_code == 200
    assert isinstance(res_ports.json(), list)

    # 5. ONT endpoints
    res_onts = client.get("/api/olt/devices/OLT-MUM-001/onts", headers=headers)
    assert res_onts.status_code == 200
    assert isinstance(res_onts.json(), list)

    # 6. Alarms
    res_alarms = client.get("/api/olt/devices/OLT-MUM-001/alarms", headers=headers)
    assert res_alarms.status_code == 200
    assert isinstance(res_alarms.json(), list)


def test_olt_api_unauthorized_and_viewer_restrictions():
    """Verify OLT endpoints are protected against unauthenticated and unauthorized access."""
    # 1. Unauthenticated request -> 401
    client.cookies.clear()
    unauth_res = client.get("/api/olt/devices/OLT-MUM-001/telemetry")
    assert unauth_res.status_code == 401

    # 2. Viewer role attempting manual collection trigger -> 403 Forbidden
    viewer_token = _get_token("client@pmrgsolution.com", "Client@pmrg123")
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}
    forbidden_collect = client.post("/api/olt/devices/OLT-MUM-001/collect", headers=viewer_headers)
    assert forbidden_collect.status_code == 403


# --- 3. Security Hardening Tests (Demo Login, Customer Linkage, Geolocation) ---

def test_demo_login_disabled_in_production(monkeypatch):
    """Verify demo-login backdoor is strictly blocked when ENVIRONMENT=production (Rule 30)."""
    # 1. Under production environment: returns 403 Forbidden
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    prod_res = client.post("/api/auth/demo-login/Admin")
    assert prod_res.status_code == 403
    assert "strictly disabled in production" in prod_res.json()["detail"]

    # 2. Under development environment: allowed for testing
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    dev_res = client.post("/api/auth/demo-login/Admin")
    assert dev_res.status_code == 200
    assert "access_token" in dev_res.json()


def test_customer_ticket_creation_never_null_customer_id():
    """
    Verify customer ticket linkage (Rule 32):
    When authenticated customer raises a ticket, customer_id is never NULL in DB.
    """
    cust_token = _get_token("customer@pmrg.in")
    headers = {"Authorization": f"Bearer {cust_token}"}

    create_res = client.post(
        "/api/tickets",
        headers=headers,
        json={
            "source": "CUSTOMER",
            "category": "Speed",
            "priority": "P3",
            "description": "Optical fiber broadband bandwidth degraded below 50Mbps profile."
        }
    )
    assert create_res.status_code == 200
    ticket_data = create_res.json()
    ticket_id = ticket_data["id"]

    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        assert t is not None
        assert t.customer_id is not None, "FATAL: ticket.customer_id must never be NULL for authenticated customer!"
        
        # Verify customer identity was derived from authenticated user
        cust = db.query(Customer).filter(Customer.id == t.customer_id).first()
        assert cust is not None
        assert cust.email.lower() == "customer@pmrg.in"
    finally:
        db.close()
