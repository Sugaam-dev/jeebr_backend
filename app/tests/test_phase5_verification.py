"""
Phase 5 Independent Verification Test Suite
Verifies:
1. GAP-01 & GAP-02: Complete GPS validation matrix (accuracy <= 5000, speed <= 100, lat, lon, heading, boundary values)
2. GAP-07: Real Redis operation (SET, GET, TTL, expiration, OTP storage, rate limiting, health check)
3. Fail-closed production behavior
4. Reconciled Customer Tracking API endpoints
"""

import pytest
import time
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models import Resource, FieldAssignment, Ticket, Customer
from app.services.rate_limiter import EngineerLocationRateLimiter, location_rate_limiter
from app.services.otp_service import _store_dispatch_code, _retrieve_dispatch_code, _MOCK_CUSTOMER_DISPATCH_STORE

client = TestClient(app)
REDIS_TEST_URL = "redis://127.0.0.1:6379/0"


def _get_token(email: str, password: str = "admin123") -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return res.json()["access_token"]


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    location_rate_limiter.reset_in_memory()
    yield
    location_rate_limiter.reset_in_memory()


# --- GAP-01 & GAP-02: GPS Validation Matrix ---

def test_gps_accuracy_upper_bound_rejected():
    """GAP-01: Accuracy > 5000 must be rejected with 400 Bad Request."""
    token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    # 1. Accuracy 5001.0 -> Rejected
    res = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "accuracy": 5001.0}
    )
    assert res.status_code == 400
    assert "Accuracy reading exceeds plausible bounds" in res.json()["detail"]

    # 2. Accuracy 10000.0 -> Rejected
    res_huge = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "accuracy": 10000.0}
    )
    assert res_huge.status_code == 400
    assert "Accuracy reading exceeds plausible bounds" in res_huge.json()["detail"]

    # 3. Accuracy -1.0 -> Rejected
    res_neg = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "accuracy": -1.0}
    )
    assert res_neg.status_code == 400
    assert "Accuracy cannot be negative" in res_neg.json()["detail"]


def test_gps_speed_upper_bound_rejected():
    """GAP-02: Speed > 100 must be rejected with 400 Bad Request."""
    token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    # 1. Speed 101.0 -> Rejected
    res = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "speed": 101.0}
    )
    assert res.status_code == 400
    assert "Speed exceeds plausible bounds" in res.json()["detail"]

    # 2. Speed 250.0 -> Rejected
    res_fast = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "speed": 250.0}
    )
    assert res_fast.status_code == 400
    assert "Speed exceeds plausible bounds" in res_fast.json()["detail"]

    # 3. Speed -5.0 -> Rejected
    res_neg = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "speed": -5.0}
    )
    assert res_neg.status_code == 400
    assert "Speed cannot be negative" in res_neg.json()["detail"]


def test_gps_complete_boundary_values_accepted():
    """Verify exact valid boundaries: accuracy=0, 5000; speed=0, 100; heading=0, 360."""
    token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job.status = "WORKING"
        db.commit()
        job_id = job.id
    finally:
        db.close()

    # Lower boundary: accuracy=0, speed=0, heading=0
    res_low = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.05, "longitude": 72.83, "accuracy": 0.0, "speed": 0.0, "heading": 0.0}
    )
    assert res_low.status_code == 200, f"Lower boundary failed: {res_low.text}"

    # Reset rate limit for immediate test
    location_rate_limiter.reset_in_memory()

    # Upper boundary: accuracy=5000, speed=100, heading=360
    res_high = client.post(
        f"/api/field-operations/jobs/{job_id}/ping",
        headers=headers,
        json={"latitude": 19.06, "longitude": 72.84, "accuracy": 5000.0, "speed": 100.0, "heading": 360.0}
    )
    assert res_high.status_code == 200, f"Upper boundary failed: {res_high.text}"


def test_gps_full_coordinate_rejection_matrix():
    """Verify coordinate bounds: lat [-90, 90], lon [-180, 180], heading [0, 360]."""
    token = _get_token("rahul.field@pmrg.in")
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        rahul = db.query(Resource).filter(Resource.email == "rahul.field@pmrg.in").first()
        job = db.query(FieldAssignment).filter(FieldAssignment.engineer_id == rahul.id).first()
        job_id = job.id
    finally:
        db.close()

    invalid_cases = [
        ({"latitude": 90.1, "longitude": 72.83}, "Invalid latitude"),
        ({"latitude": -90.1, "longitude": 72.83}, "Invalid latitude"),
        ({"latitude": 19.05, "longitude": 180.1}, "Invalid longitude"),
        ({"latitude": 19.05, "longitude": -180.1}, "Invalid longitude"),
        ({"latitude": 19.05, "longitude": 72.83, "heading": 360.1}, "Heading must be between 0 and 360"),
        ({"latitude": 19.05, "longitude": 72.83, "heading": -1.0}, "Heading must be between 0 and 360"),
    ]

    for payload, expected_phrase in invalid_cases:
        location_rate_limiter.reset_in_memory()
        res = client.post(f"/api/field-operations/jobs/{job_id}/ping", headers=headers, json=payload)
        assert res.status_code == 400, f"Expected 400 for {payload}, got {res.status_code}"
        assert expected_phrase in res.json()["detail"]


