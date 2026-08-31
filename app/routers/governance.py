from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Recommendation, AuditLog, User
from app.schemas import RecommendationResponse, AuditLogResponse, ApproveRejectRequest
from app.auth import get_current_user, require_roles
from app.services.governance_service import approve_recommendation, reject_recommendation

router = APIRouter(prefix="/governance", tags=["Human-in-the-Loop AI Governance"])

# Mapping of module to allowed approving roles
MODULE_ROLE_MAP = {
    "Predictive Service Assurance": ["NOC", "Admin"],
    "Churn Prediction & Retention AI": ["Care", "Admin"],
    "Intelligent Customer Journeys": ["Care", "Admin"],
    "AI-driven OSS/BSS Orchestration": ["NOC", "Admin"],
    "Revenue Assurance & Leakage Analytics": ["Revenue", "Admin"]
}

@router.get("/recommendations", response_model=List[RecommendationResponse])
def get_recommendations(
    status: Optional[str] = None,
    source_module: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Recommendation)
    if status:
        query = query.filter(Recommendation.status == status)
    if source_module:
        query = query.filter(Recommendation.source_module == source_module)
    return query.order_by(Recommendation.created_at.desc()).all()

@router.post("/approve", response_model=RecommendationResponse)
def approve_action(
    req: ApproveRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    rec = db.query(Recommendation).filter(Recommendation.id == req.recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Enforce RBAC per module
    allowed = MODULE_ROLE_MAP.get(rec.source_module, ["Admin"])
    if current_user.role != "Admin" and current_user.role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Approval requires role in {allowed}. Current user role is {current_user.role}."
        )

    try:
        updated = approve_recommendation(db, req.recommendation_id, current_user, req.notes)
        return updated
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/reject", response_model=RecommendationResponse)
def reject_action(
    req: ApproveRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    rec = db.query(Recommendation).filter(Recommendation.id == req.recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Enforce RBAC per module
    allowed = MODULE_ROLE_MAP.get(rec.source_module, ["Admin"])
    if current_user.role != "Admin" and current_user.role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Rejection requires role in {allowed}. Current user role is {current_user.role}."
        )

    try:
        updated = reject_recommendation(db, req.recommendation_id, current_user, req.notes)
        return updated
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/audit-trail", response_model=List[AuditLogResponse])
def get_audit_trail(
    source_module: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(AuditLog)
    if source_module:
        query = query.filter(AuditLog.source_module == source_module)
    if decision:
        query = query.filter(AuditLog.decision == decision)
    return query.order_by(AuditLog.timestamp.desc()).limit(limit).all()
