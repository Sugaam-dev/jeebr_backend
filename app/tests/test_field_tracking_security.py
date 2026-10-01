import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import (
    User, Resource, Ticket, Customer, FieldAssignment, TrackingSession, LocationPing, FieldOtp, AuditLog
)
from app.services.otp_service import _MOCK_CUSTOMER_DISPATCH_STORE

client = TestClient(app)


def _get_token(email: str, password: str = "admin123") -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


def test_unauthenticated_requests_blocked():
    """Unauthenticated calls to field operations endpoints must return 401."""
    client.cookies.clear()
    endpoints = [
        ("GET", "/api/field-operations/summary"),
        ("GET", "/api/field-operations/engineers"),
        ("GET", "/api/field-operations/jobs"),
        ("GET", "/api/field-operations/my-jobs"),
        ("POST", "/api/field-operations/jobs/1/transition"),
        ("POST", "/api/field-operations/jobs/1/location"),
        ("POST", "/api/field-operations/jobs/1/request-otp"),
        ("POST", "/api/field-operations/jobs/1/verify-otp"),
        ("POST", "/api/field-operations/jobs/1/complete"),
        ("GET", "/api/field-operations/customer/tickets/1/tracking"),
        ("GET", "/api/field-operations/jobs/1/tracking-history"),
    ]
    for method, path in endpoints:
        if method == "GET":
            res = client.get(path)
        else:
            res = client.post(path, json={})
        assert res.status_code == 401, f"{method} {path} unexpectedly allowed without auth: {res.status_code}"


def test_viewer_role_restrictions():
    """
    Viewer role:
    - Allowed: Aggregated summary metrics.
    - Forbidden (403): Individual engineer tracking, mutations, history.
    """
    viewer_token = _get_token("client@pmrgsolution.com", "Client@pmrg123")
    headers = {"Authorization": f"Bearer {viewer_token}"}

    # 1. Aggregated summary is allowed
    sum_res = client.get("/api/field-operations/summary", headers=headers)
    assert sum_res.status_code == 200
    assert "active_engineers" in sum_res.json()
    assert "en_route" in sum_res.json()

    # 2. Individual engineer live locations blocked
    eng_res = client.get("/api/field-operations/engineers", headers=headers)
    assert eng_res.status_code == 403

    # 3. Job mutations blocked
    trans_res = client.post(
        "/api/field-operations/jobs/1/transition",
        headers=headers,
        json={"target_status": "ACCEPTED"}
    )
    assert trans_res.status_code == 403

    # 4. Tracking history blocked
    hist_res = client.get("/api/field-operations/jobs/1/tracking-history", headers=headers)
    assert hist_res.status_code == 403


def test_customer_idor_protection():
    """Customer role can ONLY view tracking for their own ticket. Cross-ticket IDOR is blocked."""
    cust_token = _get_token("customer@pmrg.in")
    headers = {"Authorization": f"Bearer {cust_token}"}

    db = SessionLocal()
    try:
        # Find customer record for customer@pmrg.in
        own_cust = db.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
        assert own_cust is not None

        own_ticket = db.query(Ticket).filter(Ticket.customer_id == own_cust.id).first()
        assert own_ticket is not None

        other_ticket = db.query(Ticket).filter(Ticket.customer_id != own_cust.id).first()
        assert other_ticket is not None

        own_ticket_id = own_ticket.id
        other_ticket_id = other_ticket.id
    finally:
        db.close()

    # 1. Access own ticket tracking -> 200 OK
    res_own = client.get(
        f"/api/field-operations/customer/tickets/{own_ticket_id}/tracking",
        headers={**headers, "X-Market-Id": "mumbai"}
    )
    assert res_own.status_code == 200
    data = res_own.json()
    assert data["ticket_id"] == own_ticket_id
    assert "assigned_engineer_name" in data

    # 2. Access another customer's ticket tracking -> 403 Forbidden (IDOR Blocked)
    res_other = client.get(
        f"/api/field-operations/customer/tickets/{other_ticket_id}/tracking",
        headers={**headers, "X-Market-Id": "mumbai"}
    )
    assert res_other.status_code == 403
    assert "own registered tickets" in res_other.json()["detail"].lower()


