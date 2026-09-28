"""
Phase 6 Targeted RBAC, Authorization Consistency & Engineer Workload Remediation Tests
Validates:
1. Registration Role Assignment Matrix (Issue #1, Sections 6-7)
2. RBAC Hierarchy & Permissions (Sections 2-5, 8-11)
3. Multi-Session Authorization & IDOR Resource-Scope Protections (Issue #2, Sections 12-14)
4. Engineer Workload & Capacity Enforcement (Issue #3, Sections 20-25)
"""
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.main import app
from app.database import SessionLocal
from app.models import User, Resource, FieldAssignment, Ticket, Customer, Role, Permission
from app.services.workload_service import (
    calculate_engineer_workload,
    reserve_engineer_capacity,
    ACTIVE_FIELD_STATES,
    TERMINAL_FIELD_STATES,
    get_active_jobs_count
)

client = TestClient(app)

def _random_suffix():
    return uuid.uuid4().hex[:6]

def _login(email, password="admin123"):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed: {res.text}"
    return res.json()["access_token"]


# ============================================================================
# 1. ISSUE #1: REGISTRATION ROLE ASSIGNMENT MATRIX
# ============================================================================

def test_registration_allowed_roles_succeed():
    """Customer and Viewer self-registration must succeed with exact role preserved."""
    suffix = _random_suffix()
    
    # Customer
    c_res = client.post("/api/auth/signup", json={
        "full_name": f"Customer Test {suffix}",
        "email": f"cust_{suffix}@test.com",
        "password": "Password123!",
        "role": "Customer"
    })
    assert c_res.status_code == 201
    assert c_res.json()["role"] == "Customer"
    assert c_res.json()["rank"] == 20

    # Viewer
    v_res = client.post("/api/auth/signup", json={
        "full_name": f"Viewer Test {suffix}",
        "email": f"viewer_{suffix}@test.com",
        "password": "Password123!",
        "role": "Viewer"
    })
    assert v_res.status_code == 201
    assert v_res.json()["role"] == "Viewer"
    assert v_res.json()["rank"] == 10


def test_registration_privileged_roles_rejected_with_403():
    """Privileged roles must be rejected with 403 Forbidden upon self-registration."""
    suffix = _random_suffix()
    forbidden_roles = ["SUPER_ADMIN", "Admin", "NOC", "Care", "Revenue", "Executive", "Field Engineer"]
    
    for r in forbidden_roles:
        res = client.post("/api/auth/signup", json={
            "full_name": f"Attacker {r}",
            "email": f"attacker_{r.lower().replace(' ', '_')}_{suffix}@test.com",
            "password": "Password123!",
            "role": r
        })
        assert res.status_code == 403, f"Expected 403 for role '{r}', got {res.status_code}: {res.text}"
        assert "cannot be self-registered" in res.json()["detail"]


# ============================================================================
# 2. RBAC HIERARCHY & PRIVILEGE BOUNDARIES
# ============================================================================

def test_super_admin_universal_authority_and_bootstrap():
    """Super Admin exists, can list permissions and manage roles."""
    token = _login("superadmin@pmrg.in", "admin123")
    headers = {"Authorization": f"Bearer {token}"}
    
    # Check permissions
    p_res = client.get("/api/rbac/permissions", headers=headers)
    assert p_res.status_code == 200
    perms = p_res.json()
    assert len(perms) >= 29

    # Check roles
    r_res = client.get("/api/rbac/roles", headers=headers)
    assert r_res.status_code == 200
    roles = r_res.json()
    role_names = [role["name"] for role in roles]
    assert "SUPER_ADMIN" in role_names
    assert "Admin" in role_names


def test_admin_cannot_create_or_target_super_admin():
    """Admin (rank 80) cannot create Super Admin or assign Super Admin role."""
    admin_token = _login("admin@pmrg.in", "admin123")
    headers = {"Authorization": f"Bearer {admin_token}"}
    suffix = _random_suffix()

    # Attempt to create user with SUPER_ADMIN role -> 403
    res_user = client.post("/api/auth/users", json={
        "full_name": f"Super Admin Attempt {suffix}",
        "email": f"fake_sa_{suffix}@test.com",
        "password": "Password123!",
        "role": "SUPER_ADMIN"
    }, headers=headers)
    assert res_user.status_code == 403
    assert "authority to create user accounts with role 'SUPER_ADMIN'" in res_user.json()["detail"]

    # Attempt to create a role with rank 100 -> 403
    res_role = client.post("/api/rbac/roles", json={
        "name": f"Supreme God {suffix}",
        "display_name": f"Supreme God {suffix}",
        "rank": 100,
        "description": "Exploit attempt",
        "permissions": ["system.config.manage"]
    }, headers=headers)
    assert res_role.status_code == 403


