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
from app.markets import get_current_market, MARKETS
from app.services.churn_engine import evaluate_customer_signals
from app.services.journey_engine import evaluate_single_customer_journey

router = APIRouter(prefix="/pilot-bundle", tags=["Recommended Pilot Bundle E2E Trace"])

@router.get("/scenario", response_model=PilotBundleScenarioResponse)
def get_pilot_bundle_scenario(
    node_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    target_node_code = node_code or MARKETS.get(market, MARKETS["mumbai"]).default_node
    # 1. Fetch target degraded node scoped to market
    node = db.query(Node).filter(Node.node_code == target_node_code, Node.market_id == market).first()
    if not node:
        node = db.query(Node).filter(Node.status.in_(['Critical', 'Degraded']), Node.market_id == market).first()
    if not node:
        node = db.query(Node).filter(Node.market_id == market).first()
    if not node:
        raise HTTPException(status_code=404, detail=f"No network nodes found for market {market}")

    # 2. Fetch impacted at-risk customer connected to this node
    customer = db.query(Customer).filter(
        Customer.node_id == node.id,
        Customer.status == 'At-Risk',
        Customer.market_id == market
    ).first()

    if not customer:
        customer = db.query(Customer).filter(Customer.node_id == node.id, Customer.market_id == market).first()
    if not customer:
        customer = db.query(Customer).filter(Customer.market_id == market).first()
    if not customer:
        raise HTTPException(status_code=404, detail=f"No customer records found for market {market}")

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

    # 6. Fetch related recommendations for both Node and Customer scoped to market
    recs = db.query(Recommendation).filter(
        Recommendation.market_id == market,
        (
            ((Recommendation.target_entity_type == 'Node') & (Recommendation.target_entity_id == node.id)) |
            ((Recommendation.target_entity_type == 'Customer') & (Recommendation.target_entity_id == customer.id))
        )
    ).order_by(Recommendation.created_at.desc()).all()

    # 7. Fetch related audit logs scoped to market
    audits = db.query(AuditLog).filter(
        AuditLog.market_id == market,
        AuditLog.source_module.in_([
            "Predictive Service Assurance",
            "Churn Prediction & Retention AI",
            "Intelligent Customer Journeys"
        ])
    ).order_by(AuditLog.timestamp.desc()).limit(10).all()

    tech_name = "Suresh Sawant (Mumbai Unit)" if market == "mumbai" else "Debabrata Mukherjee (Kolkata Unit)"

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
            description=f"Physical telemetry monitors optical power attenuation ({node.optical_power_dbm} dBm vs nominal -19 dBm) and backhaul saturation ({node.utilization_pct}%) on OLT port.",
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
            title=f"Cascading Churn Propensity: {customer.name}",
            subtitle=f"{customer.segment} ({customer.locality})",
            status="Alert Active",
            primary_metric=f"{churn_pred.churn_risk_score:.0f}%",
            primary_metric_label="Churn Risk",
            confidence_score=churn_pred.confidence_score,
            description=f"Subscriber {customer.name} experienced repeat speed/outage tickets downstream of degraded hub {node.node_name}. Churn risk jumped to {churn_pred.churn_risk_score:.0f}%.",
            entity_label=f"{customer.name} ({customer.customer_code})",
            signals=[
                {"factor": "Repeat Outage Incident", "weight": "+30 pts", "detail": f"Correlated with {node.node_code} optical degradation"},
                {"factor": "Bandwidth Usage Drop", "weight": "+25 pts", "detail": "Consumption declined over 35% vs baseline quota"},
                {"factor": "Enterprise SLA Exposure", "weight": "+20 pts", "detail": "Risk of high contractual billing penalty"}
            ],
            actions_available=[
                {"label": "Review Subscriber 360", "action": "view_customer360"},
                {"label": "Evaluate Retention Pack", "action": "eval_save_offer"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=3,
            loop_phase="Recommend",
            module_name="Intelligent Customer Journeys",
            title="Next-Best-Action Save Proposition",
            subtitle="Automated Retention Package",
            status="Recommended",
            primary_metric="84%",
            primary_metric_label="Save Probability",
            confidence_score=0.91,
            description=f"AI Journey Engine generates automated retention plan: {churn_pred.suggested_retention_action}.",
            entity_label=f"{customer.plan_name} Renewal",
            signals=[
                {"proposal": "Proactive Validity Extension", "detail": "3-day buffer to prevent automatic expiry churn"},
                {"proposal": "Speed Boost Voucher", "detail": "Temporary upgrade to compensate for service interruption"},
                {"proposal": "WhatsApp 1-Click Link", "detail": "Personalized renewal link with 20% concession"}
            ],
            actions_available=[
                {"label": "Preview WhatsApp Template", "action": "preview_wa"},
                {"label": "Customize Concession", "action": "custom_discount"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=4,
            loop_phase="Approve",
            module_name="Human-in-the-Loop AI Governance",
            title="Consolidated Multi-Domain Approval",
            subtitle="Dual Sign-off (NOC + Care)",
            status="Pending",
            primary_metric="2 Sign-offs",
            primary_metric_label="Pending Review",
            confidence_score=0.95,
            description=f"Two AI proposals staged: (1) NOC Lead approval for {node.node_name} field technician calibration, (2) Care Lead approval for {customer.name} retention package.",
            entity_label="Multi-Agent Governance Gate",
            signals=[
                {"domain": "NOC Engineering", "action": f"Field dispatch to {node.node_name}", "auth_role": "NOC"},
                {"domain": "Customer Care", "action": f"Retention offer for {customer.name}", "auth_role": "Care"}
            ],
            actions_available=[
                {"label": "Approve All Actions (1-Click)", "action": "approve_all"},
                {"label": "Inspect Explainability Factors", "action": "inspect_shap"}
            ]
        ),
        PilotBundleTraceStep(
            step_number=5,
            loop_phase="Execute",
            module_name="AI-driven OSS/BSS Orchestration",
            title="Simulated Downstream Execution",
            subtitle=f"Field Order #FDO-2026-981 & WhatsApp API",
            status="Executed",
            primary_metric="< 4.2s",
            primary_metric_label="Execution Latency",
            confidence_score=0.98,
            description=f"Upon human sign-off, the governance console executes simulated workflows: automated field technician dispatch to {node.area} and WhatsApp voucher delivery.",
            entity_label="Dispatched to Field & WhatsApp Delivered",
            signals=[
                {"system": "Field Service", "status": f"Work order dispatched to {node.area} team"},
                {"system": "WhatsApp Business API", "status": "Interactive message delivered with 1-click accept"},
                {"system": "SAP BRIM Billing", "status": "INR 250 downtime credit queued"}
            ],
            execution_receipt={
                "dispatch_id": "FDO-2026-981",
                "technician": tech_name,
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
            description=f"Immutable audit log recorded with reviewer identity, timestamp, and model confidence. Optical power restored on {node.node_code}; subscriber NPS recovered.",
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
        scenario_id=f"scenario-{market}-cascading-churn",
        scenario_title=f"{node.area} Optical Degradation Cascading to VIP Churn & SLA Complaint",
        scenario_summary=f"End-to-end operating loop demonstrating how SentinelOS Governed AI correlates physical layer optical attenuation on {node.node_code} to downstream subscriber churn risk, triggers proactive Next-Best-Action, routes both to the centralized Approval Console, and records an immutable audit trail.",
        node=NodeResponse.model_validate(node),
        impacted_customer=CustomerListResponse.model_validate(customer),
        churn_prediction=churn_pred,
        journey_item=matched_journey,
        related_tickets=ticket_summaries,
        related_recommendations=[RecommendationResponse.model_validate(r) for r in recs],
        related_audit_logs=[AuditLogResponse.model_validate(a) for a in audits],
        trace_steps=trace_steps
    )
