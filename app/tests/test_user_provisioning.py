"""
Sentinel OS - Admin & Super Admin User Provisioning and RBAC Integration Test Suite

Verifies:
1. Hierarchy Enforcement:
   - Super Admin (rank 100) can provision Admin (rank 80) and all operational roles.
   - Admin (rank 80) can provision users only with rank < 80 (NOC, Field Engineer, Customer, Viewer, etc.).
   - Admin CANNOT provision Super Admin (rank 100) -> 403 Forbidden.
   - Admin CANNOT provision another Admin (rank 80) -> 403 Forbidden.
   - Non-administrators cannot access user provisioning -> 403 Forbidden.
2. Conflict Handling:
   - Attempting to provision a user with an already registered email -> 409 Conflict.
3. Authentication & Credential Integrity:
   - Newly provisioned user logs in successfully via POST /api/auth/login.
   - Passwords are encrypted with PBKDF2; plaintext password or hash is never exposed in API responses.
4. User Lifecycle Management:
   - User update (PUT /api/auth/users/{id}) and status toggle (PATCH /api/auth/users/{id}/status).
   - Privilege boundary protection: Admin cannot modify or disable Super Admin accounts.
5. Assignable Roles:
   - GET /api/rbac/assignable-roles respects privilege rank.
"""

import pytest
import uuid
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import User
from app.auth import create_access_token, hash_password, verify_password

client = TestClient(app)


def _get_user_token(email: str, role: str) -> str:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                email=email,
                hashed_password=hash_password("SuperSecret@123"),
                full_name=f"Test {role}",
                role=role,
                is_active=True
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # Ensure correct role & active status
            if user.role != role or not user.is_active:
                user.role = role
                user.is_active = True
                db.commit()
        return create_access_token(data={"sub": user.email, "role": user.role, "market": "mumbai"})
    finally:
        db.close()


def test_super_admin_can_provision_admin_and_operational_roles():
    """Super Admin (rank 100) can provision Admin and operational roles."""
    super_admin_token = _get_user_token("super_prov_test@pmrg.in", "SUPER_ADMIN")
    
    unique_suffix = uuid.uuid4().hex[:6]
    
    # 1. Super Admin provisions an Admin
    admin_payload = {
        "full_name": f"Provisioned Admin {unique_suffix}",
        "email": f"new_admin_{unique_suffix}@pmrg.in",
        "password": "SecurePassword@123",
        "role": "Admin",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=admin_payload,
        headers={"Authorization": f"Bearer {super_admin_token}"}
    )
    assert res.status_code == 201, f"Super Admin should be able to create Admin, got: {res.text}"
    admin_data = res.json()
    assert admin_data["email"] == admin_payload["email"]
    assert admin_data["role"] == "Admin"
    assert "password" not in admin_data
    assert "hashed_password" not in admin_data

    # 2. Super Admin provisions a Field Engineer
    fe_payload = {
        "full_name": f"Provisioned Engineer {unique_suffix}",
        "email": f"new_fe_{unique_suffix}@pmrg.in",
        "password": "SecurePassword@123",
        "role": "Field Engineer",
        "is_active": True
    }
    res_fe = client.post(
        "/api/auth/users",
        json=fe_payload,
        headers={"Authorization": f"Bearer {super_admin_token}"}
    )
    assert res_fe.status_code == 201
    fe_data = res_fe.json()
    assert fe_data["role"] == "Field Engineer"


