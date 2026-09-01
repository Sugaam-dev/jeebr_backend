from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Node, User
from app.schemas import NodeDegradationPrediction, RecommendAssuranceRequest, RecommendationResponse
from app.auth import get_current_user, require_roles
from app.services.assurance_engine import evaluate_node_degradations
from app.services.governance_service import create_or_get_recommendation

router = APIRouter(prefix="/assurance", tags=["Predictive Service Assurance"])

@router.get("/predictions", response_model=List[NodeDegradationPrediction])
def get_node_predictions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return evaluate_node_degradations(db)

@router.post("/recommend", response_model=RecommendationResponse)
def propose_assurance_dispatch(
    req: RecommendAssuranceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    node = db.query(Node).filter(Node.id == req.node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    predictions = evaluate_node_degradations(db)
    node_pred = next((p for p in predictions if p.node_id == req.node_id), None)
    
    action_text = req.action_type or (node_pred.suggested_action if node_pred else f"Dispatch Field Tech to {node.node_name}")
    confidence = 0.94 if (node_pred and node_pred.degradation_risk_score > 60) else 0.86

    rec = create_or_get_recommendation(
        db=db,
        source_module="Predictive Service Assurance",
        target_entity_type="Node",
        target_entity_id=node.id,
        target_entity_label=f"{node.node_name} ({node.area})",
        title=f"Proactive Field Dispatch - {node.node_name}",
        description=f"AI detected high degradation risk ({node_pred.degradation_risk_score if node_pred else 75:.1f}%). {action_text}",
        recommended_action=action_text,
        confidence_score=confidence,
        action_payload={
            "node_code": node.node_code,
            "area": node.area,
            "health_score": node.health_score,
            "optical_power": node.optical_power_dbm,
            "utilization": node.utilization_pct,
            "impacted_customers": node_pred.impacted_customers_count if node_pred else 0,
            "signals": [s.model_dump() if hasattr(s, 'model_dump') else s.dict() if hasattr(s, 'dict') else s for s in (node_pred.contributing_signals if node_pred else [])]
        }
    )
    return rec
