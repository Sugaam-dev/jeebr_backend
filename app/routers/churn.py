from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Customer, User
from app.schemas import ChurnCustomerPrediction, RecommendRetentionRequest, RecommendationResponse
from app.auth import get_current_user, require_roles
from app.services.churn_engine import get_at_risk_customers, calculate_customer_churn_score
from app.services.governance_service import create_or_get_recommendation

router = APIRouter(prefix="/churn", tags=["Churn Prediction & Retention AI"])

@router.get("/at-risk", response_model=List[ChurnCustomerPrediction])
def list_at_risk_customers(
    min_score: float = Query(30.0, ge=0.0, le=100.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_at_risk_customers(db, min_score=min_score)

@router.post("/recommend", response_model=RecommendationResponse)
def propose_retention_action(
    req: RecommendRetentionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    customer = db.query(Customer).filter(Customer.id == req.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    score, risk_lvl, confidence, factors, suggested_action, rev_risk = calculate_customer_churn_score(customer, db)
    action_text = req.action_type or suggested_action

    rec = create_or_get_recommendation(
        db=db,
        source_module="Churn Prediction & Retention AI",
        target_entity_type="Customer",
        target_entity_id=customer.id,
        target_entity_label=f"{customer.name} ({customer.customer_code})",
        title=f"Targeted Retention Save Offer - {customer.name}",
        description=f"At-risk score {score:.1f}% ({risk_lvl} Risk, ₹{rev_risk:,.0f}/yr ARPU at risk). {action_text}",
        recommended_action=action_text,
        confidence_score=round(confidence, 2),
        action_payload={
            "customer_code": customer.customer_code,
            "locality": customer.locality,
            "segment": customer.segment,
            "plan_name": customer.plan_name,
            "arpu": customer.arpu,
            "churn_score": score,
            "signals": [s.model_dump() if hasattr(s, 'model_dump') else s.dict() if hasattr(s, 'dict') else s for s in factors]
        }
    )
    return rec
