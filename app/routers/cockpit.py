from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import Customer, Node, Ticket, Invoice, Recommendation, AuditLog, User
from app.schemas import CockpitSummaryResponse, CockpitKPISummary, ModuleHealthStatus, AuditLogResponse
from app.auth import get_current_user
from app.services.churn_engine import get_at_risk_customers
from app.services.assurance_engine import evaluate_node_degradations

router = APIRouter(prefix="/cockpit", tags=["Executive Cockpit"])

@router.get("/summary", response_model=CockpitSummaryResponse)
def get_cockpit_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    total_custs = db.query(Customer).count()
    active_custs = db.query(Customer).filter(Customer.status == 'Active').count()
    
    at_risk_list = get_at_risk_customers(db, min_score=40.0)
    total_at_risk = len(at_risk_list)
    at_risk_monthly_rev = sum(c.arpu for c in at_risk_list)

    node_preds = evaluate_node_degradations(db)
    degraded_nodes = [n for n in node_preds if n.degradation_risk_score >= 35.0]
    degraded_node_count = len(degraded_nodes)
    impacted_cust_count = sum(n.impacted_customers_count for n in degraded_nodes)

    leakage_sum = db.query(func.sum(Invoice.leakage_amount)).filter(Invoice.anomaly_flag == True).scalar() or 0.0
    open_tickets = db.query(Ticket).filter(Ticket.status.in_(['Open', 'In-Progress'])).count()
    pending_recs = db.query(Recommendation).filter(Recommendation.status == 'PENDING').count()
    approved_recs = db.query(Recommendation).filter(Recommendation.status.in_(['APPROVED', 'EXECUTED'])).count()

    # Locality risk distribution
    localities = ["Bandra West", "Andheri East", "BKC", "Powai", "Lower Parel", "Dadar", "Malad West", "Thane West"]
    loc_dist = []
    for loc in localities:
        cust_cnt = db.query(Customer).filter(Customer.locality == loc).count()
        at_risk_cnt = sum(1 for c in at_risk_list if c.locality == loc)
        loc_dist.append({
            "locality": loc,
            "total_customers": cust_cnt,
            "at_risk_customers": at_risk_cnt,
            "risk_percentage": round((at_risk_cnt / max(1, cust_cnt)) * 100, 1)
        })

    # Churn risk band breakdown
    churn_bands = [
        {"band": "Critical (>70%)", "count": sum(1 for c in at_risk_list if c.churn_risk_score >= 70)},
        {"band": "High (45-70%)", "count": sum(1 for c in at_risk_list if 45 <= c.churn_risk_score < 70)},
        {"band": "Medium (25-45%)", "count": sum(1 for c in at_risk_list if 25 <= c.churn_risk_score < 45)},
        {"band": "Low (<25%)", "count": max(0, total_custs - total_at_risk)}
    ]

    # Leakage breakdown by anomaly type
    leakage_types = ["Plan Mismatch", "Duplicate Credit", "Unbilled Usage", "Dunning Failure"]
    leakage_dist = []
    for lt in leakage_types:
        amt = db.query(func.sum(Invoice.leakage_amount)).filter(
            Invoice.anomaly_flag == True,
            Invoice.anomaly_type == lt
        ).scalar() or 0.0
        cnt = db.query(Invoice).filter(
            Invoice.anomaly_flag == True,
            Invoice.anomaly_type == lt
        ).count()
        leakage_dist.append({"category": lt, "amount": amt, "count": cnt})

    # Module health statuses
    module_statuses = [
        ModuleHealthStatus(module_name="Predictive Service Assurance", status="Active (Real-time)", active_alerts=degraded_node_count, confidence_avg=0.92),
        ModuleHealthStatus(module_name="Churn Prediction & Retention AI", status="Active (Real-time)", active_alerts=total_at_risk, confidence_avg=0.91),
        ModuleHealthStatus(module_name="Intelligent Customer Journeys", status="Active (Rule-based)", active_alerts=12, confidence_avg=0.88),
        ModuleHealthStatus(module_name="AI-driven OSS/BSS Orchestration", status="Active (Queue-driven)", active_alerts=open_tickets, confidence_avg=0.94),
        ModuleHealthStatus(module_name="Revenue Assurance & Leakage Analytics", status="Active (Audit scan)", active_alerts=len(leakage_dist), confidence_avg=0.96),
        ModuleHealthStatus(module_name="Human-in-the-Loop AI Governance", status="Active (Governed)", active_alerts=pending_recs, confidence_avg=0.99),
    ]

    # Recent audits
    audits = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(8).all()
    audit_res = [AuditLogResponse.model_validate(a) for a in audits]

    kpis = CockpitKPISummary(
        total_active_customers=active_custs,
        total_at_risk_customers=total_at_risk,
        at_risk_monthly_revenue=at_risk_monthly_rev,
        open_degraded_nodes=degraded_node_count,
        customers_impacted_by_degradation=impacted_cust_count,
        total_detected_leakage_inr=leakage_sum,
        open_tickets_count=open_tickets,
        pending_governance_approvals=pending_recs,
        approved_actions_today=approved_recs,
        avg_approval_turnaround_mins=4.2
    )

    return CockpitSummaryResponse(
        kpis=kpis,
        module_statuses=module_statuses,
        locality_risk_distribution=loc_dist,
        churn_risk_distribution=churn_bands,
        leakage_by_category=leakage_dist,
        recent_audit_events=audit_res
    )
