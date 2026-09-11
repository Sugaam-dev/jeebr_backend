from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine, Base
from app.routers import (
    auth, customers, assurance, churn, journeys, orchestration, revenue, governance, cockpit, pilot_bundle, ticketing
)
from app import markets

# Create database tables
Base.metadata.create_all(bind=engine)

def ensure_ticket_columns():
    """Ensure newly added ticketing columns and call logs exist in the database without breaking existing tables."""
    from sqlalchemy import text
    try:
        # Re-run create_all for newly added models like approval_call_logs
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
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
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    
    # 2. Strict-Transport-Security (HTTPS production environments)
    if settings.COOKIE_SECURE or settings.ENVIRONMENT.lower() in ('production', 'prod'):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

    # 3. Practical Content-Security-Policy tailored for SentinelOS Vite/React Single-Page App
    # Allows self assets, inline styles required by React/Tailwind, and data/blob for local SVGs/images
    csp_directives = [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "connect-src 'self' " + " ".join(settings.BACKEND_CORS_ORIGINS),
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

@app.get("/")
def root():
    return {
        "system": settings.PROJECT_NAME,
        "status": "online",
        "docs": "/docs",
        "loop": "Observe -> Predict -> Recommend -> Approve -> Execute -> Learn"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
