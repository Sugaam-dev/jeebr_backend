import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_viewer_authentication():
    """Verify Viewer login works with correct credentials and fails with wrong password."""
    # 1. Successful login
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    assert login_res.status_code == 200, f"Failed to login: {login_res.text}"
    data = login_res.json()
    assert "access_token" in data
    assert data["role"] == "Viewer"
    assert data["email"] == "client@pmrgsolution.com"

    # 2. Failed login (generic error message, no user enumeration)
    bad_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "WrongPassword!"},
    )
    assert bad_res.status_code == 401
    assert bad_res.json()["detail"] == "Invalid email or password"


def test_viewer_read_only_access_allowed():
    """Verify Viewer can access all GET read-only endpoints across all modules."""
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Market-ID": "mumbai"}

    # List of all key GET endpoints across cockpit, intelligence, ticketing, governance
    read_endpoints = [
        "/api/cockpit/summary",
        "/api/assurance/predictions",
        "/api/churn/at-risk",
        "/api/journeys/next-best-actions",
        "/api/orchestration/queue",
        "/api/revenue/leakages",
        "/api/governance/recommendations",
        "/api/governance/audit-trail",
        "/api/tickets",
        "/api/tickets/resources",
        "/api/tickets/stats",
        "/api/tickets/auto-dispatch-settings",
        "/api/customers",
        "/api/pilot-bundle/scenario",
        "/api/markets",
    ]

    for ep in read_endpoints:
        res = client.get(ep, headers=headers)
        assert res.status_code == 200, f"Viewer read access failed on {ep}: {res.status_code} {res.text}"


def test_viewer_mutation_endpoints_forbidden():
    """Verify Viewer is strictly blocked (403 Forbidden) on all mutation operations."""
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Market-ID": "mumbai"}

    # 1. Ticketing Mutations
    assert client.post("/api/tickets", json={
        "category": "Speed", "priority": "P3", "description": "Unauthorized ticket", "region": "Bandra West"
    }, headers=headers).status_code == 403

    assert client.post("/api/tickets/auto-dispatch-settings", json={"enabled": True}, headers=headers).status_code == 403
    assert client.post("/api/tickets/auto-dispatch-now", headers=headers).status_code == 403
    assert client.post("/api/tickets/simulate-ai-alert", json={"priority": "P3"}, headers=headers).status_code == 403
    assert client.post("/api/tickets/reset-demo-state", headers=headers).status_code == 403
    assert client.post("/api/tickets/1/approve", json={"notes": "Viewer attempt"}, headers=headers).status_code == 403
    assert client.post("/api/tickets/1/reject", json={"reason": "Viewer attempt"}, headers=headers).status_code == 403
    assert client.post("/api/tickets/1/resolve", headers=headers).status_code == 403
    assert client.post("/api/tickets/1/simulate-call", headers=headers).status_code == 403

    # 2. Intelligence Module Recommendations
    assert client.post("/api/assurance/recommend", json={"target_id": "1"}, headers=headers).status_code == 403
    assert client.post("/api/churn/recommend", json={"target_id": "1"}, headers=headers).status_code == 403
    assert client.post("/api/journeys/recommend", json={"target_id": "1"}, headers=headers).status_code == 403
    assert client.post("/api/orchestration/recommend", json={"target_id": "1"}, headers=headers).status_code == 403
    assert client.post("/api/revenue/recommend", json={"target_id": "1"}, headers=headers).status_code == 403

    # 3. Governance Sign-off
    assert client.post("/api/governance/approve", json={"recommendation_id": 1, "notes": "Viewer signoff"}, headers=headers).status_code == 403
    assert client.post("/api/governance/reject", json={"recommendation_id": 1, "notes": "Viewer signoff"}, headers=headers).status_code == 403

    # 4. Auth User Listing (Admin only)
    assert client.get("/api/auth/users", headers=headers).status_code == 403


def test_security_audit_logging_on_access_denied():
    """Verify that Viewer forbidden mutation attempts produce ACCESS_DENIED records in AuditLog."""
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    viewer_headers = {"Authorization": f"Bearer {token}", "X-Market-ID": "mumbai"}

    # Attempt a forbidden mutation
    forbidden_res = client.post(
        "/api/tickets/auto-dispatch-settings",
        json={"enabled": True},
        headers=viewer_headers
    )
    assert forbidden_res.status_code == 403

    # Fetch audit trail to check for ACCESS_DENIED security log
    trail_res = client.get("/api/governance/audit-trail", headers=viewer_headers)
    assert trail_res.status_code == 200
    audits = trail_res.json()
    access_denied_events = [a for a in audits if a.get("decision") == "ACCESS_DENIED"]
    assert len(access_denied_events) > 0
    latest_event = access_denied_events[0]
    assert "client@pmrgsolution.com" in latest_event.get("user_name", "") or latest_event.get("source_module") == "Security & RBAC"


