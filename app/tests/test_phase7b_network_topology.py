"""
Phase 7B — OLT / ONT / ONU Network Topology & Health Layer Test Suite

Covers:
  - Device listing & type/status filtering
  - OLT inventory and PON ports
  - Fiber cabinets and Splitters
  - Customer reverse topology lookup (Customer -> ONT -> Splitter -> Cabinet -> PON -> OLT)
  - Device topology traversal (upstream & downstream)
  - Device optical health & telemetry
  - Impact analysis (deterministic graph traversal)
  - Optical alarms
  - Map layers (GeoJSON features for OLT, Cabinet, Splitter, ONT, Links)
  - RBAC: SUPER_ADMIN, Admin, NOC allowed; Viewer, Customer, unauthenticated denied
  - Tenant / Market isolation: cross-market access rejected with 403
  - Deterministic synthetic provider verification
"""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import User, Customer, NetworkDevice, OLTPort, NetworkLink, NetworkAlarm

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


# ─── 1. Network Overview & Inventory Tests ────────────────────────────────────

class TestNetworkOverviewAndDevices:
    def test_network_overview_mumbai(self, tokens):
        res = client.get("/api/network/overview", headers=auth_header(tokens["noc"], "mumbai"))
        assert res.status_code == 200
        data = res.json()
        assert data["market_id"] == "mumbai"
        assert data["total_olts"] >= 3
        assert data["total_fiber_cabinets"] >= 6
        assert data["total_splitters"] >= 12
        assert data["total_ont_onu"] >= 20
        assert data["telemetry_source"] == "Synthetic / Demo Telemetry"
        assert "health_breakdown" in data
        assert data["health_breakdown"]["HEALTHY"] > 0

    def test_list_devices_filtering(self, tokens):
        # All devices
        res = client.get("/api/network/devices", headers=auth_header(tokens["admin"], "mumbai"))
        assert res.status_code == 200
        devices = res.json()
        assert len(devices) > 0

        # Filter by type OLT
        res_olt = client.get("/api/network/devices?device_type=OLT", headers=auth_header(tokens["admin"], "mumbai"))
        assert res_olt.status_code == 200
        olts = res_olt.json()
        assert len(olts) >= 3
        assert all(d["device_type"] == "OLT" for d in olts)

        # Filter by type FIBER_CABINET
        res_cab = client.get("/api/network/devices?device_type=FIBER_CABINET", headers=auth_header(tokens["admin"], "mumbai"))
        assert res_cab.status_code == 200
        cabs = res_cab.json()
        assert len(cabs) >= 6
        assert all(d["device_type"] == "FIBER_CABINET" for d in cabs)


# ─── 2. OLT & PON Port Tests ──────────────────────────────────────────────────

class TestOLTAndPorts:
    def test_list_olts_mumbai(self, tokens):
        res = client.get("/api/network/olts", headers=auth_header(tokens["noc"], "mumbai"))
        assert res.status_code == 200
        olts = res.json()
        assert len(olts) >= 3
        for olt in olts:
            assert olt["device_type"] == "OLT"
            assert "ports" in olt
            assert len(olt["ports"]) >= 4
            assert olt["latitude"] is not None
            assert olt["longitude"] is not None
            assert olt["connected_ont_count"] >= 0

    def test_get_olt_ports(self, tokens):
        res = client.get("/api/network/olts", headers=auth_header(tokens["noc"], "mumbai"))
        olt_id = res.json()[0]["id"]

        ports_res = client.get(f"/api/network/olts/{olt_id}/ports", headers=auth_header(tokens["noc"], "mumbai"))
        assert ports_res.status_code == 200
        ports = ports_res.json()
        assert len(ports) >= 4
        for p in ports:
            assert "port_number" in p
            assert p["technology"] in ("GPON", "XGS-PON", "EPON")
            assert p["capacity"] > 0
            assert p["tx_power_dbm"] > 0
            assert p["rx_power_dbm"] < 0


# ─── 3. Customer Reverse Topology Lookup Tests ────────────────────────────────

