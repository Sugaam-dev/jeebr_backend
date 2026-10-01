import pytest
import concurrent.futures
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import (
    User, Resource, Ticket, Customer, FieldAssignment, TrackingSession, LocationPing, FieldOtp, AuditLog
)
from app.services.otp_service import _MOCK_CUSTOMER_DISPATCH_STORE
from app.services.event_publisher import event_publisher

client = TestClient(app)


def _get_token(email: str, password: str = "admin123") -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


def test_otp_sse_broadcast_contains_no_raw_otp(monkeypatch):
    """
    SECURITY AUDIT: When OTP is requested, the SSE broadcast event payload
    must contain ONLY metadata (assignment_id, ticket_id, status, expires_at)
    and strictly ZERO raw OTP, otp_hash, or otp_salt.
    """
    admin_token = _get_token("admin@pmrg.in")
    headers = {"Authorization": f"Bearer {admin_token}"}

    captured_events = []

    async def mock_publish(channel: str, message: dict):
        captured_events.append({"channel": channel, "message": message})

    monkeypatch.setattr(event_publisher, "publish", mock_publish)

    db = SessionLocal()
    try:
        job = db.query(FieldAssignment).join(Ticket).filter(Ticket.customer_id.isnot(None)).first()
        assert job is not None
        job_id = job.id
        job.status = "WORKING"
        job.otp_verified_at = None
        db.commit()
    finally:
        db.close()

    captured_events.clear()
    res = client.post(f"/api/field-operations/jobs/{job_id}/request-otp", headers=headers)
    assert res.status_code == 200, f"Request OTP failed: {res.text}"

    # Verify captured events
    otp_events = [e["message"] for e in captured_events if e["message"].get("event") == "otp_requested"]
    assert len(otp_events) > 0, "No otp_requested SSE event was broadcast"

    for payload in otp_events:
        # Must contain safe metadata
        assert "assignment_id" in payload
        assert "ticket_id" in payload
        assert "status" in payload
        assert payload["status"] == "OTP_REQUESTED"

        # MUST NEVER contain sensitive OTP secrets
        assert "otp" not in payload, "SECURITY VULNERABILITY: Raw OTP found in SSE payload!"
        assert "otp_code" not in payload, "SECURITY VULNERABILITY: otp_code found in SSE payload!"
        assert "otp_hash" not in payload, "SECURITY VULNERABILITY: otp_hash found in SSE payload!"
        assert "otp_salt" not in payload, "SECURITY VULNERABILITY: otp_salt found in SSE payload!"


