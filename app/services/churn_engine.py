from typing import List, Tuple, Optional, Dict
from sqlalchemy.orm import Session
from app.models import Customer, UsageRecord, Ticket, Invoice, Node, Recommendation
from app.schemas import ChurnCustomerPrediction, ContributingSignal

def evaluate_customer_signals(
    customer: Customer,
    usage: Optional[UsageRecord],
    tickets: List[Ticket],
    invoices: List[Invoice],
    node: Optional[Node]
) -> Tuple[float, str, float, List[ContributingSignal], str, float]:
    score = 10.0
    factors: List[ContributingSignal] = []

    recent_tickets = [t for t in tickets if t.status in ['Open', 'In-Progress']]
    repeat_tickets = [t for t in tickets if t.repeat_flag]
    ticket_impact = 0.0
    if len(recent_tickets) > 0:
        ticket_impact += min(30.0, len(recent_tickets) * 15.0)
    if len(repeat_tickets) > 0:
        ticket_impact += min(15.0, len(repeat_tickets) * 10.0)
    
    if ticket_impact > 0:
        score += ticket_impact
        factors.append(ContributingSignal(
            signal="Complaint Recency & Repeat Incidents",
            value=f"{len(recent_tickets)} open, {len(repeat_tickets)} repeat",
            weight=f"+{ticket_impact:.0f} pts",
            detail=f"Subscriber logged {len(tickets)} total tickets with unresolved connectivity complaints.",
            impact_type="negative"
        ))

    usage_impact = 0.0
    if usage:
        if usage.usage_trend == 'Declining':
            drop_abs = abs(usage.trend_pct)
            usage_impact = min(30.0, (drop_abs / 100.0) * 45.0)
            score += usage_impact
            factors.append(ContributingSignal(
                signal="Bandwidth Consumption Drop",
                value=f"-{drop_abs:.1f}% drop ({usage.monthly_gb:.0f} GB used)",
                weight=f"+{usage_impact:.0f} pts",
                detail=f"Monthly consumption fell sharply below quota ({usage.quota_gb:.0f} GB allocated).",
                impact_type="negative"
            ))
        elif usage.usage_trend == 'Growing':
            score = max(0.0, score - 8.0)
            factors.append(ContributingSignal(
                signal="Active Traffic Growth",
                value=f"+{usage.trend_pct:.1f}% increase",
                weight="-8 pts",
                detail="Subscriber traffic steadily expanding month-over-month.",
                impact_type="positive"
            ))

    node_impact = 0.0
    if node and (node.status in ['Degraded', 'Critical'] or node.health_score < 70.0):
        node_impact = min(20.0, (100.0 - node.health_score) * 0.3)
        score += node_impact
        factors.append(ContributingSignal(
            signal="Upstream Node Health Exposure",
            value=f"{node.node_name} ({node.health_score:.0f}% health)",
            weight=f"+{node_impact:.0f} pts",
            detail=f"Subscriber connected to degraded area hub in {node.area} with {node.alarm_count} active alarms.",
            impact_type="negative"
        ))

    late_invoices = [inv for inv in invoices if inv.status in ['Late', 'Failed', 'Unpaid']]
    pay_impact = 0.0
    if len(late_invoices) > 0:
        pay_impact = min(25.0, len(late_invoices) * 12.5)
        score += pay_impact
        factors.append(ContributingSignal(
            signal="Payment Delays / Failed Billing",
            value=f"{len(late_invoices)} overdue invoices",
            weight=f"+{pay_impact:.0f} pts",
            detail="Billing payment delays correlate with subscriber churn intent.",
            impact_type="negative"
        ))

    if customer.tenure_months < 6:
        score += 8.0
        factors.append(ContributingSignal(
            signal="Early Lifecycle Vulnerability",
            value=f"{customer.tenure_months} months tenure",
            weight="+8 pts",
            detail="Accounts in first 6 months have 2.8x higher churn propensity.",
            impact_type="negative"
        ))
    elif customer.tenure_months > 24:
        score = max(0.0, score - 10.0)
        factors.append(ContributingSignal(
            signal="Long-Term Brand Loyalty",
            value=f"{customer.tenure_months} months tenure",
            weight="-10 pts",
            detail="Established subscriber with multi-year renewal history.",
            impact_type="positive"
        ))

    if customer.nps_score <= 4:
        score += 15.0
        factors.append(ContributingSignal(
            signal="CSAT / NPS Detractor Rating",
            value=f"{customer.nps_score}/10 NPS",
            weight="+15 pts",
            detail="Recent survey response flagged severe dissatisfaction.",
            impact_type="negative"
        ))

    final_score = min(99.0, max(5.0, score))
    confidence = min(0.98, max(0.82, final_score / 100.0 + 0.15))

    if final_score >= 70.0:
        risk_level = 'Critical'
        suggested_action = "Authorize VIP Account Manager Outreach + 20% Retention Concession & Proactive ONT Wi-Fi 6 Inspection"
    elif final_score >= 45.0:
        risk_level = 'High'
        suggested_action = "Apply Complimentary 30-Day Speed Boost (300 Mbps) & Schedule Care Satisfaction Follow-up"
    elif final_score >= 25.0:
        risk_level = 'Medium'
        suggested_action = "Deliver Annual Loyalty OTT Bundle Voucher (Disney+ Hotstar / Prime Video) on contract renewal"
    else:
        risk_level = 'Low'
        suggested_action = "Maintain standard engagement and quarterly digital health check-in"

    estimated_rev_loss = customer.arpu * 12.0

    return round(final_score, 1), risk_level, round(confidence, 2), factors, suggested_action, estimated_rev_loss