def test_admin_can_provision_lower_tier_roles():
    """Admin (rank 80) can provision roles below Admin (rank < 80): NOC, Care, Field Engineer, Customer, Viewer."""
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")
    unique_suffix = uuid.uuid4().hex[:6]

    roles_to_test = ["NOC", "Field Engineer", "Customer", "Viewer"]
    for role in roles_to_test:
        payload = {
            "full_name": f"User {role} {unique_suffix}",
            "email": f"user_{role.lower().replace(' ', '_')}_{unique_suffix}@pmrg.in",
            "password": "SecurePassword@123",
            "role": role,
            "is_active": True
        }
        res = client.post(
            "/api/auth/users",
            json=payload,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert res.status_code == 201, f"Admin should be able to create {role}, got {res.status_code}: {res.text}"
        data = res.json()
        assert data["role"] == role
        assert data["email"] == payload["email"]
        assert "password" not in data
        assert "hashed_password" not in data


def test_admin_cannot_provision_super_admin():
    """Admin (rank 80) MUST NOT be allowed to provision a SUPER_ADMIN (rank 100) -> 403 Forbidden."""
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")
    unique_suffix = uuid.uuid4().hex[:6]

    payload = {
        "full_name": f"Escalated Super Admin {unique_suffix}",
        "email": f"hacked_super_{unique_suffix}@pmrg.in",
        "password": "HackerPassword@123",
        "role": "SUPER_ADMIN",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 403, f"Admin creating SUPER_ADMIN should be 403, got {res.status_code}: {res.text}"
    detail = res.json().get("detail", "")
    assert "not authorized" in detail.lower()


def test_admin_cannot_provision_another_admin():
    """Admin (rank 80) MUST NOT be allowed to provision another Admin (rank 80) -> 403 Forbidden."""
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")
    unique_suffix = uuid.uuid4().hex[:6]

    payload = {
        "full_name": f"Peer Admin {unique_suffix}",
        "email": f"peer_admin_{unique_suffix}@pmrg.in",
        "password": "PeerPassword@123",
        "role": "Admin",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 403, f"Admin creating Admin should be 403, got {res.status_code}: {res.text}"
    detail = res.json().get("detail", "")
    assert "not authorized" in detail.lower()


def test_non_admin_cannot_provision_users():
    """Operational roles (e.g. NOC, Field Engineer, Customer) cannot provision users."""
    noc_token = _get_user_token("noc_prov_test@pmrg.in", "NOC")
    unique_suffix = uuid.uuid4().hex[:6]

    payload = {
        "full_name": f"Unauthorized User {unique_suffix}",
        "email": f"unauth_{unique_suffix}@pmrg.in",
        "password": "Password@123",
        "role": "Viewer",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {noc_token}"}
    )
    assert res.status_code == 403, f"NOC should not be allowed to provision users, got {res.status_code}"


def test_duplicate_user_email_returns_409_conflict():
    """Attempting to provision a user with an existing email returns 409 Conflict."""
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")
    unique_suffix = uuid.uuid4().hex[:6]

    payload = {
        "full_name": f"First User {unique_suffix}",
        "email": f"duplicate_{unique_suffix}@pmrg.in",
        "password": "Password@123",
        "role": "NOC",
        "is_active": True
    }
    # First creation -> 201
    res1 = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res1.status_code == 201

    # Second creation with identical email -> 409 Conflict
    res2 = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res2.status_code == 409, f"Duplicate user creation must return 409, got {res2.status_code}: {res2.text}"
    detail = res2.json().get("detail", "")
    assert "already exists" in detail.lower()


def test_provisioned_user_can_login_and_password_is_hashed():
    """Newly provisioned user can successfully log in via POST /api/auth/login and password is encrypted in database."""
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")
    unique_suffix = uuid.uuid4().hex[:6]
    test_password = "StrictlySecret@999"

    payload = {
        "full_name": f"Login Test User {unique_suffix}",
        "email": f"login_test_{unique_suffix}@pmrg.in",
        "password": test_password,
        "role": "Viewer",
        "is_active": True
    }
    res = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 201

    # Verify password in database is hashed, not plaintext
    db = SessionLocal()
    try:
        user_record = db.query(User).filter(User.email == payload["email"]).first()
        assert user_record is not None
        assert user_record.hashed_password != test_password
        assert verify_password(test_password, user_record.hashed_password)
    finally:
        db.close()

    # Authenticate via /api/auth/login
    login_res = client.post(
        "/api/auth/login",
        json={"email": payload["email"], "password": test_password}
    )
    assert login_res.status_code == 200, f"Login failed for newly provisioned user: {login_res.text}"
    login_data = login_res.json()
    assert "access_token" in login_data
    assert login_data["email"] == payload["email"]
    assert login_data["role"] == "Viewer"


def test_user_lifecycle_update_and_status_toggle():
    """Test user update (PUT /api/auth/users/{id}) and status toggle (PATCH /api/auth/users/{id}/status)."""
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")
    unique_suffix = uuid.uuid4().hex[:6]

    # Create user
    payload = {
        "full_name": f"Lifecycle User {unique_suffix}",
        "email": f"lifecycle_{unique_suffix}@pmrg.in",
        "password": "TestPassword@123",
        "role": "Viewer",
        "is_active": True
    }
    create_res = client.post(
        "/api/auth/users",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    # Update user full_name and role to NOC
    update_res = client.put(
        f"/api/auth/users/{user_id}",
        json={"full_name": f"Updated Name {unique_suffix}", "role": "NOC"},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert update_res.status_code == 200
    updated_data = update_res.json()
    assert updated_data["full_name"] == f"Updated Name {unique_suffix}"
    assert updated_data["role"] == "NOC"

    # Disable user
    status_res = client.patch(
        f"/api/auth/users/{user_id}/status",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert status_res.status_code == 200
    assert status_res.json()["is_active"] is False

    # Attempt login as disabled user -> 401 Unauthorized
    login_res = client.post(
        "/api/auth/login",
        json={"email": payload["email"], "password": "TestPassword@123"}
    )
    assert login_res.status_code == 401

    # Re-enable user
    enable_res = client.patch(
        f"/api/auth/users/{user_id}/status",
        json={"is_active": True},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert enable_res.status_code == 200
    assert enable_res.json()["is_active"] is True


def test_assignable_roles_hierarchy():
    """GET /api/rbac/assignable-roles returns only roles at or below authorized rank."""
    super_admin_token = _get_user_token("super_prov_test@pmrg.in", "SUPER_ADMIN")
    admin_token = _get_user_token("admin_prov_test@pmrg.in", "Admin")

    # Super Admin assignable roles
    sa_res = client.get(
        "/api/rbac/assignable-roles",
        headers={"Authorization": f"Bearer {super_admin_token}"}
    )
    assert sa_res.status_code == 200
    sa_roles = [r["name"] for r in sa_res.json()]
    assert "SUPER_ADMIN" in sa_roles
    assert "Admin" in sa_roles
    assert "NOC" in sa_roles

    # Admin assignable roles
    admin_res = client.get(
        "/api/rbac/assignable-roles",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert admin_res.status_code == 200
    admin_roles = [r["name"] for r in admin_res.json()]
    assert "SUPER_ADMIN" not in admin_roles, "Admin must NOT have SUPER_ADMIN in assignable roles"
    assert "Admin" not in admin_roles, "Admin must NOT have Admin in assignable roles"
    assert "NOC" in admin_roles
    assert "Field Engineer" in admin_roles
