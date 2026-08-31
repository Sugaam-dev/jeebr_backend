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

def test_4_scored_engines():
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

def test_rbac_and_governance():
    noc_res = client.post("/api/auth/demo-login/NOC")
    noc_token = noc_res.json()["access_token"]
    noc_headers = {"Authorization": f"Bearer {noc_token}"}

    rev_res = client.post("/api/auth/demo-login/Revenue")
    rev_token = rev_res.json()["access_token"]
    rev_headers = {"Authorization": f"Bearer {rev_token}"}

    # Propose Revenue Recommendation
    rev_list = client.get("/api/revenue/leakages", headers=rev_headers).json()
    rec_res = client.post("/api/revenue/recommend", json={"invoice_id": rev_list[0]["invoice_id"]}, headers=rev_headers)
    assert rec_res.status_code == 200
    rec_id = rec_res.json()["id"]

    # NOC user should be forbidden from approving Revenue recommendation
    noc_try = client.post("/api/governance/approve", json={"recommendation_id": rec_id}, headers=noc_headers)
    assert noc_try.status_code == 403, "NOC user must not approve revenue recommendation"

    # Revenue user CAN approve
    rev_approve = client.post("/api/governance/approve", json={"recommendation_id": rec_id, "notes": "Approved by Revenue Lead"}, headers=rev_headers)
    assert rev_approve.status_code == 200
    assert rev_approve.json()["status"] in ["APPROVED", "EXECUTED"]

if __name__ == "__main__":
    print("Running updated test suite...")
    test_health()
    print("[PASS] Health check")
    test_demo_logins()
    print("[PASS] All 5 Demo logins")
    test_4_scored_engines()
    print("[PASS] All 4 Flagship Scored Engines (Explainable Signals & Confidence)")
    test_rbac_and_governance()
    print("[PASS] Governance RBAC & Audit Execution")
    print("ALL TESTS PASSED!")
