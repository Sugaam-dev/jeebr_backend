from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request, Response, status, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
import app.database as app_db
engine = app_db.engine
Base = app_db.Base
from app.routers import (
    auth, customers, assurance, churn, journeys, orchestration, revenue, governance, cockpit, pilot_bundle, ticketing, field_operations, olt_telemetry, rbac, network, provisioning, finance, inventory, tariffs, vouchers
)
from app import markets

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start background scheduler runner if enabled
    if getattr(settings, "SCHEDULER_ENABLED", True):
        from app.services.billing_scheduler_runner import scheduler_runner
        try:
            await scheduler_runner.start()
        except Exception as e:
            logger.error(f"Failed to start scheduler runner during app startup: {e}")
    yield
    # Shutdown: Cleanly stop background scheduler runner
    if getattr(settings, "SCHEDULER_ENABLED", True):
        from app.services.billing_scheduler_runner import scheduler_runner
        try:
            await scheduler_runner.stop()
        except Exception as e:
            logger.error(f"Failed to stop scheduler runner during app shutdown: {e}")

def check_schema_compatibility(db_engine):
    """
    Read-only startup diagnostic: verifies essential tables exist without performing DDL.
    Does NOT create, alter, or drop tables or columns.
    """
    import sqlalchemy as sa
    try:
        inspector = sa.inspect(db_engine)
        existing_tables = set(inspector.get_table_names())
        required_tables = {'users', 'nodes', 'customers', 'tickets'}
        missing = required_tables - existing_tables
        if missing:
            logger.warning("Database schema compatibility diagnostic: missing tables %s. Ensure Alembic migrations are applied.", missing)
            return False
        return True
    except Exception as exc:
        logger.warning("Unable to verify database schema compatibility: %s", exc)
        return False

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="""
    ### SentinelOS — Governed AI Overlay (PMRG Solution LLP)
    A governed AI intelligence layer connecting **Network -> Customer -> OSS/BSS -> Operations -> Revenue**
    through the repeatable loop: **Observe -> Predict -> Recommend -> Approve -> Execute -> Learn**.
    Supports isolated regional market telemetry (Mumbai & Kolkata).
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# CORS Middleware - Explicit allowlist + dynamic Vercel wildcard regex
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_origin_regex=r"^https?://.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _build_cors_headers(request: Request, existing_headers: dict = None) -> dict:
    origin = request.headers.get("origin", "")
    headers = dict(existing_headers or {})
    is_allowed = (
        origin in settings.BACKEND_CORS_ORIGINS
        or origin.endswith(".vercel.app")
        or origin.startswith("http://localhost:")
        or origin.startswith("http://127.0.0.1:")
    )
    if is_allowed and origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Access-Control-Allow-Headers"] = "*"
        headers["Access-Control-Allow-Methods"] = "*"
    return headers

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    headers = _build_cors_headers(request)
    field_errors = {}
    for err in exc.errors():
        loc = err.get("loc", [])
        field = str(loc[-1]) if loc else "general"
        field_errors[field] = err.get("msg", "Validation error")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error_code": "VALIDATION_ERROR",
            "message": "Request validation failed. Please check the highlighted fields.",
            "field_errors": field_errors,
            "detail": jsonable_encoder(exc.errors())
        },
        headers=headers
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    headers = _build_cors_headers(request, exc.headers)
    status_code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "UNPROCESSABLE_ENTITY",
        429: "TOO_MANY_REQUESTS",
        500: "INTERNAL_SERVER_ERROR"
    }
    error_code = status_code_map.get(exc.status_code, "ERROR")
    msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": error_code,
            "message": msg,
            "detail": exc.detail
        },
        headers=headers
    )

@app.exception_handler(Exception)
async def global_cors_exception_handler(request: Request, exc: Exception):
    """Ensure CORS headers are attached even on unexpected 500 errors without leaking internal tracebacks."""
    logger.exception(f"Unhandled error processing {request.method} {request.url}: {exc}")
    headers = _build_cors_headers(request)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected server error occurred. Please try again shortly.",
            "detail": f"Internal Server Error: {str(exc)}"
        },
        headers=headers
    )

# Security Headers & Content-Security-Policy Middleware
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    
    # 1. Standard defense-in-depth headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self), microphone=(), camera=()"
    
    # 2. Strict-Transport-Security (HTTPS production environments)
    if settings.COOKIE_SECURE or settings.ENVIRONMENT.lower() in ('production', 'prod'):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

    # 3. Practical Content-Security-Policy tailored for SentinelOS Vite/React Single-Page App
    # Allows self assets, inline styles required by React/Tailwind, and data/blob for local SVGs/images
    csp_directives = [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://maps.googleapis.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob: https://maps.googleapis.com https://maps.gstatic.com https://*.googleapis.com https://*.ggpht.com",
        "connect-src 'self' https://maps.googleapis.com " + " ".join(settings.BACKEND_CORS_ORIGINS),
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'"
    ]
    response.headers["Content-Security-Policy"] = "; ".join(csp_directives)
    return response

# Include API Routers
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(markets.router, prefix=settings.API_V1_STR)
app.include_router(cockpit.router, prefix=settings.API_V1_STR)
app.include_router(customers.router, prefix=settings.API_V1_STR)
app.include_router(assurance.router, prefix=settings.API_V1_STR)
app.include_router(churn.router, prefix=settings.API_V1_STR)
app.include_router(journeys.router, prefix=settings.API_V1_STR)
app.include_router(orchestration.router, prefix=settings.API_V1_STR)
app.include_router(revenue.router, prefix=settings.API_V1_STR)
app.include_router(governance.router, prefix=settings.API_V1_STR)
app.include_router(pilot_bundle.router, prefix=settings.API_V1_STR)
app.include_router(ticketing.router, prefix=settings.API_V1_STR)
app.include_router(field_operations.router, prefix=f"{settings.API_V1_STR}/field-operations")
app.include_router(field_operations.router, prefix=f"{settings.API_V1_STR}/field")
app.include_router(olt_telemetry.router, prefix=settings.API_V1_STR)
app.include_router(rbac.router, prefix=settings.API_V1_STR)
app.include_router(network.router, prefix=settings.API_V1_STR)
app.include_router(provisioning.router, prefix=settings.API_V1_STR)
app.include_router(finance.router, prefix=settings.API_V1_STR)
app.include_router(inventory.router, prefix=settings.API_V1_STR)
app.include_router(tariffs.router, prefix=settings.API_V1_STR)
app.include_router(vouchers.router, prefix=settings.API_V1_STR)

@app.on_event("startup")
def on_startup():
    try:
        check_schema_compatibility(engine)
    except Exception as exc:
        logger.error(f"Database schema compatibility check notice: {exc}")

@app.get("/")
def root():
    return {
        "system": settings.PROJECT_NAME,
        "status": "online",
        "docs": "/docs",
        "loop": "Observe -> Predict -> Recommend -> Approve -> Execute -> Learn"
    }

def check_database_connectivity(timeout_seconds: float = 2.0):
    """
    Lightweight, bounded database connectivity ping.
    Executes SELECT 1 with an explicit statement timeout.
    Safely releases connection back to the connection pool upon block exit.
    Never exposes internal queries, hostnames, or raw error messages.
    Returns: (status: 'connected' | 'disconnected', latency_ms: Optional[float])
    """
    import time
    from sqlalchemy import text
    try:
        t0 = time.time()
        with engine.connect() as conn:
            if getattr(getattr(engine, "dialect", None), "name", None) == "postgresql":
                conn.execute(text(f"SET statement_timeout = {int(timeout_seconds * 1000)};"))
            row = conn.execute(text("SELECT 1;")).scalar()
            if row == 1:
                latency = round((time.time() - t0) * 1000, 2)
                return ("connected", latency)
        return ("disconnected", None)
    except Exception as exc:
        logger.warning("Database connectivity health check notice: %s", type(exc).__name__)
        return ("disconnected", None)

@app.get("/health/live")
@app.get(f"{settings.API_V1_STR}/health/live")
def liveness_probe():
    """Liveness probe: verifies process execution loop is alive."""
    from datetime import datetime
    return {
        "status": "alive",
        "timestamp": datetime.utcnow().isoformat(),
        "environment": settings.ENVIRONMENT
    }

@app.get("/health/ready")
@app.get(f"{settings.API_V1_STR}/health/ready")
def readiness_probe(response: Response):
    """Readiness probe: verifies essential dependency (PostgreSQL) is ready to serve queries."""
    from datetime import datetime
    db_status, db_latency = check_database_connectivity(timeout_seconds=2.0)
    is_ready = (db_status == "connected")
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if is_ready else "not_ready",
        "database": {
            "status": db_status,
            "latency_ms": db_latency
        },
        "timestamp": datetime.utcnow().isoformat(),
        "environment": settings.ENVIRONMENT
    }

@app.get("/health")
@app.get(f"{settings.API_V1_STR}/health")
def health_check(response: Response):
    """
    Comprehensive health check: reports overall platform health and individual subsystem diagnostics.
    Returns HTTP 200 when healthy, HTTP 503 when mandatory dependencies (PostgreSQL) fail.
    Never exposes internal credentials, queries, or raw stack traces.
    """
    from datetime import datetime
    import time

    # 1. Database connectivity (Mandatory required dependency)
    db_status, db_latency = check_database_connectivity(timeout_seconds=2.0)

    # 2. Redis status (Optional / in-memory fallback unless configured)
    redis_info = {"status": "not_configured", "installed": True, "mode": "in_memory_fallback"}
    redis_url = getattr(settings, "REDIS_URL", None)
    redis_healthy = True
    if redis_url and redis_url.strip():
        try:
            import redis
            start = time.time()
            r = redis.from_url(redis_url.strip(), decode_responses=True)
            ping_ok = r.ping()
            latency_ms = round((time.time() - start) * 1000, 2)
            redis_info = {
                "status": "connected" if ping_ok else "unresponsive",
                "installed": True,
                "latency_ms": latency_ms
            }
            if not ping_ok:
                redis_healthy = False
        except Exception:
            redis_info = {
                "status": "disconnected",
                "installed": True
            }
            redis_healthy = False

    # 3. Overall status evaluation
    if db_status != "connected":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        overall_status = "unhealthy"
    elif not redis_healthy and settings.ENVIRONMENT.lower() in ("production", "prod"):
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return {
        "status": overall_status,
        "timestamp": datetime.utcnow().isoformat(),
        "environment": settings.ENVIRONMENT,
        "database": {
            "status": db_status,
            "latency_ms": db_latency
        },
        "redis": redis_info
    }

