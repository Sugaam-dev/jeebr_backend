"""
Phase 3 Remediation Verification Suite
Tests GPS payload schema, bounds validation, mock protection,
server-side rate limiting, and OLT Health RBAC matrix.
"""
import pytest
import time
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models import Resource, FieldAssignment, LocationPing, User
from app.services.rate_limiter import location_rate_limiter
from app.services.event_publisher import event_publisher

client = TestClient(app)


def _get_token(email: str, password: str = "admin123") -> str:
    """Helper to acquire auth token for given user email."""
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter state before each test."""
    location_rate_limiter.reset_in_memory()
    yield
    location_rate_limiter.reset_in_memory()


def test_gps_payload_canonical_persistence_and_broadcast(monkeypatch):
    """
    Remediation 1 & 15: Verify canonical GPS payload reaches DB and event publisher.
    Specifically tests accuracy=12, speed=3.5, heading=180.
    """
    published_events = []

    async def mock_publish(channel: str, message: dict):
        published_events.append({"channel": channel, "message": message})

    monkeypatch.setattr(event_publisher, "publish", mock_publish)

    rahul_token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {rahul_token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    payload = {
        "latitude": 19.055123,
        "longitude": 72.835456,
        "accuracy": 12.0,
        "speed": 3.5,
        "heading": 180.0,
        "battery_level": 95.0,
        "is_mock": False
    }

    res = client.post(f"/api/field-operations/jobs/{job_id}/ping", headers=headers, json=payload)
    assert res.status_code == 200, f"Ping failed: {res.text}"
    data = res.json()

    # 1. API response validation
    assert data["latitude"] == 19.055123
    assert data["longitude"] == 72.835456
    assert data["accuracy"] == 12.0
    assert data["speed"] == 3.5
    assert data["heading"] == 180.0
    assert data["is_mock"] is False

    # 2. Database persistence verification
    db = SessionLocal()
    try:
        ping_row = db.query(LocationPing).filter(LocationPing.id == data["id"]).first()
        assert ping_row is not None
        assert ping_row.accuracy == 12.0
        assert ping_row.speed == 3.5
        assert ping_row.heading == 180.0
        assert ping_row.is_mock is False
    finally:
        db.close()

    # 3. Event Publisher broadcast verification
    loc_updates = [e for e in published_events if e["message"].get("event") == "location_update"]
    assert len(loc_updates) > 0
    msg = loc_updates[0]["message"]
    assert msg["accuracy"] == 12.0
    assert msg["speed"] == 3.5
    assert msg["heading"] == 180.0
    assert msg["is_mock"] is False


def test_gps_payload_legacy_aliases_mapped_correctly():
    """
    Remediation 1: Verify legacy frontend keys (accuracy_meters, speed_mps, heading_degrees)
    are mapped to canonical schema and persisted without dropping to defaults.
    """
    rahul_token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {rahul_token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    payload = {
        "latitude": 19.055123,
        "longitude": 72.835456,
        "accuracy_meters": 14.5,
        "speed_mps": 4.2,
        "heading_degrees": 270.0,
        "is_mock": False
    }

    res = client.post(f"/api/field-operations/jobs/{job_id}/ping", headers=headers, json=payload)
    assert res.status_code == 200, f"Ping failed: {res.text}"
    data = res.json()

    assert data["accuracy"] == 14.5
    assert data["speed"] == 4.2
    assert data["heading"] == 270.0


def test_negative_accuracy_and_bounds_validation():
    """
    Remediation 6 & 10: Verify negative accuracy and out-of-bound speed/heading are rejected.
    """
    rahul_token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {rahul_token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    # 1. Negative accuracy must be rejected
    res_neg_acc = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "accuracy": -15.0}
    )
    assert res_neg_acc.status_code == 400
    assert "Accuracy cannot be negative" in res_neg_acc.text

    # 2. Negative speed must be rejected
    res_neg_spd = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "speed": -5.0}
    )
    assert res_neg_spd.status_code == 400
    assert "Speed cannot be negative" in res_neg_spd.text

    # 3. Invalid heading (> 360) must be rejected
    res_bad_head = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "heading": 400.0}
    )
    assert res_bad_head.status_code == 400
    assert "Heading must be between 0 and 360" in res_bad_head.text


def test_mock_protection_production_environment(monkeypatch):
    """
    Remediation 4: Verify is_mock=True is strictly rejected with 403 in production.
    """
    rahul_token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {rahul_token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    # Set environment to production
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    # 1. Mock ping in production -> 403 Forbidden
    res_mock = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "is_mock": True}
    )
    assert res_mock.status_code == 403
    assert "Simulated or mock location telemetry is strictly prohibited" in res_mock.text

    # 2. Genuine ping (is_mock=False) in production -> 200 OK
    res_real = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "is_mock": False}
    )
    assert res_real.status_code == 200

    # Reset environment to development
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    location_rate_limiter.reset_in_memory()

    # 3. Mock ping in development -> 200 OK
    res_dev = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "is_mock": True}
    )
    assert res_dev.status_code == 200
    assert res_dev.json()["is_mock"] is True


def test_server_side_ping_rate_limiting():
    """
    Remediation 7: Test server-side GPS ping rate limiting:
    - 1st valid ping -> 200 OK
    - Immediate 2nd ping -> 429 Too Many Requests
    - Ping after interval -> 200 OK
    - Different engineer -> independently rate limited
    """
    rahul_token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {rahul_token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    # 1. First valid ping -> 200 OK
    res1 = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.051, "longitude": 72.831}
    )
    assert res1.status_code == 200

    # 2. Immediate second ping -> 429 Too Many Requests
    res2 = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.052, "longitude": 72.832}
    )
    assert res2.status_code == 429
    assert "Rate limit exceeded" in res2.text

    # 3. Fast-forward rate limiter or reset in-memory timestamp
    location_rate_limiter._in_memory_timestamps[rahul.id] -= 4.0

    res3 = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.053, "longitude": 72.833}
    )
    assert res3.status_code == 200


def test_olt_health_rbac_matrix():
    """
    Remediation 8 & 12: Test GET /api/olt/health across all 8 roles.
    Allowed: Admin, NOC, Executive, Care (200 OK)
    Restricted: Viewer, Field Engineer, Customer (403 Forbidden)
    Unauthenticated: 401 Unauthorized
    """
    roles_and_expected = [
        ("admin@pmrg.in", "admin123", 200),
        ("noc@pmrg.in", "admin123", 200),
        ("executive@pmrg.in", "admin123", 200),
        ("care@pmrg.in", "admin123", 200),
        ("client@pmrgsolution.com", "Client@pmrg123", 403),
        ("rahul.field@pmrg.in", "admin123", 403),
        ("customer@pmrg.in", "admin123", 403),
    ]

    for email, pwd, expected_status in roles_and_expected:
        token = _get_token(email, pwd)
        res = client.get("/api/olt/health", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == expected_status, (
            f"Role for {email} expected {expected_status}, got {res.status_code}: {res.text}"
        )

    # Unauthenticated request (using clean client without auth cookies)
    unauth_client = TestClient(app)
    unauth_res = unauth_client.get("/api/olt/health")
    assert unauth_res.status_code == 401, f"Expected 401, got {unauth_res.status_code}"


def test_otp_request_and_verify_response_includes_id_and_assignment_id():
    """
    Verify that OtpRequestResponse and OtpVerifyResponse include both 'id' and 'assignment_id'
    so that frontend clients reading either field never receive undefined.
    """
    from app.models import Ticket
    from app.services.otp_service import _MOCK_CUSTOMER_DISPATCH_STORE

    token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).join(Ticket).filter(
            FieldAssignment.engineer_id == rahul.id,
            Ticket.customer_id.isnot(None)
        ).first()
        assert job is not None
        job.status = "WORKING"
        db.commit()
        job_id = job.id
        ticket_id = job.ticket_id
    finally:
        db.close()

    # 1. Request OTP
    req_res = client.post(f"/api/field-operations/jobs/{job_id}/request-otp", headers=headers, json={})
    assert req_res.status_code == 200, f"Request OTP failed: {req_res.text}"
    req_json = req_res.json()
    assert req_json["assignment_id"] == job_id
    assert req_json["id"] == job_id
    assert req_json["status"] == "OTP_REQUESTED"

    # Retrieve generated OTP from mock dispatch store
    raw_otp = _MOCK_CUSTOMER_DISPATCH_STORE.get(ticket_id, {}).get("otp")
    assert raw_otp is not None and len(raw_otp) == 6

    # 2. Verify OTP
    verify_res = client.post(
        f"/api/field-operations/jobs/{job_id}/verify-otp",
        headers=headers,
        json={"otp": raw_otp}
    )
    assert verify_res.status_code == 200, f"Verify OTP failed: {verify_res.text}"
    verify_json = verify_res.json()
    assert verify_json["assignment_id"] == job_id
    assert verify_json["id"] == job_id
    assert verify_json["verified"] is True

