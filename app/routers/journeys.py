from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Customer, User
from app.schemas import (
    JourneyCustomerItem, RecommendJourneyRequest, RecommendationResponse,
    JourneyFunnelSummaryResponse
)
from app.auth import get_current_user, require_roles
from app.services.journey_engine import (
    evaluate_customer_journeys, evaluate_single_customer_journey, get_journey_funnel_summary
)
from app.services.governance_service import create_or_get_recommendation

router = APIRouter(prefix="/journeys", tags=["Intelligent Customer Journeys"])

@router.get("/funnel-summary", response_model=JourneyFunnelSummaryResponse)
def get_funnel_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_journey_funnel_summary(db)

@router.get("/next-best-actions", response_model=List[JourneyCustomerItem])
def list_journey_nbas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return evaluate_customer_journeys(db)

@router.post("/recommend", response_model=RecommendationResponse)
def propose_journey_action(
    req: RecommendJourneyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    customer = db.query(Customer).filter(Customer.id == req.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    matched = evaluate_single_customer_journey(customer, db)
    
    action_text = req.action_type or (matched.next_best_action if matched else "Trigger Journey Engagement")
    confidence = matched.confidence_score if matched else 0.88

    rec = create_or_get_recommendation(
        db=db,
        source_module="Intelligent Customer Journeys",
        target_entity_type="Customer",
        target_entity_id=customer.id,
        target_entity_label=f"{customer.name} ({customer.customer_code})",
        title=f"Next-Best-Action - Stage: {customer.current_stage or 'Use'}",
        description=f"{matched.action_reason if matched else 'Customer lifecycle optimization'}. {action_text}",
        recommended_action=action_text,
        confidence_score=confidence,
        action_payload={
            "customer_code": customer.customer_code,
            "stage": customer.current_stage,
            "locality": customer.locality,
            "channel": matched.suggested_channel if matched else "WhatsApp",
            "signals": [s.model_dump() for s in matched.contributing_signals] if matched else [
                {"signal": "Lifecycle Stage", "value": customer.current_stage or "Use", "weight": "+30 pts"}
            ]
        }
    )
    return rec