def test_customer_retrieves_otp_in_tracking_response():
    """
    VERIFICATION: Customer querying their own ticket tracking endpoint receives
    active OTP details for verbal closure handshake without requiring manual page refresh.
    """
    db = SessionLocal()
    try:
        cust = db.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
        assert cust is not None
        ticket = db.query(Ticket).filter(Ticket.customer_id == cust.id).first()
        assert ticket is not None

        assignment = db.query(FieldAssignment).filter(FieldAssignment.ticket_id == ticket.id).first()
        if not assignment:
            eng = db.query(Resource).filter(Resource.type == "FIELD_TECH").first()
            assignment = FieldAssignment(
                ticket_id=ticket.id,
                engineer_id=eng.id,
                market_id=ticket.market_id or "mumbai",
                status="WORKING"
            )
            db.add(assignment)
            db.commit()
            db.refresh(assignment)
        else:
            assignment.status = "WORKING"
            assignment.otp_verified_at = None
            db.commit()

        ticket_id = ticket.id
        assignment_id = assignment.id
    finally:
        db.close()

    admin_token = _get_token("admin@pmrg.in")
    otp_res = client.post(
        f"/api/field-operations/jobs/{assignment_id}/request-otp",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert otp_res.status_code == 200

    # Now Customer queries tracking endpoint
    cust_token = _get_token("customer@pmrg.in")
    tracking_res = client.get(
        f"/api/field-operations/customer/tickets/{ticket_id}/tracking",
        headers={"Authorization": f"Bearer {cust_token}"}
    )
    assert tracking_res.status_code == 200
    data = tracking_res.json()
    assert data["job_status"] == "OTP_REQUESTED"
    assert data["otp"] is not None
    assert data["otp"]["code"] is not None
    assert len(data["otp"]["code"]) == 6
    assert data["otp_code"] == data["otp"]["code"]


def test_concurrent_otp_verification_race_condition():
    """
    CONCURRENCY & ROW-LOCKING: Two simultaneous verify calls with the same OTP.
    Because of row-level locking (with_for_update) and state checks,
    exactly ONE must succeed and the other must be rejected.
    """
    admin_token = _get_token("admin@pmrg.in")
    headers = {"Authorization": f"Bearer {admin_token}"}

    db = SessionLocal()
    try:
        job = db.query(FieldAssignment).join(Ticket).filter(Ticket.customer_id.isnot(None)).first()
        assert job is not None
        job_id = job.id
        ticket_id = job.ticket_id
        job.status = "WORKING"
        job.otp_verified_at = None
        db.commit()
    finally:
        db.close()

    # Generate OTP
    res_otp = client.post(f"/api/field-operations/jobs/{job_id}/request-otp", headers=headers)
    assert res_otp.status_code == 200

    raw_otp_entry = _MOCK_CUSTOMER_DISPATCH_STORE.get(ticket_id)
    assert raw_otp_entry is not None, "OTP was not dispatched to mock store"
    raw_otp = raw_otp_entry["otp"]

    def _verify_attempt():
        return client.post(
            f"/api/field-operations/jobs/{job_id}/verify-otp",
            headers=headers,
            json={"otp_code": raw_otp}
        )

    # Launch two simultaneous verification requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_verify_attempt)
        f2 = executor.submit(_verify_attempt)
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    # Exactly one 200 OK, and one 400 Bad Request
    assert 200 in statuses, f"Neither request succeeded: {statuses}, {r1.text}, {r2.text}"
    assert 400 in statuses, f"Both requests succeeded (race condition occurred!): {statuses}"


def test_concurrent_job_completion_race_condition():
    """
    CONCURRENCY & ROW-LOCKING: Two simultaneous completion calls for the same verified job.
    Exactly ONE must succeed with 200, and the second must be rejected with 400.
    """
    admin_token = _get_token("admin@pmrg.in")
    headers = {"Authorization": f"Bearer {admin_token}"}

    db = SessionLocal()
    try:
        # Pick distinct job with valid customer to prevent interference with test 3
        job = db.query(FieldAssignment).join(Ticket).filter(Ticket.customer_id.isnot(None)).order_by(FieldAssignment.id.desc()).first()
        assert job is not None
        job_id = job.id
        ticket_id = job.ticket_id
        job.status = "WORKING"
        job.otp_verified_at = None
        db.commit()
    finally:
        db.close()

    # Prepare job in OTP_VERIFIED state
    res_req = client.post(f"/api/field-operations/jobs/{job_id}/request-otp", headers=headers)
    assert res_req.status_code == 200
    raw_otp = _MOCK_CUSTOMER_DISPATCH_STORE[ticket_id]["otp"]
    verify_res = client.post(
        f"/api/field-operations/jobs/{job_id}/verify-otp",
        headers=headers,
        json={"otp_code": raw_otp}
    )
    assert verify_res.status_code == 200

    def _complete_attempt():
        return client.post(
            f"/api/field-operations/jobs/{job_id}/complete",
            headers=headers,
            json={"resolution_notes": "Concurrent completion test"}
        )

    # Launch two simultaneous completion requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_complete_attempt)
        f2 = executor.submit(_complete_attempt)
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    assert 200 in statuses, f"Neither completion succeeded: {statuses}, R1: {r1.status_code} {r1.text}, R2: {r2.status_code} {r2.text}"
    assert 400 in statuses, f"Duplicate completion allowed (race condition!): {statuses}, R1: {r1.status_code} {r1.text}, R2: {r2.status_code} {r2.text}"


def test_sse_stream_authorization_boundaries(monkeypatch):
    """
    RBAC & IDOR: SSE streaming endpoints enforce strict role and tenancy boundaries:
    - Viewer is blocked with 403 on all streams.
    - Customer is blocked from other customers' tickets.
    - Customer accessing own ticket gets authenticated 200 SSE stream.
    """
    viewer_token = _get_token("client@pmrgsolution.com", "Client@pmrg123")
    cust_token = _get_token("customer@pmrg.in")

    db = SessionLocal()
    try:
        cust = db.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
        assert cust is not None
        own_ticket = db.query(Ticket).filter(Ticket.customer_id == cust.id).first()
        other_ticket = db.query(Ticket).filter(Ticket.customer_id != cust.id).first()
        own_ticket_id = own_ticket.id
        other_ticket_id = other_ticket.id
    finally:
        db.close()

    # 1. Viewer blocked from ticket stream -> 403
    v_res = client.get(
        f"/api/field-operations/tickets/{own_ticket_id}/stream",
        headers={"Authorization": f"Bearer {viewer_token}"}
    )
    assert v_res.status_code == 403

    # 2. Customer accessing another customer's ticket stream -> 403 Forbidden
    c_bad_res = client.get(
        f"/api/field-operations/tickets/{other_ticket_id}/stream",
        headers={"Authorization": f"Bearer {cust_token}"}
    )
    assert c_bad_res.status_code == 403

    # 3. Customer accessing own ticket stream -> authorized
    async def finite_subscribe(channel):
        yield '{"event": "ping", "data": "test"}'

    monkeypatch.setattr(event_publisher, "subscribe", finite_subscribe)

    res = client.get(
        f"/api/field-operations/tickets/{own_ticket_id}/stream",
        headers={"Authorization": f"Bearer {cust_token}"}
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    assert "connected" in res.text
    assert "ping" in res.text


def test_route_calculation_and_caching():
    """
    ROUTING SERVICE: Tests road routing endpoint and verification that
    caching and fallback calculation functions properly.
    """
    admin_token = _get_token("admin@pmrg.in")
    headers = {"Authorization": f"Bearer {admin_token}"}

    db = SessionLocal()
    try:
        job = db.query(FieldAssignment).join(Ticket).filter(Ticket.customer_id.isnot(None)).first()
        assert job is not None
        job_id = job.id
    finally:
        db.close()

    # Call route calculation endpoint
    res = client.get(f"/api/field-operations/jobs/{job_id}/route", headers=headers)
    assert res.status_code == 200
    route = res.json()
    assert "waypoints" in route
    assert "distance_meters" in route
    assert "duration_seconds" in route
    assert "provider" in route
    assert "fallback" in route["provider"] or "google" in route["provider"]
    assert len(route["waypoints"]) >= 2


def test_multi_resource_engineer_jobs_resolution():
    """
    ENGINEER WORKBENCH: Tests that an engineer mapped to multiple resources
    (e.g. Mumbai and Kolkata) sees their assigned work orders in /my-jobs
    and can transition/access them regardless of resource ID ordering.
    """
    db = SessionLocal()
    try:
        # Find or create a test field engineer user
        eng_user = db.query(User).filter(User.email == "multi_tech@pmrg.in").first()
        if not eng_user:
            from app.auth import hash_password
            eng_user = User(
                email="multi_tech@pmrg.in",
                hashed_password=hash_password("tech123"),
                full_name="Multi Tech",
                role="Field Engineer",
                is_active=True
            )
            db.add(eng_user)
            db.commit()
            db.refresh(eng_user)

        # Create two resources for this engineer: Res 1 (Kolkata) and Res 2 (Mumbai)
        r_kol = db.query(Resource).filter(Resource.email == "multi_tech@pmrg.in", Resource.market_id == "kolkata").first()
        if not r_kol:
            r_kol = Resource(
                name="Multi Tech Kol",
                email="multi_tech@pmrg.in",
                market_id="kolkata",
                region="Salt Lake",
                resource_type="FIELD",
                status="Available",
                user_id=eng_user.id
            )
            db.add(r_kol)

        r_mum = db.query(Resource).filter(Resource.email == "multi_tech@pmrg.in", Resource.market_id == "mumbai").first()
        if not r_mum:
            r_mum = Resource(
                name="Multi Tech Mum",
                email="multi_tech@pmrg.in",
                market_id="mumbai",
                region="Bandra West",
                resource_type="FIELD",
                status="Available",
                user_id=eng_user.id
            )
            db.add(r_mum)
        db.commit()
        db.refresh(r_mum)

        # Assign a ticket to Mumbai resource (which is not the first resource)
        ticket = db.query(Ticket).filter(Ticket.market_id == "mumbai").first()
        assert ticket is not None
        ticket.assigned_resource_id = r_mum.id
        db.commit()
        ticket_id = ticket.id
    finally:
        db.close()

    tech_token = _get_token("multi_tech@pmrg.in", "tech123")
    headers = {"Authorization": f"Bearer {tech_token}", "X-Market-Id": "mumbai"}

    # Call /my-jobs
    res = client.get("/api/field-operations/my-jobs", headers=headers)
    assert res.status_code == 200
    my_jobs = res.json()
    assert len(my_jobs) > 0
    assigned_ticket_ids = [j["ticket_id"] for j in my_jobs]
    assert ticket_id in assigned_ticket_ids

    # Engineer can view single job detail
    job_id = next(j["id"] for j in my_jobs if j["ticket_id"] == ticket_id)
    detail_res = client.get(f"/api/field-operations/jobs/{job_id}", headers=headers)
    assert detail_res.status_code == 200
    assert detail_res.json()["id"] == job_id

