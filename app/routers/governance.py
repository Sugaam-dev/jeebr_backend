from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Recommendation, AuditLog, User
from app.schemas import (
    RecommendationResponse, AuditLogResponse, ApproveRejectRequest,
    ExecuteRecommendationRequest, GenerateRecommendationRequest
)
from app.auth import get_current_user, require_roles, require_not_viewer, log_security_event
from app.services.rbac_service import is_super_admin, is_admin_or_super, normalize_role_name
from app.markets import get_current_market
from app.services.ai_orchestrator import get_ai_orchestration_service

router = APIRouter(prefix="/governance", tags=["Human-in-the-Loop AI Governance"])

# Mapping of module to allowed approving roles
MODULE_ROLE_MAP = {
    "Predictive Service Assurance": ["NOC", "Admin"],
    "Churn Prediction & Retention AI": ["Care", "Admin"],
    "Intelligent Customer Journeys": ["Care", "Admin"],
    "AI-driven OSS/BSS Orchestration": ["NOC", "Admin"],
    "Revenue Assurance & Leakage Analytics": ["Revenue", "Admin"],
    "Automatic Ticketing & Regional Dispatch": ["NOC", "Admin"],
    "Network Operations": ["NOC", "Admin"]
}


@router.get("/recommendations", response_model=List[RecommendationResponse])
def get_recommendations(
    status: Optional[str] = None,
    source_module: Optional[str] = None,
    risk_level: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    query = db.query(Recommendation).filter(Recommendation.market_id == market)
    if status:
        query = query.filter(Recommendation.status == status)
    if source_module:
        query = query.filter(Recommendation.source_module == source_module)
    if risk_level:
        query = query.filter(Recommendation.risk_level == risk_level.upper())
    return query.order_by(Recommendation.created_at.desc()).all()


@router.get("/recommendations/{recommendation_id}", response_model=RecommendationResponse)
def get_recommendation_detail(
    recommendation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    if not is_super_admin(current_user.role) and rec.market_id != market:
        raise HTTPException(status_code=403, detail="Forbidden: Cross-market access denied")

    return rec


@router.post("/recommendations/generate", response_model=RecommendationResponse)
def generate_recommendation_endpoint(
    req: GenerateRecommendationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "SUPER_ADMIN"])),
    market: str = Depends(get_current_market)
):
    target_market = req.market_id or market
    orchestrator = get_ai_orchestration_service()
    return orchestrator.generate_recommendation_for_incident(
        db=db,
        incident_type=req.incident_type,
        target_type=req.target_type,
        target_id=req.target_id,
        market_id=target_market,
        alarm_id=req.alarm_id,
        actor=current_user
    )