def calculate_customer_churn_score(customer: Customer, db: Session) -> Tuple[float, str, float, List[ContributingSignal], str, float]:
    usage = db.query(UsageRecord).filter(UsageRecord.customer_id == customer.id).first()
    tickets = db.query(Ticket).filter(Ticket.customer_id == customer.id).all()
    invoices = db.query(Invoice).filter(Invoice.customer_id == customer.id).all()
    node = db.query(Node).filter(Node.id == customer.node_id).first() if customer.node_id else None
    return evaluate_customer_signals(customer, usage, tickets, invoices, node)

def get_at_risk_customers(db: Session, min_score: float = 30.0) -> List[ChurnCustomerPrediction]:
    customers = db.query(Customer).filter(Customer.status != 'Churned').all()
    if not customers:
        return []

    # Batch fetch all related data in single round trips
    all_usage = {u.customer_id: u for u in db.query(UsageRecord).all()}
    
    all_tickets: Dict[int, List[Ticket]] = {}
    for t in db.query(Ticket).all():
        all_tickets.setdefault(t.customer_id, []).append(t)

    all_invoices: Dict[int, List[Invoice]] = {}
    for inv in db.query(Invoice).all():
        all_invoices.setdefault(inv.customer_id, []).append(inv)

    all_nodes = {n.id: n for n in db.query(Node).all()}

    pending_rec_ids = {
        r.target_entity_id for r in db.query(Recommendation.target_entity_id).filter(
            Recommendation.source_module == 'Churn Prediction & Retention AI',
            Recommendation.target_entity_type == 'Customer',
            Recommendation.status == 'PENDING'
        ).all()
    }

    results = []
    for c in customers:
        usage = all_usage.get(c.id)
        tickets = all_tickets.get(c.id, [])
        invoices = all_invoices.get(c.id, [])
        node = all_nodes.get(c.node_id) if c.node_id else None

        score, risk_lvl, conf, factors, action, rev_risk = evaluate_customer_signals(
            c, usage, tickets, invoices, node
        )
        if score >= min_score:
            has_pending = c.id in pending_rec_ids

            results.append(ChurnCustomerPrediction(
                customer_id=c.id,
                customer_code=c.customer_code,
                name=c.name,
                locality=c.locality,
                segment=c.segment,
                plan_name=c.plan_name,
                arpu=c.arpu,
                tenure_months=c.tenure_months,
                churn_risk_score=score,
                risk_level=risk_lvl,
                confidence_score=conf,
                top_factors=factors,
                suggested_retention_action=action,
                estimated_revenue_at_risk=rev_risk,
                has_pending_recommendation=has_pending
            ))

    results.sort(key=lambda x: x.churn_risk_score, reverse=True)
    return results
