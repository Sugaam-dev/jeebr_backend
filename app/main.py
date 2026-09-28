from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine, Base
from app.routers import (
    auth, customers, assurance, churn, journeys, orchestration, revenue, governance, cockpit, pilot_bundle, ticketing, field_operations, olt_telemetry, rbac
)
from app import markets

# Create database tables
Base.metadata.create_all(bind=engine)

def ensure_ticket_columns():
    """Ensure newly added ticketing columns, field operations tables and columns exist in the database."""
    from sqlalchemy import text
    try:
        # Re-run create_all for newly added models
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            try:
                conn.execute(text("SET lock_timeout = '2s';"))
            except Exception:
                pass
            columns_to_add = [
                ("source", "VARCHAR(50) DEFAULT 'CUSTOMER'"),
                ("region", "VARCHAR(100)"),
                ("assigned_resource_id", "INTEGER REFERENCES resources(id)"),
                ("assigned_at", "TIMESTAMP"),
                ("approval_status", "VARCHAR(50) DEFAULT 'NOT_REQUIRED'"),
                ("approved_by_id", "INTEGER REFERENCES users(id)"),
                ("approved_at", "TIMESTAMP"),
                ("approval_notes", "TEXT"),
            ]
            for col, col_def in columns_to_add:
                try:
                    conn.execute(text(f"ALTER TABLE tickets ADD COLUMN IF NOT EXISTS {col} {col_def};"))
                    conn.commit()
                except Exception:
                    pass
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50) DEFAULT '+91 98200 12345';"))
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE tickets ALTER COLUMN customer_id DROP NOT NULL;"))
                conn.commit()
            except Exception:
                pass

            # Customer service location columns
            for col, col_def in [
                ("service_latitude", "FLOAT"),
                ("service_longitude", "FLOAT"),
                ("service_address", "VARCHAR(255)")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE customers ADD COLUMN IF NOT EXISTS {col} {col_def};"))
                    conn.commit()
                except Exception:
                    pass

            # Resource location and user_id columns
            for col, col_def in [
                ("user_id", "INTEGER REFERENCES users(id)"),
                ("current_latitude", "FLOAT"),
                ("current_longitude", "FLOAT"),
                ("last_ping_at", "TIMESTAMP"),
                ("location_status", "VARCHAR(50) DEFAULT 'UNAVAILABLE'")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE resources ADD COLUMN IF NOT EXISTS {col} {col_def};"))
                    conn.commit()
                except Exception:
                    pass

            # Location ping is_mock column
            try:
                conn.execute(text("ALTER TABLE location_pings ADD COLUMN IF NOT EXISTS is_mock BOOLEAN DEFAULT FALSE;"))
                conn.commit()
            except Exception:
                pass

            # Customers financial & signup defaults
            for col, default_val in [
                ("arpu", "295.0"),
                ("actual_arpu", "295.0"),
                ("revenue_30d", "295.0"),
                ("plan_price", "299.0"),
                ("signup_date", "NOW()")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE customers ALTER COLUMN {col} SET DEFAULT {default_val};"))
                    conn.commit()
                except Exception:
                    pass
    except Exception:
        pass

ensure_ticket_columns()

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
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
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

@app.get("/")
def root():
    return {
        "system": settings.PROJECT_NAME,
        "status": "online",
        "docs": "/docs",
        "loop": "Observe -> Predict -> Recommend -> Approve -> Execute -> Learn"
    }

@app.get("/health")
@app.get(f"{settings.API_V1_STR}/health")
def health_check():
    from datetime import datetime
    import time
    health_data = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "environment": settings.ENVIRONMENT
    }

    # Redis health & readiness verification (Section 18)
    redis_url = getattr(settings, "REDIS_URL", None)
    if redis_url and redis_url.strip():
        try:
            import redis
            start = time.time()
            r = redis.from_url(redis_url.strip(), decode_responses=True)
            ping_ok = r.ping()
            latency_ms = round((time.time() - start) * 1000, 2)
            health_data["redis"] = {
                "status": "connected" if ping_ok else "unresponsive",
                "installed": True,
                "latency_ms": latency_ms
            }
        except Exception as e:
            health_data["redis"] = {
                "status": "disconnected",
                "installed": True,
                "error": str(e)
            }
            if settings.ENVIRONMENT.lower() in ("production", "prod"):
                health_data["status"] = "degraded"
    else:
        health_data["redis"] = {
            "status": "not_configured",
            "installed": True,
            "mode": "in_memory_fallback"
        }

    return health_data