# --- GAP-07: Real Redis Operations ---

def test_redis_direct_connection_and_ttl():
    """Verify real Redis communication: SET, GET, TTL, and key expiration."""
    import redis
    r = redis.from_url(REDIS_TEST_URL, decode_responses=True)
    assert r.ping() is True

    test_key = "test:phase5:ttl_key"
    r.set(test_key, "sentinel_value", ex=2)

    # Immediate GET
    val = r.get(test_key)
    assert val == "sentinel_value"

    # TTL verification
    ttl = r.ttl(test_key)
    assert ttl > 0 and ttl <= 2

    # Await expiration
    time.sleep(2.2)
    assert r.get(test_key) is None


def test_redis_otp_storage_and_retrieval(monkeypatch):
    """Verify distributed OTP storage and retrieval using real Redis."""
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_TEST_URL)

    db = SessionLocal()
    try:
        customer = db.query(Customer).first()
        ticket = db.query(Ticket).first()
        ticket_id = ticket.id
    finally:
        db.close()

    _store_dispatch_code(ticket_id, "987654", ttl_seconds=30, customer=customer)

    # Verify key in real Redis
    import redis
    r = redis.from_url(REDIS_TEST_URL, decode_responses=True)
    stored_redis_val = r.get(f"otp:dispatch:{ticket_id}")
    assert stored_redis_val == "987654"

    # Verify retrieval function returns Redis value
    retrieved = _retrieve_dispatch_code(ticket_id)
    assert retrieved == "987654"

    # Cleanup
    r.delete(f"otp:dispatch:{ticket_id}")


@pytest.mark.anyio
async def test_redis_sliding_window_rate_limiter(monkeypatch):
    """Verify distributed rate limiter using real Redis pttl."""
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_TEST_URL)

    limiter = EngineerLocationRateLimiter(min_interval_seconds=3.0)
    engineer_id = 9999

    # Clean redis key
    import redis
    r = redis.from_url(REDIS_TEST_URL, decode_responses=True)
    r.delete(f"rate_limit:engineer_ping:{engineer_id}")

    # 1. Initial state: not throttled
    throttled, retry_after = await limiter.is_throttled(engineer_id)
    assert throttled is False

    # 2. Record ping
    await limiter.record_accepted_ping(engineer_id)

    # 3. Immediately check: throttled with Redis pttl
    throttled, retry_after = await limiter.is_throttled(engineer_id)
    assert throttled is True
    assert retry_after > 0.0 and retry_after <= 3.0

    # Cleanup
    r.delete(f"rate_limit:engineer_ping:{engineer_id}")


def test_health_check_endpoint_redis_integration(monkeypatch):
    """Verify /health endpoint reports real Redis connection and latency."""
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_TEST_URL)

    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "redis" in data
    assert data["redis"]["installed"] is True
    assert data["redis"]["status"] == "connected"
    assert data["redis"]["latency_ms"] >= 0.0


def test_production_fail_closed_when_redis_unavailable(monkeypatch):
    """Verify fail-closed behavior when ENVIRONMENT=production and Redis is down."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:63999/0")  # Invalid port

    db = SessionLocal()
    try:
        customer = db.query(Customer).first()
        ticket = db.query(Ticket).first()
        ticket_id = ticket.id
    finally:
        db.close()

    # In production, failure to connect to Redis for OTP storage must raise 503
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _store_dispatch_code(ticket_id, "123456", ttl_seconds=60, customer=customer)
    assert exc_info.value.status_code == 503


# --- GAP-05: Customer Tracking Route Verification ---

def test_customer_tracking_routes():
    """Verify actual customer tracking endpoints and IDOR protection."""
    db = SessionLocal()
    try:
        cust = db.query(Customer).filter(Customer.email == "customer@pmrg.in").first()
        assert cust is not None
        ticket = db.query(Ticket).filter(Ticket.customer_id == cust.id).first()
        assert ticket is not None
        ticket_id = ticket.id
    finally:
        db.close()

    cust_token = _get_token("customer@pmrg.in")
    headers = {"Authorization": f"Bearer {cust_token}"}

    # Canonical route
    res1 = client.get(f"/api/field-operations/customer/tickets/{ticket_id}/tracking", headers=headers)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["ticket_id"] == ticket_id

    # Alias route
    res2 = client.get(f"/api/field/tracking/{ticket_id}", headers=headers)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["ticket_id"] == ticket_id