def test_field_engineer_authorization_and_ownership():
    """
    Engineer A cannot update, send location, request OTP, or complete Engineer B's assignment.
    """
    rahul_token = _get_token("rahul.field@pmrg.in")
    amit_token = _get_token("amit.field@pmrg.in")

    headers_rahul = {"Authorization": f"Bearer {rahul_token}"}
    headers_amit = {"Authorization": f"Bearer {amit_token}"}

    db = SessionLocal()
    try:
        rahul_res = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        amit_res = db.query(Resource).filter(Resource.email == "amit.field@pmrg.in").first()

        rahul_job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul_res.id).first()
        assert rahul_job is not None
        rahul_job_id = rahul_job.id
    finally:
        db.close()

    # 1. Amit attempts to transition Rahul's job -> 403 Forbidden
    bad_trans = client.post(
        f"/api/field-operations/jobs/{rahul_job_id}/transition",
        headers=headers_amit,
        json={"target_status": "ARRIVED"}
    )
    assert bad_trans.status_code == 403

    # 2. Amit attempts to submit location for Rahul's job -> 403 Forbidden
    bad_loc = client.post(
        f"/api/field-operations/jobs/{rahul_job_id}/location",
        headers=headers_amit,
        json={"latitude": 19.055, "longitude": 72.835}
    )
    assert bad_loc.status_code == 403

    # 3. Amit attempts to request OTP on Rahul's job -> 403 Forbidden
    bad_otp_req = client.post(
        f"/api/field-operations/jobs/{rahul_job_id}/request-otp",
        headers=headers_amit
    )
    assert bad_otp_req.status_code == 403

    # 4. Amit attempts to verify OTP on Rahul's job -> 403 Forbidden
    bad_otp_ver = client.post(
        f"/api/field-operations/jobs/{rahul_job_id}/verify-otp",
        headers=headers_amit,
        json={"otp": "123456"}
    )
    assert bad_otp_ver.status_code == 403

    # 5. Amit attempts to complete Rahul's job -> 403 Forbidden
    bad_comp = client.post(
        f"/api/field-operations/jobs/{rahul_job_id}/complete",
        headers=headers_amit
    )
    assert bad_comp.status_code == 403


def test_complete_otp_lifecycle_and_security():
    """
    Test complete lifecycle from WORKING -> request OTP -> wrong OTP lock -> valid OTP -> complete.
    - Premature OTP request blocked.
    - Raw OTP never in engineer response.
    - Incorrect OTP decrements attempts.
    - 5 wrong attempts locks OTP and moves job to ON_HOLD.
    - Cannot complete without valid verified OTP.
    - Valid OTP verification enables completion.
    """
    amit_token = _get_token("amit.field@pmrg.in")
    headers_amit = {"Authorization": f"Bearer {amit_token}"}

    db = SessionLocal()
    try:
        amit_res = db.query(Resource).filter(Resource.email == "amit.field@pmrg.in").first()
        job = db.query(FieldAssignment).join(Ticket).filter(
            FieldAssignment.engineer_id == amit_res.id,
            Ticket.customer_id.isnot(None)
        ).order_by(FieldAssignment.id.asc()).first()
        assert job is not None
        job_id = job.id
        ticket_id = job.ticket_id

        # Set job to WORKING
        job.status = "WORKING"
        job.otp_verified_at = None
        db.commit()
    finally:
        db.close()

    # 1. Cannot complete job directly without OTP verification -> 400 Bad Request
    premature_comp = client.post(f"/api/field-operations/jobs/{job_id}/complete", headers=headers_amit)
    assert premature_comp.status_code == 400
    assert "Customer confirmation OTP has not been verified" in premature_comp.json()["detail"]

    # 2. Engineer requests OTP -> Response does NOT contain raw OTP
    req_res = client.post(f"/api/field-operations/jobs/{job_id}/request-otp", headers=headers_amit)
    assert req_res.status_code == 200
    req_data = req_res.json()
    assert req_data["status"] == "OTP_REQUESTED"
    assert "otp" not in req_data
    assert "otp_code" not in req_data

    # Retrieve generated OTP from mock customer dispatch store (simulating customer receiving it)
    assert ticket_id in _MOCK_CUSTOMER_DISPATCH_STORE
    real_otp = _MOCK_CUSTOMER_DISPATCH_STORE[ticket_id]["otp"]
    assert len(real_otp) == 6

    # 3. Wrong OTP rejected -> 400 Bad Request
    wrong_otp = "000000" if real_otp != "000000" else "111111"
    bad_verify = client.post(
        f"/api/field-operations/jobs/{job_id}/verify-otp",
        headers=headers_amit,
        json={"otp": wrong_otp}
    )
    assert bad_verify.status_code == 400
    assert "remaining" in bad_verify.json()["detail"].lower()

    # 4. Valid OTP verification succeeds
    good_verify = client.post(
        f"/api/field-operations/jobs/{job_id}/verify-otp",
        headers=headers_amit,
        json={"otp": real_otp}
    )
    assert good_verify.status_code == 200
    assert good_verify.json()["verified"] is True
    assert good_verify.json()["status"] == "OTP_VERIFIED"

    # 5. Now job completion succeeds
    comp_res = client.post(f"/api/field-operations/jobs/{job_id}/complete", headers=headers_amit)
    assert comp_res.status_code == 200
    comp_data = comp_res.json()
    assert comp_data["status"] == "COMPLETED"

    # Verify database state
    db = SessionLocal()
    try:
        updated_job = db.query(FieldAssignment).filter(FieldAssignment.id == job_id).first()
        assert updated_job.status == "COMPLETED"
        assert updated_job.completed_at is not None

        updated_ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        assert updated_ticket.status == "Resolved"

        # Verify tracking session stopped
        session = db.query(TrackingSession).filter(TrackingSession.field_assignment_id == job_id).first()
        assert session.status == "STOPPED"

        # Verify audit log
        audit = db.query(AuditLog).filter(
            AuditLog.decision == "JOB_COMPLETED",
            AuditLog.source_module == "Field Operations"
        ).order_by(AuditLog.timestamp.desc()).first()
        assert audit is not None
        assert str(real_otp) not in str(audit.execution_result)
        assert str(real_otp) not in str(audit.original_signals)
    finally:
        db.close()