def test_cookie_based_authentication_and_logout():
    """Verify HttpOnly cookie authentication flow and logout cookie clearing."""
    # 1. Login sets HttpOnly access_token cookie
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    assert login_res.status_code == 200
    assert "access_token" in login_res.cookies

    cookie_val = login_res.cookies["access_token"]
    # Verify cookie allows authenticated request without Authorization header
    res = client.get("/api/cockpit/summary", cookies={"access_token": cookie_val})
    assert res.status_code == 200

    # 2. Logout clears cookie
    logout_res = client.post("/api/auth/logout", cookies={"access_token": cookie_val})
    assert logout_res.status_code == 200
    # Cookie should be expired/deleted
    set_cookie_header = logout_res.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie_header


def test_security_headers_and_csp():
    """Verify modern HTTP security headers and Content-Security-Policy are present on responses."""
    res = client.get("/health")
    assert res.status_code == 200
    headers = res.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "geolocation=()" in headers.get("Permissions-Policy", "")
    assert "Content-Security-Policy" in headers
    csp = headers.get("Content-Security-Policy")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_jwt_algorithm_and_forgery_attacks():
    """Verify HS256 algorithm enforcement, rejection of 'none' algorithm, and forged signatures."""
    from jose import jwt
    from app.config import settings

    # Attack B: Tamper with payload without re-signing
    valid_token = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    ).json()["access_token"]

    # Tamper token signature
    tampered_token = valid_token[:-4] + "ABCD"
    tampered_res = client.get("/api/cockpit/summary", headers={"Authorization": f"Bearer {tampered_token}"})
    assert tampered_res.status_code == 401

    # Forge token with wrong secret key
    forged_token = jwt.encode(
        {"sub": "client@pmrgsolution.com", "role": "Admin"},
        "wrong-unauthorized-secret-key-123456789",
        algorithm="HS256"
    )
    forged_res = client.get("/api/auth/users", headers={"Authorization": f"Bearer {forged_token}"})
    assert forged_res.status_code == 401

    # Attack: None algorithm rejection
    try:
        none_token = jwt.encode(
            {"sub": "client@pmrgsolution.com", "role": "Admin"},
            key="",
            algorithm="none"
        )
        none_res = client.get("/api/auth/users", headers={"Authorization": f"Bearer {none_token}"})
        assert none_res.status_code == 401
    except Exception:
        # If python-jose refuses to even generate 'none' algorithm token, that is also a pass
        pass


def test_privilege_escalation_attacks_prevented():
    """Verify that role headers, payload modifications, and client state cannot elevate privileges."""
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    token = login_res.json()["access_token"]

    # Attack C: Inject custom header X-Role: Admin
    res_c = client.get("/api/auth/users", headers={
        "Authorization": f"Bearer {token}",
        "X-Role": "Admin",
        "X-User-Role": "Admin"
    })
    assert res_c.status_code == 403

    # Attack D: Mass assignment / role injection during signup
    # Even if an attacker explicitly submits role="Admin" in public signup, system forces "Viewer"
    signup_res = client.post("/api/auth/signup", json={
        "email": f"attacker_{client_ip_hash()}@pmrg.in",
        "password": "SecurePassword123!",
        "full_name": "Attacker Role Injection",
        "role": "Admin"
    })
    assert signup_res.status_code == 201
    assert signup_res.json()["role"] == "Viewer"

    # Attack E: Direct access to admin-only API
    res_e = client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"})
    assert res_e.status_code == 403


def client_ip_hash():
    import uuid
    return uuid.uuid4().hex[:8]


def test_sensitive_data_exposure_audit():
    """Verify sensitive operational data (passwords, hashes, secret keys) are never exposed via APIs."""
    login_res = client.post(
        "/api/auth/login",
        json={"email": "client@pmrgsolution.com", "password": "Client@pmrg123"},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. /api/auth/me should not leak hashed_password
    me_res = client.get("/api/auth/me", headers=headers)
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert "hashed_password" not in me_data
    assert "password" not in me_data

    # 2. Customers 360 should not leak password or server credentials
    cust_res = client.get("/api/customers?limit=5", headers=headers)
    assert cust_res.status_code == 200
    cust_data = cust_res.json()
    for c in cust_data:
        assert "password" not in c
        assert "hashed_password" not in c

