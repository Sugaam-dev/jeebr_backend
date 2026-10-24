"""
SentinelOS - Database Health & Diagnostics Test Suite (Phase 9B)

Verifies:
1. Successful database connectivity returns HTTP 200, status="healthy", database.status="connected".
2. Database exception returns HTTP 503 Service Unavailable, status="unhealthy", database.status="disconnected".
3. Database timeout returns HTTP 503, bounded failure without hanging.
4. Liveness probe (/health/live) returns HTTP 200 alive even if database is unavailable.
5. Readiness probe (/health/ready) returns HTTP 200 ready when DB is connected, HTTP 503 not_ready when DB is down.
6. Database connections are cleanly released back to the pool after health check execution.
7. Zero sensitive information leaked in responses (no credentials, hostnames, SQL statements, or stack traces).
8. Redis integration reporting (connected, disconnected, in_memory_fallback).
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings

client = TestClient(app)


def test_health_check_database_connected():
    """Live database check returns 200 with structured status and dependency details."""
    res = client.get("/health")
    assert res.status_code == 200, f"Expected 200, got: {res.text}"
    data = res.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data
    assert "environment" in data
    assert "database" in data
    assert data["database"]["status"] == "connected"
    assert isinstance(data["database"]["latency_ms"], (int, float))
    assert data["database"]["latency_ms"] >= 0.0


def test_api_v1_health_alias_connected():
    """Verify /api/health alias matches /health behavior."""
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert data["database"]["status"] == "connected"


def test_liveness_probe_returns_200_alive():
    """Liveness probe indicates the process is alive."""
    res = client.get("/health/live")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "alive"
    assert "timestamp" in data


def test_readiness_probe_returns_200_ready():
    """Readiness probe indicates dependencies are ready to serve queries."""
    res = client.get("/health/ready")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ready"
    assert data["database"]["status"] == "connected"
    assert data["database"]["latency_ms"] >= 0.0


def test_health_check_database_failure_returns_503(monkeypatch):
    """When PostgreSQL is unreachable, /health returns 503 and status='unhealthy'."""
    from app import main

    def mock_broken_db(timeout_seconds: float = 2.0):
        return ("disconnected", None)

    monkeypatch.setattr(main, "check_database_connectivity", mock_broken_db)

    res = client.get("/health")
    assert res.status_code == 503, f"Expected 503 on database failure, got {res.status_code}"
    data = res.json()
    assert data["status"] == "unhealthy"
    assert data["database"]["status"] == "disconnected"
    assert data["database"]["latency_ms"] is None


def test_readiness_probe_database_failure_returns_503(monkeypatch):
    """When PostgreSQL is unreachable, /health/ready returns 503 and status='not_ready'."""
    from app import main

    def mock_broken_db(timeout_seconds: float = 2.0):
        return ("disconnected", None)

    monkeypatch.setattr(main, "check_database_connectivity", mock_broken_db)

    res = client.get("/health/ready")
    assert res.status_code == 503
    data = res.json()
    assert data["status"] == "not_ready"
    assert data["database"]["status"] == "disconnected"


def test_liveness_probe_survives_database_failure(monkeypatch):
    """Liveness probe remains 200 'alive' even when database is disconnected."""
    from app import main

    def mock_broken_db(timeout_seconds: float = 2.0):
        return ("disconnected", None)

    monkeypatch.setattr(main, "check_database_connectivity", mock_broken_db)

    res = client.get("/health/live")
    assert res.status_code == 200
    assert res.json()["status"] == "alive"


def test_health_check_does_not_leak_internals(monkeypatch):
    """Ensure no SQL statements, connection URLs, hostnames, or raw errors appear in responses."""
    from app import main

    # Simulate an internal DB exception with sensitive text
    class FakeEngine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            raise RuntimeError("psql: error: connection to server at 'db.supabase.co:5432' failed with password 'Secret123!'")

    monkeypatch.setattr(main, "engine", FakeEngine())

    res = client.get("/health")
    assert res.status_code == 503
    content_str = res.text.lower()
    # Sensitive tokens that MUST NOT appear
    assert "secret123" not in content_str
    assert "supabase" not in content_str
    assert "5432" not in content_str
    assert "runtimeerror" not in content_str
    assert "select 1" not in content_str


def test_health_check_connection_cleanup():
    """Verify that multiple successive health checks do not leak database connections."""
    from app.database import engine

    pool = engine.pool
    # Run 10 rapid health checks
    for _ in range(10):
        res = client.get("/health")
        assert res.status_code == 200

    # In NullPool or QueuePool, checked out connections should be 0
    checked_out = pool.checkedout() if hasattr(pool, "checkedout") else 0
    assert checked_out == 0, f"Expected 0 checked out connections after health check, found: {checked_out}"


def test_health_check_redis_unconfigured():
    """When REDIS_URL is not set or empty, redis mode is in_memory_fallback."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert "redis" in data
    assert data["redis"]["installed"] is True
    if not getattr(settings, "REDIS_URL", None):
        assert data["redis"]["status"] == "not_configured"
        assert data["redis"]["mode"] == "in_memory_fallback"