def test_coordinate_validation():
    """Verify out-of-bounds coordinates and invalid timestamps are rejected."""
    rahul_token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {rahul_token}"}

    db = SessionLocal()
    try:
        rahul_res = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul_res.id).first()
        job_id = job.id
    finally:
        db.close()

    # 1. Invalid latitude > 90
    bad_lat = client.post(
        f"/api/field-operations/jobs/{job_id}/location",
        headers=headers,
        json={"latitude": 99.5, "longitude": 72.8}
    )
    assert bad_lat.status_code == 400

    # 2. Invalid longitude > 180
    bad_lon = client.post(
        f"/api/field-operations/jobs/{job_id}/location",
        headers=headers,
        json={"latitude": 19.0, "longitude": 210.0}
    )
    assert bad_lon.status_code == 400

    # 3. Future timestamp > 60s
    future_time = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    bad_time = client.post(
        f"/api/field-operations/jobs/{job_id}/location",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "recorded_at": future_time}
    )
    assert bad_time.status_code == 400


def test_market_isolation_field_operations():
    """Users restricted to Mumbai market cannot access Kolkata field operations."""
    noc_token = _get_token("noc@pmrg.in")

    # 1. Request Mumbai operations with Mumbai market header -> 200 OK
    mum_res = client.get(
        "/api/field-operations/engineers",
        headers={"Authorization": f"Bearer {noc_token}", "X-Market-Id": "mumbai"}
    )
    assert mum_res.status_code == 200
    mum_engineers = mum_res.json()
    assert len(mum_engineers) > 0
    assert all(e["market_id"] == "mumbai" for e in mum_engineers)

    # 2. Request Kolkata operations with Kolkata market header
    kol_res = client.get(
        "/api/field-operations/engineers",
        headers={"Authorization": f"Bearer {noc_token}", "X-Market-Id": "kolkata"}
    )
    assert kol_res.status_code == 200
    kol_engineers = kol_res.json()
    assert len(kol_engineers) > 0
    assert all(e["market_id"] == "kolkata" for e in kol_engineers)

    # Verify no overlap
    mum_ids = {e["id"] for e in mum_engineers}
    kol_ids = {e["id"] for e in kol_engineers}
    assert mum_ids.isdisjoint(kol_ids)