class TestCustomerTopology:
    def test_customer_reverse_topology_path(self, tokens):
        db = SessionLocal()
        try:
            # Pick a customer with linked ONT
            ont = db.query(NetworkDevice).filter(
                NetworkDevice.market_id == "mumbai",
                NetworkDevice.customer_id.isnot(None)
            ).first()
            assert ont is not None, "At least one customer must have a linked ONT in Mumbai"
            cust_id = ont.customer_id
        finally:
            db.close()

        res = client.get(f"/api/network/topology/customer/{cust_id}", headers=auth_header(tokens["noc"], "mumbai"))
        assert res.status_code == 200
        data = res.json()

        assert data["customer"]["id"] == cust_id
        assert data["endpoint"] is not None
        assert data["endpoint"]["device_type"] in ("ONT", "ONU")
        assert data["splitter"] is not None
        assert data["fiber_cabinet"] is not None
        assert data["olt"] is not None
        assert data["pon_port"] is not None
        assert data["telemetry_source"] == "Synthetic / Demo Telemetry"

        # Verify hierarchical path array: OLT -> PON -> CABINET -> SPLITTER -> ONT/ONU -> CUSTOMER
        path = data["path"]
        assert len(path) == 6
        assert path[0]["step"] == "OLT"
        assert path[1]["step"] == "PON"
        assert path[2]["step"] == "CABINET"
        assert path[3]["step"] == "SPLITTER"
        assert path[4]["step"] in ("ONT", "ONU")
        assert path[5]["step"] == "CUSTOMER"


# ─── 4. Device Topology & Health Tests ────────────────────────────────────────

class TestDeviceTopologyAndHealth:
    def test_device_topology_traversal(self, tokens):
        db = SessionLocal()
        try:
            cab = db.query(NetworkDevice).filter(
                NetworkDevice.market_id == "mumbai",
                NetworkDevice.device_type == "FIBER_CABINET"
            ).first()
            assert cab is not None
            cab_code = cab.device_code
        finally:
            db.close()

        res = client.get(f"/api/network/topology/device/{cab_code}", headers=auth_header(tokens["noc"], "mumbai"))
        assert res.status_code == 200
        data = res.json()
        assert data["device"]["device_code"] == cab_code
        assert len(data["upstream_path"]) >= 1
        assert data["upstream_path"][0]["device_type"] == "OLT"
        assert len(data["downstream_children"]) >= 1
        assert data["downstream_children"][0]["device_type"] == "SPLITTER"

    def test_device_health_metrics(self, tokens):
        res = client.get("/api/network/olts", headers=auth_header(tokens["noc"], "mumbai"))
        olt_code = res.json()[0]["device_code"]

        health_res = client.get(f"/api/network/devices/{olt_code}/health", headers=auth_header(tokens["noc"], "mumbai"))
        assert health_res.status_code == 200
        health = health_res.json()
        assert health["device_code"] == olt_code
        assert health["status"] in ("HEALTHY", "WARNING", "DEGRADED", "DOWN")
        assert "uptime_seconds" in health
        assert "temperature_c" in health
        assert health["telemetry_source"] == "Synthetic / Demo Telemetry"


# ─── 5. Impact Analysis Tests ─────────────────────────────────────────────────

class TestImpactAnalysis:
    def test_olt_impact_analysis(self, tokens):
        res = client.get("/api/network/olts", headers=auth_header(tokens["noc"], "mumbai"))
        olt_code = res.json()[0]["device_code"]

        impact_res = client.get(f"/api/network/impact/{olt_code}", headers=auth_header(tokens["noc"], "mumbai"))
        assert impact_res.status_code == 200
        data = impact_res.json()
        assert data["device"]["device_code"] == olt_code
        assert data["total_downstream_devices"] > 0
        assert data["total_affected_customers"] > 0
        assert len(data["affected_customers"]) == data["total_affected_customers"]

    def test_cabinet_impact_analysis(self, tokens):
        res = client.get("/api/network/cabinets", headers=auth_header(tokens["noc"], "mumbai"))
        cab_code = res.json()[0]["device_code"]

        impact_res = client.get(f"/api/network/impact/{cab_code}", headers=auth_header(tokens["noc"], "mumbai"))
        assert impact_res.status_code == 200
        data = impact_res.json()
        assert data["device"]["device_code"] == cab_code
        assert data["total_downstream_devices"] >= 2  # At least 2 splitters


