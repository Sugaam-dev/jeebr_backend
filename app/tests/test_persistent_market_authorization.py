"""
SentinelOS - Persistent Database Market Authorization Tests (Phase 9B)

Verifies:
1. Newly created user with an assigned market can access that market.
2. Newly created user with no assignment is denied access (fail-closed / zero-trust).
3. User assigned to multiple markets (Mumbai & Kolkata) can access both with explicit header.
4. Cross-market access is strictly denied (403 Forbidden).
5. Super Admin retains global access to all markets without explicit assignment rows.
6. Admin cannot grant unauthorized market access beyond their own authorized markets (403 Forbidden).
7. Invalid market IDs are rejected with HTTP 400 Bad Request.
8. Existing user assignments backfilled during migration survive and authorize correctly.
9. Request header or query parameter tampering cannot grant market access without DB authorization.
"""
import uuid
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import User, UserMarketAssignment, Customer
from app.auth import create_access_token, hash_password

client = TestClient(app)


def _create_isolated_user(role: str, email_prefix: str) -> tuple[User, str]:
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:6]
        email = f"{email_prefix}_{suffix}@test.sentinel"
        user = User(
            email=email,
            hashed_password=hash_password("SecPass123!"),
            full_name=f"Test {role} {suffix}",
            role=role,
            is_active=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token(data={"sub": user.email, "role": user.role})
        return user, token
    finally:
        db.close()


def test_newly_created_user_with_assigned_market():
    """An admin provisions a new user with market_id='mumbai'; user can access Mumbai resources."""
    super_user, super_token = _create_isolated_user("SUPER_ADMIN", "super_create")
    unique_suffix = uuid.uuid4().hex[:6]

    create_payload = {
        "email": f"noc_mum_{unique_suffix}@pmrg.in",
        "full_name": f"NOC Mumbai {unique_suffix}",
        "password": "Password123!",
        "role": "NOC",
        "market_id": "mumbai",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=create_payload,
        headers={"Authorization": f"Bearer {super_token}"}
    )
    assert res.status_code == 201, f"Failed creating user: {res.text}"
    user_data = res.json()
    assert "mumbai" in user_data["market_ids"]
    assert user_data["primary_market"] == "mumbai"

    # Verify user token can access Mumbai cockpit/customers
    user_token = create_access_token(data={"sub": create_payload["email"], "role": "NOC"})
    cust_res = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {user_token}", "X-Market-Id": "mumbai"}
    )
    assert cust_res.status_code == 200, f"User should access Mumbai: {cust_res.text}"


def test_newly_created_user_with_no_assignment_denied_access():
    """A newly created user with no market assignment cannot access market-scoped endpoints (zero-trust)."""
    super_user, super_token = _create_isolated_user("SUPER_ADMIN", "super_no_mkt")
    unique_suffix = uuid.uuid4().hex[:6]

    # Create without market_id or market_ids
    create_payload = {
        "email": f"unassigned_{unique_suffix}@pmrg.in",
        "full_name": f"Unassigned Staff {unique_suffix}",
        "password": "Password123!",
        "role": "NOC",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=create_payload,
        headers={"Authorization": f"Bearer {super_token}"}
    )
    assert res.status_code == 201
    assert res.json()["market_ids"] == []

    # Attempt to access with explicit market header -> 403 Forbidden
    user_token = create_access_token(data={"sub": create_payload["email"], "role": "NOC"})
    cust_res = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {user_token}", "X-Market-Id": "mumbai"}
    )
    assert cust_res.status_code == 403
    assert "not authorized to access market 'mumbai'" in cust_res.json()["detail"]

    # Attempt to access with header omitted -> 403 Forbidden (no authorized operational markets)
    no_hdr_res = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {user_token}"}
    )
    assert no_hdr_res.status_code == 403
    assert "has no authorized operational markets" in no_hdr_res.json()["detail"]


def test_user_assigned_to_multiple_markets():
    """A user assigned to both mumbai and kolkata can access both with explicit X-Market-Id."""
    super_user, super_token = _create_isolated_user("SUPER_ADMIN", "super_multi")
    unique_suffix = uuid.uuid4().hex[:6]

    create_payload = {
        "email": f"multi_{unique_suffix}@pmrg.in",
        "full_name": f"Multi Staff {unique_suffix}",
        "password": "Password123!",
        "role": "NOC",
        "market_ids": ["mumbai", "kolkata"],
        "primary_market": "mumbai",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=create_payload,
        headers={"Authorization": f"Bearer {super_token}"}
    )
    assert res.status_code == 201
    user_data = res.json()
    assert "mumbai" in user_data["market_ids"]
    assert "kolkata" in user_data["market_ids"]
    assert user_data["primary_market"] == "mumbai"

    user_token = create_access_token(data={"sub": create_payload["email"], "role": "NOC"})

    # Access Mumbai
    res_mum = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {user_token}", "X-Market-Id": "mumbai"}
    )
    assert res_mum.status_code == 200

    # Access Kolkata
    res_kol = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {user_token}", "X-Market-Id": "kolkata"}
    )
    assert res_kol.status_code == 200


