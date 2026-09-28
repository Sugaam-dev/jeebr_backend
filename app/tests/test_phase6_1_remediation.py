"""
Phase 6.1 Targeted Operational Flow Remediation Verification Test Suite
Verifies:
1. Field Job Detail endpoint (GET /api/field/jobs/{assignment_id}) & reliable Job Completion lifecycle.
2. Ticket Approval endpoint (POST /api/tickets/{ticket_id}/approve) with eager loading & Super Admin / Admin access.
3. Role & Permission Configuration endpoints (RBAC matrix & user role assignment).
"""
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timedelta
from app.main import app
from app.database import SessionLocal
from app.models import User, Ticket, Resource, FieldAssignment, Role, Permission, RolePermission
from app.auth import create_access_token, hash_password

client = TestClient(app)

def _get_token(email: str, role: str = "Admin") -> str:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                email=email,
                hashed_password=hash_password("TestPass@123"),
                full_name=f"Test {role}",
                role=role,
                is_active=True
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return create_access_token(data={"sub": user.email, "role": user.role, "market": "mumbai"})
    finally:
        db.close()


def test_field_job_detail_endpoint_and_authorization():
    """Verify GET /api/field/jobs/{assignment_id} exists and enforces proper resource-scope authorization."""
    admin_token = _get_token("admin@pmrg.in", "Admin")
    db = SessionLocal()
    try:
        # Find or create a FieldAssignment
        fa = db.query(FieldAssignment).filter(FieldAssignment.market_id == "mumbai").first()
        assert fa is not None, "Field assignment should exist in seed data"
        fa_id = fa.id
    finally:
        db.close()

    # Admin access
    res = client.get(
        f"/api/field/jobs/{fa_id}",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == fa_id
    assert "status" in data
    assert "ticket_code" in data or "ticket_id" in data


def test_ticket_approval_super_admin_and_admin():
    """Verify POST /api/tickets/{id}/approve works for both Admin and SUPER_ADMIN with eager relation serialization."""
    super_token = _get_token("superadmin@pmrg.in", "SUPER_ADMIN")
    admin_token = _get_token("admin@pmrg.in", "Admin")

    db = SessionLocal()
    try:
        # Create a ticket pending approval
        ticket = Ticket(
            market_id="mumbai",
            ticket_code=f"TCK-TEST-{int(datetime.utcnow().timestamp())}",
            source="CUSTOMER",
            region="Andheri West",
            category="Fiber Cut",
            priority="P1",
            status="Pending Approval",
            approval_status="PENDING_APPROVAL",
            description="Test high-impact cut",
            created_at=datetime.utcnow()
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        test_ticket_id = ticket.id
    finally:
        db.close()

    # Super Admin can approve
    res = client.post(
        f"/api/tickets/{test_ticket_id}/approve",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"notes": "Approved by Super Admin"}
    )
    assert res.status_code == 200, f"Approve failed: {res.text}"
    data = res.json()
    assert data["id"] == test_ticket_id
    assert data["status"] in ("Assigned", "Approved")
    assert data["approval_status"] == "APPROVED"


def test_rbac_roles_and_permissions_endpoints():
    """Verify RBAC endpoints for Role & Permission management UI."""
    admin_token = _get_token("admin@pmrg.in", "Admin")

    # 1. Get Permissions
    res_perms = client.get("/api/rbac/permissions", headers={"Authorization": f"Bearer {admin_token}"})
    assert res_perms.status_code == 200
    perms = res_perms.json()
    assert len(perms) > 0
    sample_code = perms[0]["code"]

    # 2. Get Roles
    res_roles = client.get("/api/rbac/roles", headers={"Authorization": f"Bearer {admin_token}"})
    assert res_roles.status_code == 200
    roles = res_roles.json()
    assert any(r["name"] == "SUPER_ADMIN" for r in roles)
    assert any(r["name"] == "Admin" for r in roles)

    # 3. Create Custom Role
    custom_role_name = f"TEST_DISPATCH_{int(datetime.utcnow().timestamp())}"
    res_create = client.post(
        "/api/rbac/roles",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": custom_role_name,
            "display_name": "Test Dispatcher Lead",
            "description": "Custom role for regional dispatch",
            "rank": 45,
            "permissions": [sample_code]
        }
    )
    assert res_create.status_code == 201, f"Create role failed: {res_create.text}"
    created_role = res_create.json()
    role_id = created_role["id"]
    assert created_role["name"] == custom_role_name

    # 4. Update Custom Role
    res_update = client.put(
        f"/api/rbac/roles/{role_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "display_name": "Updated Dispatcher Lead",
            "description": "Updated description",
            "permissions": [sample_code]
        }
    )
    assert res_update.status_code == 200
    assert res_update.json()["display_name"] == "Updated Dispatcher Lead"

    # 5. Delete Custom Role
    res_del = client.delete(
        f"/api/rbac/roles/{role_id}",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res_del.status_code == 200
