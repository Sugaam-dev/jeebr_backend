from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.models import Recommendation, AuditLog, User, Ticket, Customer, Node, Invoice

def create_or_get_recommendation(
    db: Session,
    source_module: str,
    target_entity_type: str,
    target_entity_id: int,
    target_entity_label: str,
    title: str,
    description: str,
    recommended_action: str,
    confidence_score: float,
    action_payload: Optional[Dict[str, Any]] = None
) -> Recommendation:
    # Check if already exists in PENDING status
    existing = db.query(Recommendation).filter(
        Recommendation.source_module == source_module,
        Recommendation.target_entity_type == target_entity_type,
        Recommendation.target_entity_id == target_entity_id,
        Recommendation.status == 'PENDING'
    ).first()

    if existing:
        return existing

    rec = Recommendation(
        source_module=source_module,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        target_entity_label=target_entity_label,
        title=title,
        description=description,
        recommended_action=recommended_action,
        action_payload=action_payload or {},
        confidence_score=confidence_score,
        status='PENDING',
        created_at=datetime.utcnow()
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec

def approve_recommendation(
    db: Session,
    recommendation_id: int,
    user: User,
    notes: Optional[str] = None
) -> Recommendation:
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if not rec:
        raise ValueError("Recommendation not found")

    rec.status = 'APPROVED'
    rec.reviewed_by_id = user.id
    rec.reviewed_at = datetime.utcnow()
    rec.review_notes = notes or f"Approved by {user.full_name} ({user.role})"

    # Simulate downstream action execution
    execution_result = simulate_execution(db, rec)
    rec.status = 'EXECUTED'

    # Create Audit Log
    audit = AuditLog(
        recommendation_id=rec.id,
        source_module=rec.source_module,
        action_taken=rec.recommended_action,
        decision='APPROVED',
        user_id=user.id,
        user_name=user.full_name,
        user_role=user.role,
        confidence_score=rec.confidence_score,
        original_signals=rec.action_payload.get('signals', {"target": rec.target_entity_label}),
        execution_result=execution_result,
        timestamp=datetime.utcnow()
    )
    db.add(audit)
    db.commit()
    db.refresh(rec)
    return rec

def reject_recommendation(
    db: Session,
    recommendation_id: int,
    user: User,
    notes: Optional[str] = None
) -> Recommendation:
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if not rec:
        raise ValueError("Recommendation not found")

    rec.status = 'REJECTED'
    rec.reviewed_by_id = user.id
    rec.reviewed_at = datetime.utcnow()
    rec.review_notes = notes or f"Rejected by {user.full_name} ({user.role})"

    # Create Audit Log
    audit = AuditLog(
        recommendation_id=rec.id,
        source_module=rec.source_module,
        action_taken=rec.recommended_action,
        decision='REJECTED',
        user_id=user.id,
        user_name=user.full_name,
        user_role=user.role,
        confidence_score=rec.confidence_score,
        original_signals=rec.action_payload.get('signals', {"target": rec.target_entity_label}),
        execution_result={"status": "Rejected by reviewer", "notes": notes},
        timestamp=datetime.utcnow()
    )
    db.add(audit)
    db.commit()
    db.refresh(rec)
    return rec

def simulate_execution(db: Session, rec: Recommendation) -> Dict[str, Any]:
    # Simulate execution on underlying entities based on source module and entity type
    if rec.source_module == 'Predictive Service Assurance' or rec.target_entity_type == 'Node':
        node = db.query(Node).filter(Node.id == rec.target_entity_id).first()
        if node:
            # Calibrate / improve node
            node.health_score = min(100.0, node.health_score + 25.0)
            if node.health_score > 70.0:
                node.status = 'Healthy'
            db.commit()
        return {
            "action": f"Field Dispatch Order #FDO-2026-{100 + rec.id} dispatched to Mumbai Area Engineering Team",
            "status": "Dispatched",
            "telemetry_calibration": "OTDR line trace initiated; optical attenuation restored to -21.4 dBm"
        }

    elif rec.source_module == 'AI-driven OSS/BSS Orchestration' or rec.target_entity_type == 'Ticket':
        ticket = db.query(Ticket).filter(Ticket.id == rec.target_entity_id).first()
        if ticket:
            ticket.status = 'Resolved'
            ticket.resolved_at = datetime.utcnow()
            db.commit()
        return {
            "action": f"Automated workflow #{ticket.ticket_code if ticket else 'TCK-1'} executed via TR-069 / OSS orchestrator",
            "status": "Resolved",
            "orchestration_time": "3.8 seconds"
        }

    elif rec.source_module == 'Revenue Assurance & Leakage Analytics' or rec.target_entity_type == 'Invoice':
        invoice = db.query(Invoice).filter(Invoice.id == rec.target_entity_id).first()
        if invoice:
            invoice.anomaly_flag = False
            invoice.status = 'Paid'
            db.commit()
        return {
            "action": f"Billing adjustment for Invoice #{invoice.invoice_code if invoice else 'INV'} posted to SAP BRIM ledger",
            "status": "Adjusted",
            "ledger_sync": "Catalog aligned; differential invoice generated"
        }

    elif rec.source_module == 'Intelligent Customer Journeys':
        cust = db.query(Customer).filter(Customer.id == rec.target_entity_id).first()
        if cust:
            cust.nps_score = min(10, cust.nps_score + 2)
            if cust.current_stage == 'Complaint':
                cust.current_stage = 'Use'
            db.commit()
        return {
            "action": f"Omnichannel Next-Best-Action triggered via WhatsApp Business API & PMRG Self-Care App for {cust.name if cust else 'subscriber'}",
            "status": "Action Triggered",
            "channel_delivery": "Instant WhatsApp notification delivered (Receipt ID #WA-9821)"
        }

    elif rec.source_module == 'Churn Prediction & Retention AI' or rec.target_entity_type == 'Customer':
        cust = db.query(Customer).filter(Customer.id == rec.target_entity_id).first()
        if cust:
            cust.nps_score = min(10, cust.nps_score + 2)
            cust.status = 'Active'
            db.commit()
        return {
            "action": f"Retention save package & 20% billing credit applied to {cust.name if cust else 'subscriber'} in SAP BRIM",
            "status": "Retention Active",
            "care_outreach": "Priority Relationship Manager satisfaction call scheduled"
        }

    return {"action": "Simulated task completed successfully", "status": "Success"}
