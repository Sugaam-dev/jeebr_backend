from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine, Base
from app.routers import (
    auth, customers, assurance, churn, journeys, orchestration, revenue, governance, cockpit, pilot_bundle
)

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="""
    ### PMRG Solution AI Overlay for PMRG Internet (Mumbai ISP)
    A governed AI intelligence layer connecting **Network -> Customer -> OSS/BSS -> Operations -> Revenue**
    through the repeatable loop: **Observe -> Predict -> Recommend -> Approve -> Execute -> Learn**.
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
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(cockpit.router, prefix=settings.API_V1_STR)
app.include_router(customers.router, prefix=settings.API_V1_STR)
app.include_router(assurance.router, prefix=settings.API_V1_STR)
app.include_router(churn.router, prefix=settings.API_V1_STR)
app.include_router(journeys.router, prefix=settings.API_V1_STR)
app.include_router(orchestration.router, prefix=settings.API_V1_STR)
app.include_router(revenue.router, prefix=settings.API_V1_STR)
app.include_router(governance.router, prefix=settings.API_V1_STR)
app.include_router(pilot_bundle.router, prefix=settings.API_V1_STR)

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
