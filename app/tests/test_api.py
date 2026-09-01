from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

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
    assert scenario["scenario_id"] == "scenario-bandra-cascading-churn"
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

    # Verify initial pending queue contains recommendations across modules
    pending_recs = client.get("/api/governance/recommendations?status=PENDING", headers=admin_headers).json()
    assert len(pending_recs) >= 5, "Pending recommendations must exist for all 5 modules"
    
    modules_in_queue = set(r["source_module"] for r in pending_recs)
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

if __name__ == "__main__":
    print("Running updated comprehensive test suite...")
    test_health()
    print("[PASS] Health check")
    test_demo_logins()
    print("[PASS] All 5 Demo logins (Executive, NOC, Care, Revenue, Admin)")
    test_5_intelligence_modules()
    print("[PASS] All 5 Intelligence Modules (Assurance, Churn, Revenue, Orchestration, Journeys)")
    test_pilot_bundle_scenario()
    print("[PASS] Pilot Bundle Connected E2E Trace Scenario")
    test_rbac_and_governance_matrix()
    print("[PASS] Governance RBAC Permissions Matrix & Audit Trail Execution")
    print("ALL TESTS PASSED WITH 100% SUCCESS!")

