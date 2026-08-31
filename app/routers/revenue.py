from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Invoice, User
from app.schemas import RevenueLeakageItem, RecommendRevenueRequest, RecommendationResponse
from app.auth import get_current_user, require_roles
from app.services.revenue_engine import detect_revenue_leakages
from app.services.governance_service import create_or_get_recommendation

router = APIRouter(prefix="/revenue", tags=["Revenue Assurance & Leakage Analytics"])

@router.get("/leakages", response_model=List[RevenueLeakageItem])
def get_revenue_leakages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return detect_revenue_leakages(db)

@router.post("/recommend", response_model=RecommendationResponse)
def propose_revenue_remediation(
    req: RecommendRevenueRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Revenue", "Executive", "Admin"]))
):
    invoice = db.query(Invoice).filter(Invoice.id == req.invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    items = detect_revenue_leakages(db)
    matched = next((i for i in items if i.invoice_id == invoice.id), None)
    
    action_text = req.remediation_action or (matched.recommended_action if matched else "Audit and reconcile ledger")
    confidence = matched.confidence_score if matched else 0.95

    rec = create_or_get_recommendation(
        db=db,
        source_module="Revenue Assurance & Leakage Analytics",
        target_entity_type="Invoice",
        target_entity_id=invoice.id,
        target_entity_label=f"Invoice {invoice.invoice_code} ({invoice.anomaly_type or 'Anomaly'})",
        title=f"Billing Remediation - ₹{invoice.leakage_amount:,.0f} Leakage",
        description=f"{matched.description if matched else 'Billing anomaly'}. Action: {action_text}",
        recommended_action=action_text,
        confidence_score=confidence,
        action_payload={
            "invoice_code": invoice.invoice_code,
            "anomaly_type": invoice.anomaly_type,
            "leakage_amount": invoice.leakage_amount,
            "billed_amount": invoice.billed_amount,
            "expected_amount": invoice.expected_amount,
            "signals": [s.model_dump() for s in matched.contributing_signals] if matched else []
        }
    )
    return rec