# ─── 6. Network Alarms & Map Layers Tests ─────────────────────────────────────

class TestAlarmsAndMapLayers:
    def test_list_network_alarms(self, tokens):
        res = client.get("/api/network/alarms", headers=auth_header(tokens["noc"], "mumbai"))
        assert res.status_code == 200
        alarms = res.json()
        assert len(alarms) >= 1
        for a in alarms:
            assert "alarm_code" in a
            assert a["severity"] in ("INFO", "WARNING", "CRITICAL")
            assert a["status"] in ("ACTIVE", "CLEARED", "ACKNOWLEDGED")

    def test_map_layers_structure(self, tokens):
        res = client.get("/api/network/map-layers", headers=auth_header(tokens["noc"], "mumbai"))
        assert res.status_code == 200
        data = res.json()
        assert data["market_id"] == "mumbai"
        assert len(data["olts"]) >= 3
        assert len(data["fiber_cabinets"]) >= 6
        assert len(data["splitters"]) >= 12
        assert len(data["onts"]) >= 20
        assert len(data["links"]) >= 10
        # Check link format
        link = data["links"][0]
        assert "coordinates" in link
        assert len(link["coordinates"]) == 2


# ─── 7. Authorization & RBAC Tests ────────────────────────────────────────────

class TestNetworkAuthorization:
    def test_super_admin_allowed(self, tokens):
        res = client.get("/api/network/overview", headers=auth_header(tokens["superadmin"]))
        assert res.status_code == 200

    def test_admin_allowed(self, tokens):
        res = client.get("/api/network/overview", headers=auth_header(tokens["admin"]))
        assert res.status_code == 200

    def test_noc_allowed(self, tokens):
        res = client.get("/api/network/overview", headers=auth_header(tokens["noc"]))
        assert res.status_code == 200

    def test_viewer_denied(self, tokens):
        res = client.get("/api/network/overview", headers=auth_header(tokens["viewer"]))
        assert res.status_code == 403

    def test_customer_denied(self, tokens):
        res = client.get("/api/network/overview", headers=auth_header(tokens["customer"]))
        assert res.status_code == 403

    def test_unauthenticated_denied(self):
        res = client.get("/api/network/overview")
        assert res.status_code in (401, 403)


# ─── 8. Tenant & Market Isolation Tests ───────────────────────────────────────

class TestTenantAndMarketIsolation:
    def test_cross_market_customer_topology_denied(self, tokens):
        db = SessionLocal()
        try:
            # Pick a Kolkata customer
            kol_cust = db.query(Customer).filter(Customer.market_id == "kolkata").first()
            assert kol_cust is not None
            kol_id = kol_cust.id
        finally:
            db.close()

        # Requesting Kolkata customer with Mumbai market header must be denied (403 or 404)
        res = client.get(
            f"/api/network/topology/customer/{kol_id}",
            headers=auth_header(tokens["noc"], "mumbai")
        )
        assert res.status_code in (403, 404)

    def test_cross_market_olt_ports_denied(self, tokens):
        db = SessionLocal()
        try:
            kol_olt = db.query(NetworkDevice).filter(
                NetworkDevice.market_id == "kolkata",
                NetworkDevice.device_type == "OLT"
            ).first()
            assert kol_olt is not None
            kol_olt_id = kol_olt.id
        finally:
            db.close()

        # Requesting Kolkata OLT ports with Mumbai market header must be rejected
        res = client.get(
            f"/api/network/olts/{kol_olt_id}/ports",
            headers=auth_header(tokens["noc"], "mumbai")
        )
        assert res.status_code in (403, 404)

    def test_deterministic_queries_no_random_changes(self, tokens):
        # Repeated queries must return identical results
        res1 = client.get("/api/network/olts", headers=auth_header(tokens["noc"], "mumbai")).json()
        res2 = client.get("/api/network/olts", headers=auth_header(tokens["noc"], "mumbai")).json()
        assert [o["id"] for o in res1] == [o["id"] for o in res2]
        assert [o["device_code"] for o in res1] == [o["device_code"] for o in res2]
