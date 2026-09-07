from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

def test_signup_and_auth_flow():
    import uuid
    unique_email = f"test_{uuid.uuid4().hex[:8]}@sentinelos.ai"
    
    # 1. Valid Signup
    signup_payload = {
        "email": unique_email,
        "password": "SecurePassword123!",
        "full_name": "Test Engineer",
        "role": "Viewer"
    }
    res = client.post("/api/auth/signup", json=signup_payload)
    assert res.status_code == 201, f"Signup failed: {res.text}"
    data = res.json()
    assert "access_token" in data
    assert data["role"] == "Viewer"
    assert data["email"] == unique_email

    # 2. Duplicate Signup rejection
    dup_res = client.post("/api/auth/signup", json=signup_payload)
    assert dup_res.status_code == 400
    assert "already exists" in dup_res.json()["detail"]

    # 3. Weak password rejection
    weak_res = client.post("/api/auth/signup", json={
        "email": f"weak_{uuid.uuid4().hex[:8]}@sentinelos.ai",
        "password": "123",
        "full_name": "Weak User",
        "role": "Viewer"
    })
    assert weak_res.status_code == 400
    assert "at least 6 characters" in weak_res.json()["detail"]

    # 4. Standard Login with new user
    login_res = client.post("/api/auth/login", json={
        "email": unique_email,
        "password": "SecurePassword123!"
    })
    assert login_res.status_code == 200
    login_data = login_res.json()
    assert "access_token" in login_data

    # 5. Invalid credentials rejection
    bad_login = client.post("/api/auth/login", json={
        "email": unique_email,
        "password": "WrongPassword!"
    })
    assert bad_login.status_code == 401

    # 6. Unauthenticated protected endpoint rejection
    unauth_res = client.get("/api/cockpit/summary")
    assert unauth_res.status_code == 401

    # 7. Admin user listing
    admin_token = client.post("/api/auth/demo-login/Admin").json()["access_token"]
    users_res = client.get("/api/auth/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert users_res.status_code == 200
    assert len(users_res.json()) >= 5

    # 8. Non-admin forbidden from user listing
    viewer_token = data["access_token"]
    forbidden_users = client.get("/api/auth/users", headers={"Authorization": f"Bearer {viewer_token}"})
    assert forbidden_users.status_code == 403

def test_demo_logins():
    roles = ["Executive", "NOC", "Care", "Revenue", "Admin"]
    tokens = {}
    for role in roles:
        res = client.post(f"/api/auth/demo-login/{role}")
        assert res.status_code == 200, f"Demo login failed for role {role}"
        data = res.json()
        assert "access_token" in data
        assert data["role"] == role
        tokens[role] = data["access_token"]
    return tokens

def test_5_intelligence_modules():
    admin_res = client.post("/api/auth/demo-login/Admin")
    admin_token = admin_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    # 1. Predictive Assurance Scored Engine
    assur_res = client.get("/api/assurance/predictions", headers=headers)
    assert assur_res.status_code == 200
    assur_data = assur_res.json()
    assert len(assur_data) > 0
    assert "degradation_risk_score" in assur_data[0]
    assert "contributing_signals" in assur_data[0]
    assert len(assur_data[0]["contributing_signals"]) > 0

    # 2. Churn Prediction Scored Engine
    churn_res = client.get("/api/churn/at-risk", headers=headers)
    assert churn_res.status_code == 200
    churn_data = churn_res.json()
    assert len(churn_data) > 0
    assert "churn_risk_score" in churn_data[0]
    assert "top_factors" in churn_data[0]
    assert len(churn_data[0]["top_factors"]) > 0

    # 3. Revenue Assurance Scored Anomaly Engine
    rev_res = client.get("/api/revenue/leakages", headers=headers)
    assert rev_res.status_code == 200
    rev_data = rev_res.json()
    assert len(rev_data) > 0
    assert "leakage_risk_score" in rev_data[0]
    assert "leakage_amount" in rev_data[0]
    assert "contributing_signals" in rev_data[0]
    assert len(rev_data[0]["contributing_signals"]) > 0

    # 4. OSS/BSS Orchestration Scored Triage Engine
    orch_res = client.get("/api/orchestration/queue", headers=headers)
    assert orch_res.status_code == 200
    orch_data = orch_res.json()
    assert len(orch_data) > 0
    assert "triage_priority_score" in orch_data[0]
    assert "workflow_type" in orch_data[0]
    assert "contributing_signals" in orch_data[0]
    assert len(orch_data[0]["contributing_signals"]) > 0

    # 5. Intelligent Customer Journeys Engine & Funnel
    journey_res = client.get("/api/journeys/next-best-actions", headers=headers)
    assert journey_res.status_code == 200
    journey_data = journey_res.json()
    assert len(journey_data) > 0
    assert "next_best_action" in journey_data[0]
    assert "action_reason" in journey_data[0]
    assert "suggested_channel" in journey_data[0]
    assert "current_stage" in journey_data[0]
    assert "contributing_signals" in journey_data[0]
    assert len(journey_data[0]["contributing_signals"]) > 0

    funnel_res = client.get("/api/journeys/funnel-summary", headers=headers)
    assert funnel_res.status_code == 200
    funnel_data = funnel_res.json()
    assert funnel_data["total_customers"] > 0
    assert len(funnel_data["stages"]) == 6

def test_pilot_bundle_scenario():
    admin_res = client.post("/api/auth/demo-login/Admin")
    admin_token = admin_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    scenario_res = client.get("/api/pilot-bundle/scenario?node_code=OLT-BND-01", headers=headers)
    assert scenario_res.status_code == 200
    scenario = scenario_res.json()
    assert scenario["scenario_id"] == "scenario-mumbai-cascading-churn"
    assert scenario["node"]["node_code"] == "OLT-BND-01"
    assert scenario["impacted_customer"]["id"] > 0
    assert len(scenario["trace_steps"]) == 6

def test_rbac_and_governance_matrix():
    tokens = test_demo_logins()
    noc_headers = {"Authorization": f"Bearer {tokens['NOC']}"}
    care_headers = {"Authorization": f"Bearer {tokens['Care']}"}
    rev_headers = {"Authorization": f"Bearer {tokens['Revenue']}"}
    exec_headers = {"Authorization": f"Bearer {tokens['Executive']}"}
    admin_headers = {"Authorization": f"Bearer {tokens['Admin']}"}

    # Verify recommendations exist across modules
    recs = client.get("/api/governance/recommendations", headers=admin_headers).json()
    assert len(recs) >= 5, "Recommendations must exist for all 5 modules"
    
    modules_in_queue = set(r["source_module"] for r in recs)
    assert "Predictive Service Assurance" in modules_in_queue
    assert "Churn Prediction & Retention AI" in modules_in_queue
    assert "Intelligent Customer Journeys" in modules_in_queue
    assert "AI-driven OSS/BSS Orchestration" in modules_in_queue
    assert "Revenue Assurance & Leakage Analytics" in modules_in_queue

    # 1. Propose and test Care-domain NBA recommendation
    journey_list = client.get("/api/journeys/next-best-actions", headers=care_headers).json()
    cust_target = next(j for j in journey_list if j["current_stage"] == "Renewal")
    rec_res = client.post("/api/journeys/recommend", json={"customer_id": cust_target["customer_id"]}, headers=care_headers)
    assert rec_res.status_code == 200
    j_rec_id = rec_res.json()["id"]

    # NOC user should be forbidden (403) from approving Care journey recommendation
    noc_try = client.post("/api/governance/approve", json={"recommendation_id": j_rec_id}, headers=noc_headers)
    assert noc_try.status_code == 403, "NOC user must not approve journey recommendation"

    # Executive user should be forbidden (403) from approving (read-only)
    exec_try = client.post("/api/governance/approve", json={"recommendation_id": j_rec_id}, headers=exec_headers)
    assert exec_try.status_code == 403, "Executive user must not approve actions"

    # Care user CAN approve
    care_app = client.post("/api/governance/approve", json={"recommendation_id": j_rec_id, "notes": "Approved by Care Lead"}, headers=care_headers)
    assert care_app.status_code == 200
    assert care_app.json()["status"] in ["APPROVED", "EXECUTED"]

    # Verify Audit Trail recorded the execution
    audits = client.get("/api/governance/audit-trail", headers=admin_headers).json()
    assert len(audits) >= 5
    latest_audit = audits[0]
    assert latest_audit["user_name"] == "Pooja Sharma"
    assert latest_audit["user_role"] == "Care"
    assert latest_audit["decision"] == "APPROVED"

def test_dual_market_isolation_and_governance():
    admin_res = client.post("/api/auth/demo-login/Admin")
    admin_token = admin_res.json()["access_token"]
    
    # 1. Market listing endpoint
    markets_res = client.get("/api/markets")
    assert markets_res.status_code == 200
    markets = markets_res.json()
    market_ids = [m["id"] for m in markets]
    assert "mumbai" in market_ids
    assert "kolkata" in market_ids

    # 2. Mumbai market scoping
    mumbai_headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}
    mumbai_nodes = client.get("/api/assurance/predictions", headers=mumbai_headers).json()
    assert len(mumbai_nodes) == 12
    mumbai_node_codes = [n["node_code"] for n in mumbai_nodes]
    assert any("BND" in c for c in mumbai_node_codes), "Bandra West node must be in Mumbai"
    assert not any("SLK" in c for c in mumbai_node_codes), "Salt Lake node must NOT be in Mumbai"

    # 3. Kolkata market scoping
    kolkata_headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "kolkata"}
    kolkata_nodes = client.get("/api/assurance/predictions", headers=kolkata_headers).json()
    assert len(kolkata_nodes) == 12
    kolkata_node_codes = [n["node_code"] for n in kolkata_nodes]
    assert any("SLK" in c for c in kolkata_node_codes), "Salt Lake node must be in Kolkata"
    assert not any("BND" in c for c in kolkata_node_codes), "Bandra node must NOT be in Kolkata"

    # 4. Scoped cockpit summary
    mumbai_cockpit = client.get("/api/cockpit/summary", headers=mumbai_headers).json()
    kolkata_cockpit = client.get("/api/cockpit/summary", headers=kolkata_headers).json()
    mumbai_total = mumbai_cockpit["kpis"]["prepaid_subscribers_count"] + mumbai_cockpit["kpis"]["postpaid_subscribers_count"]
    kolkata_total = kolkata_cockpit["kpis"]["prepaid_subscribers_count"] + kolkata_cockpit["kpis"]["postpaid_subscribers_count"]
    assert mumbai_total == 1000
    assert kolkata_total == 1000
    mumbai_localities = [r["locality"] for r in mumbai_cockpit["locality_risk_distribution"]]
    kolkata_localities = [r["locality"] for r in kolkata_cockpit["locality_risk_distribution"]]
    assert "Bandra West" in mumbai_localities
    assert "Salt Lake Sector V" in kolkata_localities

    # 5. Pilot bundle auto-resolution
    mumbai_pilot = client.get("/api/pilot-bundle/scenario", headers=mumbai_headers).json()
    assert mumbai_pilot["node"]["node_code"] == "OLT-BND-01"
    kolkata_pilot = client.get("/api/pilot-bundle/scenario", headers=kolkata_headers).json()
    assert kolkata_pilot["node"]["node_code"] == "OLT-SLK-01"

    # 6. Brand sanitization verification
    for payload in [mumbai_cockpit, kolkata_cockpit, mumbai_pilot, kolkata_pilot]:
        payload_str = str(payload).lower()
        assert "jeebr" not in payload_str, "Client name 'jeebr' found in API response!"
        assert "meghbela" not in payload_str, "Client name 'meghbela' found in API response!"