# ============================================================================
# 3. ISSUE #2: MULTI-SESSION AUTHORIZATION & RESOURCE-SCOPE (IDOR)
# ============================================================================

def test_customer_ticket_isolation():
    """Customer cannot see other customers' tickets in list or by ID."""
    cust_token = _login("customer@pmrg.in", "admin123")
    headers = {"Authorization": f"Bearer {cust_token}"}

    # In tickets list, all returned tickets must belong to the customer
    res = client.get("/api/tickets", headers=headers)
    assert res.status_code == 200
    tickets = res.json()
    
    db = SessionLocal()
    try:
        cust = db.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
        for t in tickets:
            if t.get("customer_id"):
                assert t["customer_id"] == cust.id

        # Attempt to access a ticket belonging to another customer
        other_ticket = db.query(Ticket).filter(Ticket.customer_id != cust.id).first()
        if other_ticket:
            idor_res = client.get(f"/api/tickets/{other_ticket.id}", headers=headers)
            assert idor_res.status_code == 403
            assert "only view your own tickets" in idor_res.json()["detail"]
    finally:
        db.close()


def test_field_engineer_job_isolation():
    """Field Engineer cannot view unassigned tickets or other engineers' jobs."""
    eng_token = _login("rahul.field@pmrg.in", "admin123")
    headers = {"Authorization": f"Bearer {eng_token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        # Find a ticket assigned to a different engineer
        other_ticket = db.query(Ticket).filter(
            Ticket.assigned_resource_id.isnot(None),
            Ticket.assigned_resource_id != rahul.id
        ).first()

        if other_ticket:
            res = client.get(f"/api/tickets/{other_ticket.id}", headers=headers)
            assert res.status_code == 403
            assert "only view your own assigned jobs" in res.json()["detail"]
    finally:
        db.close()


# ============================================================================
# 4. ISSUE #3: ENGINEER WORKLOAD & CAPACITY ENFORCEMENT
# ============================================================================

def test_workload_active_states_vs_terminal_states():
    """Verify that only ACTIVE_FIELD_STATES consume capacity, and TERMINAL_FIELD_STATES do not."""
    db = SessionLocal()
    try:
        suffix = _random_suffix()
        res = Resource(
            name=f"Capacity Test Tech {suffix}",
            email=f"tech_{suffix}@pmrg.in",
            phone="9820099999",
            resource_type="FIELD_ENGINEER",
            region="Bandra West",
            market_id="mumbai",
            status="Available",
            max_capacity=3,
            active_tickets_count=0
        )
        db.add(res)

        # Create 4 test tickets for the assignments
        t1 = Ticket(ticket_code=f"TKT-TEST-{suffix}-1", description="Test 1", category="Speed", priority="P3", status="Assigned", region="Bandra West", market_id="mumbai")
        t2 = Ticket(ticket_code=f"TKT-TEST-{suffix}-2", description="Test 2", category="Speed", priority="P3", status="Assigned", region="Bandra West", market_id="mumbai")
        t3 = Ticket(ticket_code=f"TKT-TEST-{suffix}-3", description="Test 3", category="Speed", priority="P3", status="Assigned", region="Bandra West", market_id="mumbai")
        t4 = Ticket(ticket_code=f"TKT-TEST-{suffix}-4", description="Test 4", category="Speed", priority="P3", status="Assigned", region="Bandra West", market_id="mumbai")
        db.add_all([t1, t2, t3, t4])
        db.commit()
        db.refresh(res)

        # Initially 0 active jobs
        workload = calculate_engineer_workload(db, res)
        assert workload["active_jobs"] == 0
        assert workload["available_capacity"] == 3
        assert workload["capacity_status"] == "AVAILABLE"

        # Add 2 active jobs
        fa1 = FieldAssignment(
            market_id="mumbai",
            ticket_id=t1.id,
            engineer_id=res.id,
            status="ASSIGNED"
        )
        fa2 = FieldAssignment(
            market_id="mumbai",
            ticket_id=t2.id,
            engineer_id=res.id,
            status="WORKING"
        )
        # Add 1 terminal job
        fa3 = FieldAssignment(
            market_id="mumbai",
            ticket_id=t3.id,
            engineer_id=res.id,
            status="COMPLETED"
        )
        db.add_all([fa1, fa2, fa3])
        db.commit()

        # Should only count 2 active jobs
        workload2 = calculate_engineer_workload(db, res)
        assert workload2["active_jobs"] == 2
        assert workload2["available_capacity"] == 1
        assert workload2["capacity_status"] == "AVAILABLE"

        # Add 1 more active job -> reaches max 3
        fa4 = FieldAssignment(
            market_id="mumbai",
            ticket_id=t4.id,
            engineer_id=res.id,
            status="EN_ROUTE"
        )
        db.add(fa4)
        db.commit()

        workload3 = calculate_engineer_workload(db, res)
        assert workload3["active_jobs"] == 3
        assert workload3["available_capacity"] == 0
        assert workload3["capacity_status"] == "AT_CAPACITY"

        # Cleanup
        db.delete(fa1)
        db.delete(fa2)
        db.delete(fa3)
        db.delete(fa4)
        db.delete(t1)
        db.delete(t2)
        db.delete(t3)
        db.delete(t4)
        db.delete(res)
        db.commit()
    finally:
        db.close()


def test_capacity_rejection_returns_409_conflict():
    """Verify that assigning a job to an engineer at capacity raises 409 Conflict with structured payload."""
    db = SessionLocal()
    try:
        suffix = _random_suffix()
        res = Resource(
            name=f"Full Tech {suffix}",
            email=f"full_{suffix}@pmrg.in",
            phone="9820088888",
            resource_type="FIELD_ENGINEER",
            region="Bandra West",
            market_id="mumbai",
            status="Busy",
            max_capacity=2,
            active_tickets_count=2
        )
        t_a = Ticket(ticket_code=f"TKT-FULL-{suffix}-A", description="Full A", category="Speed", priority="P3", status="Assigned", region="Bandra West", market_id="mumbai")
        t_b = Ticket(ticket_code=f"TKT-FULL-{suffix}-B", description="Full B", category="Speed", priority="P3", status="Assigned", region="Bandra West", market_id="mumbai")
        db.add_all([res, t_a, t_b])
        db.commit()
        db.refresh(res)

        fa1 = FieldAssignment(
            market_id="mumbai",
            ticket_id=t_a.id,
            engineer_id=res.id,
            status="WORKING"
        )
        fa2 = FieldAssignment(
            market_id="mumbai",
            ticket_id=t_b.id,
            engineer_id=res.id,
            status="ARRIVED"
        )
        db.add_all([fa1, fa2])
        db.commit()

        # Attempt to reserve capacity via API
        admin_token = _login("admin@pmrg.in", "admin123")
        headers = {"Authorization": f"Bearer {admin_token}"}

        # Raise a test ticket to approve
        raise_res = client.post("/api/tickets", json={
            "category": "Speed",
            "priority": "P2",
            "description": f"Test capacity rejection ticket {suffix}",
            "region": "Bandra West"
        }, headers=headers)
        assert raise_res.status_code == 200
        test_ticket = raise_res.json()

        # Approve and manually assign to the at-capacity tech -> 409 Conflict
        appr_res = client.post(f"/api/tickets/{test_ticket['id']}/approve", json={
            "notes": "Testing capacity rejection",
            "resource_id": res.id
        }, headers=headers)

        assert appr_res.status_code == 409, f"Expected 409, got {appr_res.status_code}: {appr_res.text}"
        detail = appr_res.json()["detail"]
        assert detail["code"] == "ENGINEER_AT_CAPACITY"
        assert detail["active_jobs"] >= 2
        assert detail["max_active_jobs"] == 2

        # Cleanup
        db.delete(fa1)
        db.delete(fa2)
        db.delete(t_a)
        db.delete(t_b)
        db.delete(res)
        db.commit()
    finally:
        db.close()


def test_capacity_configuration_endpoint():
    """Verify PUT /api/field-operations/engineers/{id}/capacity updates capacity."""
    admin_token = _login("admin@pmrg.in", "admin123")
    headers = {"Authorization": f"Bearer {admin_token}"}

    db = SessionLocal()
    try:
        suffix = _random_suffix()
        res = Resource(
            name=f"Config Tech {suffix}",
            email=f"config_{suffix}@pmrg.in",
            phone="9820077777",
            resource_type="FIELD_ENGINEER",
            region="Bandra West",
            market_id="mumbai",
            status="Available",
            max_capacity=5,
            active_tickets_count=0
        )
        db.add(res)
        db.commit()
        db.refresh(res)

        put_res = client.put(f"/api/field-operations/engineers/{res.id}/capacity", json={
            "max_capacity": 12
        }, headers=headers)
        assert put_res.status_code == 200
        data = put_res.json()
        assert data["max_capacity"] == 12
        assert data["max_active_jobs"] == 12

        # Verify DB updated
        db.refresh(res)
        assert res.max_capacity == 12

        # Cleanup
        db.delete(res)
        db.commit()
    finally:
        db.close()