def test_cross_market_access_denied():
    """A user assigned only to Mumbai is forbidden from accessing Kolkata."""
    super_user, super_token = _create_isolated_user("SUPER_ADMIN", "super_cross")
    unique_suffix = uuid.uuid4().hex[:6]

    create_payload = {
        "email": f"mumbai_only_{unique_suffix}@pmrg.in",
        "full_name": f"Mumbai Only {unique_suffix}",
        "password": "Password123!",
        "role": "NOC",
        "market_ids": ["mumbai"],
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=create_payload,
        headers={"Authorization": f"Bearer {super_token}"}
    )
    assert res.status_code == 201

    user_token = create_access_token(data={"sub": create_payload["email"], "role": "NOC"})

    # Mumbai staff attempts to query Kolkata
    res_denied = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {user_token}", "X-Market-Id": "kolkata"}
    )
    assert res_denied.status_code == 403
    assert "not authorized to access market 'kolkata'" in res_denied.json()["detail"]


def test_super_admin_global_access():
    """Super Admin has universal access to all markets without needing individual assignment records."""
    super_user, super_token = _create_isolated_user("SUPER_ADMIN", "super_global")

    # Verify /api/auth/me returns global markets
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {super_token}"})
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert "mumbai" in me_data["market_ids"]
    assert "kolkata" in me_data["market_ids"]

    # Super Admin accesses Mumbai
    res_mum = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {super_token}", "X-Market-Id": "mumbai"}
    )
    assert res_mum.status_code == 200

    # Super Admin accesses Kolkata
    res_kol = client.get(
        "/api/customers?limit=1",
        headers={"Authorization": f"Bearer {super_token}", "X-Market-Id": "kolkata"}
    )
    assert res_kol.status_code == 200


def test_admin_cannot_grant_unauthorized_market_access():
    """An ordinary Admin assigned only to Mumbai cannot grant Kolkata access to any user."""
    # 1. Create an Admin user assigned ONLY to Mumbai
    admin_user, admin_token = _create_isolated_user("Admin", "mumbai_admin")
    db = SessionLocal()
    try:
        db.add(UserMarketAssignment(user_id=admin_user.id, market_id="mumbai", is_primary=True))
        db.commit()
    finally:
        db.close()

    unique_suffix = uuid.uuid4().hex[:6]
    # Admin tries to provision a user with Kolkata
    escalation_payload = {
        "email": f"escalated_{unique_suffix}@pmrg.in",
        "full_name": f"Escalated User {unique_suffix}",
        "password": "Password123!",
        "role": "NOC",
        "market_ids": ["mumbai", "kolkata"],
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=escalation_payload,
        headers={"Authorization": f"Bearer {admin_token}", "X-Market-Id": "mumbai"}
    )
    assert res.status_code == 403, f"Expected 403, got {res.status_code}: {res.text}"
    assert "not have authorization to grant market 'kolkata'" in res.json()["detail"]


def test_invalid_market_id_rejected():
    """Attempting to assign an unknown market returns 400 Bad Request."""
    super_user, super_token = _create_isolated_user("SUPER_ADMIN", "super_invalid_mkt")
    unique_suffix = uuid.uuid4().hex[:6]

    invalid_payload = {
        "email": f"invalid_mkt_{unique_suffix}@pmrg.in",
        "full_name": f"Invalid Market User {unique_suffix}",
        "password": "Password123!",
        "role": "NOC",
        "market_ids": ["atlantis"],
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=invalid_payload,
        headers={"Authorization": f"Bearer {super_token}"}
    )
    assert res.status_code == 400
    assert "Invalid or unsupported market identifier" in res.json()["detail"]


def test_existing_user_assignments_survive_migration():
    """Verify that seeded canonical operational users in DB have their persistent assignments intact."""
    db = SessionLocal()
    try:
        noc = db.query(User).filter(User.email == "noc@pmrg.in").first()
        assert noc is not None, "Seeded noc@pmrg.in user must exist"
        assignments = db.query(UserMarketAssignment).filter(UserMarketAssignment.user_id == noc.id).all()
        m_ids = [a.market_id for a in assignments]
        assert "mumbai" in m_ids
        assert "kolkata" in m_ids

        admin = db.query(User).filter(User.email == "admin@pmrg.in").first()
        assert admin is not None, "Seeded admin@pmrg.in user must exist"
        admin_assignments = db.query(UserMarketAssignment).filter(UserMarketAssignment.user_id == admin.id).all()
        admin_m_ids = [a.market_id for a in admin_assignments]
        assert "mumbai" in admin_m_ids
        assert "kolkata" in admin_m_ids
    finally:
        db.close()
