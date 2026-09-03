from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Node, Customer, Ticket, Recommendation, AuditLog, User, UsageRecord, Invoice
from app.schemas import (
    PilotBundleScenarioResponse, PilotBundleTraceStep, NodeResponse,
    CustomerListResponse, TicketSummary, RecommendationResponse,
    AuditLogResponse, ChurnCustomerPrediction, JourneyCustomerItem
)
from app.auth import get_current_user
from app.services.churn_engine import evaluate_customer_signals
from app.services.journey_engine import evaluate_single_customer_journey

router = APIRouter(prefix="/pilot-bundle", tags=["Recommended Pilot Bundle E2E Trace"])

@router.get("/scenario", response_model=PilotBundleScenarioResponse)
def get_pilot_bundle_scenario(
    node_code: Optional[str] = Query("OLT-BND-01"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Fetch target degraded node
    node = db.query(Node).filter(Node.node_code == node_code).first()
    if not node:
        node = db.query(Node).filter(Node.status.in_(['Critical', 'Degraded'])).first()
    if not node:
        node = db.query(Node).first()
    if not node:
        raise HTTPException(status_code=404, detail="No network nodes found")

    # 2. Fetch impacted at-risk customer connected to this node
    customer = db.query(Customer).filter(
        Customer.node_id == node.id,
        Customer.status == 'At-Risk'
    ).first()

    if not customer:
        customer = db.query(Customer).filter(Customer.node_id == node.id).first()
    if not customer:
        customer = db.query(Customer).first()
    if not customer:
        raise HTTPException(status_code=404, detail="No customer records found")

    # 3. Fetch related tickets
    tickets = db.query(Ticket).filter(
        Ticket.customer_id == customer.id
    ).order_by(Ticket.created_at.desc()).all()

    ticket_summaries = [
        TicketSummary(
            id=t.id,
            ticket_code=t.ticket_code,
            category=t.category,
            priority=t.priority,
            status=t.status,
            created_at=t.created_at,
            repeat_flag=t.repeat_flag,
            description=t.description
        ) for t in tickets
    ]

    # 4. Fetch usage and invoices for this customer once
    usage = db.query(UsageRecord).filter(UsageRecord.customer_id == customer.id).first()
    invoices = db.query(Invoice).filter(Invoice.customer_id == customer.id).all()

    # Compute Churn Prediction & Explainability without redundant DB queries
    score, risk_lvl, confidence, factors, suggested_save, rev_risk = evaluate_customer_signals(
        customer, usage, tickets, invoices, node
    )
    churn_pred = ChurnCustomerPrediction(
        customer_id=customer.id,
        customer_code=customer.customer_code,
        name=customer.name,
        locality=customer.locality,
        segment=customer.segment,
        customer_type=customer.customer_type or "Prepaid",
        plan_name=customer.plan_name,
        plan_price=customer.plan_price or customer.arpu,
        revenue_30d=customer.revenue_30d if hasattr(customer, 'revenue_30d') and customer.revenue_30d is not None else (customer.actual_arpu or customer.arpu),
        actual_arpu=customer.actual_arpu or customer.arpu,
        arpu=customer.actual_arpu or customer.arpu,
        recharge_validity_days=customer.recharge_validity_days or 28,
        days_to_expiry=customer.days_to_expiry if customer.days_to_expiry is not None else 14,
        validity_status=customer.validity_status or "Active",
        tenure_months=customer.tenure_months,
        churn_risk_score=score,
        risk_level=risk_lvl,
        confidence_score=confidence,
        top_factors=factors,
        suggested_retention_action=suggested_save,
        estimated_revenue_at_risk=rev_risk,
        has_pending_recommendation=False
    )

    # 5. Compute Journey NBA directly for this customer (zero extra DB queries)
    matched_journey = evaluate_single_customer_journey(customer, db, usage=usage, tickets=tickets)

    # 6. Fetch related recommendations for both Node and Customer
    recs = db.query(Recommendation).filter(
        (
            (Recommendation.target_entity_type == 'Node') & 
            (Recommendation.target_entity_id == node.id)
        ) | (
            (Recommendation.target_entity_type == 'Customer') & 
            (Recommendation.target_entity_id == customer.id)
        )
    ).order_by(Recommendation.created_at.desc()).all()

    # 7. Fetch related audit logs
    audits = db.query(AuditLog).filter(
        AuditLog.source_module.in_([
            "Predictive Service Assurance",
            "Churn Prediction & Retention AI",
            "Intelligent Customer Journeys"
        ])
    ).order_by(AuditLog.timestamp.desc()).limit(10).all()

    # 8. Build 6-step operating loop trace
    trace_steps = [
        PilotBundleTraceStep(
            step_number=1,
            loop_phase="Observe",
            module_name="Predictive Service Assurance",
            title=f"Telemetry Degradation on {node.node_name}",
            subtitle=f"Physical Layer Telemetry ({node.area})",
            status="Triggered",
            primary_metric=f"{node.optical_power_dbm} dBm",
            primary_metric_label="Optical Power",
            confidence_score=0.96,
            description=f"Physical telemetry monitors optical power attenuation (-29.8 dBm vs nominal -19 dBm) and backhaul saturation ({node.utilization_pct}%) on OLT port.",
            entity_label=f"{node.node_name} ({node.node_code})",
            signals=[
                {"signal": "Optical Power", "value": f"{node.optical_power_dbm} dBm", "detail": "High optical attenuation on feeder fiber"},
                {"signal": "Backhaul Utilization", "value": f"{node.utilization_pct}%", "detail": "Near capacity bottleneck"},
                {"signal": "Active Alarms", "value": f"{node.alarm_count} alarms", "detail": "Loss-of-signal & FEC errors"}
            ],
            actions_available=[
                {"label": "Inspect Telemetry Graph", "action": "inspect_telemetry"},
                {"label": "Run Remote OTDR Trace", "action": "run_otdr"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=2,
            loop_phase="Predict",
            module_name="Churn Prediction & Retention AI",
            title=f"Subscriber Churn Risk Escalation - {customer.name}",
            subtitle=f"Downstream Impact Correlation ({customer.locality})",
            status="Scored",
            primary_metric=f"{score:.1f}%",
            primary_metric_label="Churn Risk Score",
            confidence_score=0.93,
            description=f"AI model correlates physical node degradation to downstream subscriber complaints (3 outages) and 42% bandwidth drop, triggering High Churn Risk.",
            entity_label=f"{customer.name} ({customer.customer_code})",
            signals=[s.model_dump() for s in factors],
            actions_available=[
                {"label": "View Customer 360", "action": "open_360"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=3,
            loop_phase="Recommend",
            module_name="Intelligent Customer Journeys",
            title="Next-Best-Action & Retention Save Proposal",
            subtitle=f"Lifecycle Stage: {matched_journey.current_stage}",
            status="Queued",
            primary_metric=matched_journey.suggested_channel,
            primary_metric_label="Target Channel",
            confidence_score=matched_journey.confidence_score,
            description=f"Journey engine matches subscriber complaint stage with proactive Next-Best-Action: {matched_journey.next_best_action}. Reason: {matched_journey.action_reason}",
            entity_label=f"NBA: {matched_journey.next_best_action[:45]}...",
            signals=[s.model_dump() for s in matched_journey.contributing_signals],
            actions_available=[
                {"label": "Propose to Governance Queue", "action": "propose_nba"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=4,
            loop_phase="Approve",
            module_name="Human-in-the-Loop AI Governance",
            title="Centralized Human Sign-Off Console",
            subtitle="Dual-domain authorization required",
            status="Queued",
            primary_metric="2 Actions",
            primary_metric_label="Pending Review",
            confidence_score=0.95,
            description="Both the physical field dispatch order (NOC domain) and the proactive customer retention save offer (Care domain) converge into the central governance queue before simulated execution.",
            entity_label="Queue ID #PSA-1 & #ICJ-2",
            signals=[
                {"domain": "NOC Sign-off", "target": node.node_name, "action": "Emergency Splicing Dispatch"},
                {"domain": "Care Sign-off", "target": customer.name, "action": "Proactive SLA Credit & Save Offer"}
            ],
            actions_available=[
                {"label": "Approve Field Dispatch", "action": "approve_assurance"},
                {"label": "Approve Customer Save", "action": "approve_journey"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=5,
            loop_phase="Execute",
            module_name="AI-driven OSS/BSS Orchestration",
            title="Simulated Downstream Execution",
            subtitle="Field Order #FDO-2026-981 & WhatsApp API",
            status="Executed",
            primary_metric="< 4.2s",
            primary_metric_label="Execution Latency",
            confidence_score=0.98,
            description="Upon human sign-off, the governance console executes simulated workflows: automated field technician dispatch to Bandra Hub and WhatsApp voucher delivery.",
            entity_label="Dispatched to Field & WhatsApp Delivered",
            signals=[
                {"system": "Field Service", "status": "Work order #FDO-981 dispatched to Bandra team"},
                {"system": "WhatsApp Business API", "status": "Interactive message delivered with 1-click accept"},
                {"system": "SAP BRIM Billing", "status": "INR 250 downtime credit queued"}
            ],
            execution_receipt={
                "dispatch_id": "FDO-2026-981",
                "technician": "Suresh Sawant (Bandra Unit)",
                "channel_receipt": "WA-MSG-2026-8819",
                "execution_time_seconds": 3.8
            }
        ),
        PilotBundleTraceStep(
            step_number=6,
            loop_phase="Learn",
            module_name="Governance Audit & Closed-Loop Feedback",
            title="Immutable Audit Trail & Telemetry Recalibration",
            subtitle="PostgreSQL Audit Ledger + KPI Recovery",
            status="Completed",
            primary_metric="+2 NPS",
            primary_metric_label="Subscriber Recovery",
            confidence_score=0.99,
            description="Immutable audit log recorded with reviewer identity, timestamp, and model confidence. Optical power restored to -21 dBm; subscriber NPS improved from 3 to 7.",
            entity_label="Audit Event #AUDIT-2026-09",
            signals=[
                {"metric": "Node Health", "before": "38.0 (Critical)", "after": "92.5 (Healthy)"},
                {"metric": "Customer NPS", "before": "3/10 (Detractor)", "after": "7/10 (Passive)"},
                {"metric": "Churn Risk", "before": "88.5%", "after": "18.2% (Retained)"}
            ],
            execution_receipt={
                "audit_id": 901,
                "recorded_at": "PostgreSQL Ledger",
                "retention_status": "Secured for 12 Months"
            }
        )
    ]

    return PilotBundleScenarioResponse(
        scenario_id="scenario-bandra-cascading-churn",
        scenario_title="Bandra West Optical Degradation Cascading to VIP Churn & SLA Complaint",
        scenario_summary="End-to-end operating loop demonstrating how PMRG Governed AI correlates physical layer optical attenuation on OLT-BND-01 to downstream subscriber churn risk, triggers proactive Next-Best-Action, routes both to the centralized Approval Console, and records an immutable audit trail.",
        node=NodeResponse.model_validate(node),
        impacted_customer=CustomerListResponse.model_validate(customer),
        churn_prediction=churn_pred,
        journey_item=matched_journey,
        related_tickets=ticket_summaries,
        related_recommendations=[RecommendationResponse.model_validate(r) for r in recs],
        related_audit_logs=[AuditLogResponse.model_validate(a) for a in audits],
        trace_steps=trace_steps
    )
