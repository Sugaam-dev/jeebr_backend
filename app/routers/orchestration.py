from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Ticket, User
from app.schemas import OrchestrationTicketItem, RecommendOrchestrationRequest, RecommendationResponse
from app.auth import get_current_user, require_roles
from app.services.orchestration_engine import evaluate_ticket_orchestrations
from app.services.governance_service import create_or_get_recommendation

router = APIRouter(prefix="/orchestration", tags=["AI-driven OSS/BSS Orchestration"])

@router.get("/queue", response_model=List[OrchestrationTicketItem])
def get_orchestration_queue(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return evaluate_ticket_orchestrations(db)

@router.post("/recommend", response_model=RecommendationResponse)
def propose_orchestration_workflow(
    req: RecommendOrchestrationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    ticket = db.query(Ticket).filter(Ticket.id == req.ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    items = evaluate_ticket_orchestrations(db)
    matched = next((i for i in items if i.ticket_id == ticket.id), None)
    
    action_text = req.workflow_action or (matched.recommended_orchestration if matched else "Trigger OSS Remediation")
    confidence = matched.confidence_score if matched else 0.92

    rec = create_or_get_recommendation(
        db=db,
        source_module="AI-driven OSS/BSS Orchestration",
        target_entity_type="Ticket",
        target_entity_id=ticket.id,
        target_entity_label=f"Ticket {ticket.ticket_code} ({ticket.category})",
        title=f"OSS Triage - {matched.workflow_type if matched else 'Workflow'} ({ticket.ticket_code})",
        description=f"AI triage recommendation for {ticket.category} complaint: {action_text}",
        recommended_action=action_text,
        confidence_score=confidence,
        action_payload={
            "ticket_code": ticket.ticket_code,
            "category": ticket.category,
            "priority": ticket.priority,
            "workflow_type": matched.workflow_type if matched else "Automated",
            "signals": [s.model_dump() for s in matched.contributing_signals] if matched else []
        }
    )
    return rec