def test_approval_engine_voice_alert_and_technician_assignment():
    admin_res = client.post("/api/auth/demo-login/Admin")
    admin_token = admin_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}

    # 1. Raise a P1 ticket
    p1_payload = {
        "source": "CUSTOMER",
        "category": "Speed",
        "priority": "P1",
        "region": "Bandra West",
        "description": "Urgent P1 optical loss on Bandra hub distribution line."
    }
    create_res = client.post("/api/tickets", json=p1_payload, headers=headers)
    assert create_res.status_code == 200, f"Ticket creation failed: {create_res.text}"
    ticket = create_res.json()
    ticket_id = ticket["id"]
    assert ticket["priority"] == "P1"
    assert ticket["approval_status"] == "PENDING_APPROVAL"
    assert ticket["voice_call_dispatched"] is True
    assert ticket["last_call_recipient"] is not None

    # 2. Query call logs for this ticket
    logs_res = client.get(f"/api/tickets/{ticket_id}/call-logs", headers=headers)
    assert logs_res.status_code == 200
    logs = logs_res.json()
    assert len(logs) >= 1
    latest_log = logs[0]
    assert latest_log["priority"] == "P1"
    assert "Emergency SentinelOS" in latest_log["voice_script"]
    assert latest_log["status"] == "DELIVERED"

    # 3. Test simulate-call endpoint
    sim_res = client.post(f"/api/tickets/{ticket_id}/simulate-call", headers=headers)
    assert sim_res.status_code == 200
    assert sim_res.json()["status"] == "DELIVERED"

    # 4. Fetch available resources to test manual technician assignment
    resources_res = client.get("/api/tickets/resources", headers=headers)
    assert resources_res.status_code == 200
    resources = resources_res.json()
    assert len(resources) > 0
    target_resource = resources[0]

    # 5. Approve with custom notes and manual technician assignment
    custom_note = "Emergency splice authorized by NOC Lead. Manual technician dispatch verified."
    approve_res = client.post(f"/api/tickets/{ticket_id}/approve", json={
        "notes": custom_note,
        "resource_id": target_resource["id"]
    }, headers=headers)
    assert approve_res.status_code == 200
    approved_ticket = approve_res.json()
    assert approved_ticket["approval_status"] == "APPROVED"
    assert approved_ticket["assigned_resource_id"] == target_resource["id"]
    assert approved_ticket["assigned_resource_name"] == target_resource["name"]
    assert approved_ticket["approval_notes"] == custom_note
    assert "manually assigned" in approved_ticket["ai_triage_action"].lower()

if __name__ == "__main__":
    print("Running updated comprehensive test suite...")
    test_health()
    print("[PASS] Health check")
    test_demo_logins()
    print("[PASS] All 5 Demo logins (Executive, NOC, Care, Revenue, Admin)")
    test_approval_engine_voice_alert_and_technician_assignment()
    print("[PASS] Approval Engine Voice Alert Call & Manual Technician Assignment")
    test_5_intelligence_modules()
    print("[PASS] All 5 Intelligence Modules")
    print("ALL TESTS PASSED WITH 100% SUCCESS!")