@router.post("/approve", response_model=RecommendationResponse)
def approve_action(
    req: ApproveRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_not_viewer),
    market: str = Depends(get_current_market)
):
    rec = db.query(Recommendation).filter(Recommendation.id == req.recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Cross-market isolation check
    if not is_super_admin(current_user.role) and rec.market_id != market:
        log_security_event(
            db=db,
            action=f"Unauthorized cross-market approve attempt for recommendation #{rec.id} (market {rec.market_id}) from context {market}",
            decision="ACCESS_DENIED",
            user=current_user,
            endpoint="/api/governance/approve",
            method="POST",
            market_id=market
        )
        raise HTTPException(status_code=403, detail="Forbidden: Cross-market operation not permitted")

    # Enforce RBAC per module
    allowed = MODULE_ROLE_MAP.get(rec.source_module, ["Admin"])
    norm_user_role = normalize_role_name(current_user.role)
    norm_allowed = [normalize_role_name(r) for r in allowed]
    if not is_admin_or_super(current_user.role) and norm_user_role not in norm_allowed:
        log_security_event(
            db=db,
            action=f"Unauthorized approval attempt for recommendation #{rec.id} by {current_user.role}",
            decision="ACCESS_DENIED",
            user=current_user,
            endpoint="/api/governance/approve",
            method="POST",
            market_id=rec.market_id or "mumbai"
        )
        raise HTTPException(
            status_code=403,
            detail=f"Approval requires role in {allowed}. Current user role is {current_user.role}."
        )

    orchestrator = get_ai_orchestration_service()
    return orchestrator.approve_recommendation(
        db=db,
        recommendation_id=req.recommendation_id,
        user=current_user,
        notes=req.notes,
        auto_execute=req.auto_execute or False
    )


@router.post("/reject", response_model=RecommendationResponse)
def reject_action(
    req: ApproveRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_not_viewer),
    market: str = Depends(get_current_market)
):
    rec = db.query(Recommendation).filter(Recommendation.id == req.recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Cross-market isolation check
    if not is_super_admin(current_user.role) and rec.market_id != market:
        raise HTTPException(status_code=403, detail="Forbidden: Cross-market operation not permitted")

    # Enforce RBAC per module
    allowed = MODULE_ROLE_MAP.get(rec.source_module, ["Admin"])
    norm_user_role = normalize_role_name(current_user.role)
    norm_allowed = [normalize_role_name(r) for r in allowed]
    if not is_admin_or_super(current_user.role) and norm_user_role not in norm_allowed:
        log_security_event(
            db=db,
            action=f"Unauthorized reject attempt for recommendation #{rec.id} by {current_user.role}",
            decision="ACCESS_DENIED",
            user=current_user,
            endpoint="/api/governance/reject",
            method="POST",
            market_id=rec.market_id or "mumbai"
        )
        raise HTTPException(
            status_code=403,
            detail=f"Rejection requires role in {allowed}. Current user role is {current_user.role}."
        )

    orchestrator = get_ai_orchestration_service()
    return orchestrator.reject_recommendation(
        db=db,
        recommendation_id=req.recommendation_id,
        user=current_user,
        notes=req.notes
    )


@router.post("/execute", response_model=RecommendationResponse)
def execute_action(
    req: ExecuteRecommendationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_not_viewer),
    market: str = Depends(get_current_market)
):
    rec = db.query(Recommendation).filter(Recommendation.id == req.recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Cross-market isolation check
    if not is_super_admin(current_user.role) and rec.market_id != market:
        raise HTTPException(status_code=403, detail="Forbidden: Cross-market operation not permitted")

    # Enforce RBAC per module
    allowed = MODULE_ROLE_MAP.get(rec.source_module, ["Admin"])
    norm_user_role = normalize_role_name(current_user.role)
    norm_allowed = [normalize_role_name(r) for r in allowed]
    if not is_admin_or_super(current_user.role) and norm_user_role not in norm_allowed:
        log_security_event(
            db=db,
            action=f"Unauthorized execute attempt for recommendation #{rec.id} by {current_user.role}",
            decision="ACCESS_DENIED",
            user=current_user,
            endpoint="/api/governance/execute",
            method="POST",
            market_id=rec.market_id or "mumbai"
        )
        raise HTTPException(
            status_code=403,
            detail=f"Execution requires role in {allowed}. Current user role is {current_user.role}."
        )

    orchestrator = get_ai_orchestration_service()
    return orchestrator.execute_recommendation(
        db=db,
        recommendation_id=req.recommendation_id,
        user=current_user,
        idempotency_key=req.idempotency_key
    )


@router.get("/audit-trail", response_model=List[AuditLogResponse])
def get_audit_trail(
    source_module: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    query = db.query(AuditLog).filter(AuditLog.market_id == market)
    if source_module:
        query = query.filter(AuditLog.source_module == source_module)
    if decision:
        query = query.filter(AuditLog.decision == decision)
    return query.order_by(AuditLog.timestamp.desc()).limit(limit).all()


@router.post("/watchdog/reap")
def reap_stuck_executions_endpoint(
    max_age_minutes: int = Query(10, ge=1, le=1440),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "SUPER_ADMIN", "NOC"]))
):
    """
    Durable Watchdog: Scans for recommendations stuck in EXECUTING state
    following worker crashes or network timeouts. Reconciles authoritative outcomes
    safely and transitions recommendations to terminal states.
    """
    orchestrator = get_ai_orchestration_service()
    reaped = orchestrator.reap_stuck_executions(
        db=db,
        max_age_minutes=max_age_minutes,
        actor=current_user
    )
    return {
        "status": "success",
        "reaped_count": len(reaped),
        "reaped_executions": reaped
    }
