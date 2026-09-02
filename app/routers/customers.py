from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import Customer, Node, UsageRecord, Ticket, Invoice, Recommendation
from app.schemas import CustomerListResponse, Customer360Response, UsageSummary, TicketSummary, InvoiceSummary, NodeResponse
from app.auth import get_current_user
from app.services.churn_engine import evaluate_customer_signals
from app.services.journey_engine import evaluate_single_customer_journey

router = APIRouter(prefix="/customers", tags=["Customers & 360"])

@router.get("", response_model=List[CustomerListResponse])
def list_customers(
    search: Optional[str] = None,
    locality: Optional[str] = None,
    segment: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    query = db.query(Customer).options(joinedload(Customer.node))
    if search:
        query = query.filter(
            (Customer.name.ilike(f"%{search}%")) |
            (Customer.customer_code.ilike(f"%{search}%")) |
            (Customer.email.ilike(f"%{search}%"))
        )
    if locality:
        query = query.filter(Customer.locality == locality)
    if segment:
        query = query.filter(Customer.segment == segment)
    if status:
        query = query.filter(Customer.status == status)

    customers = query.limit(limit).all()
    results = []
    for c in customers:
        node_name = c.node.node_name if c.node else None
        results.append(CustomerListResponse(
            id=c.id,
            customer_code=c.customer_code,
            name=c.name,
            email=c.email,
            phone=c.phone,
            locality=c.locality,
            segment=c.segment,
            plan_name=c.plan_name,
            arpu=c.arpu,
            tenure_months=c.tenure_months,
            status=c.status,
            current_stage=c.current_stage or 'Use',
            nps_score=c.nps_score,
            node_id=c.node_id,
            node_name=node_name
        ))
    return results

@router.get("/{customer_id}/360", response_model=Customer360Response)
def get_customer_360(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    node = db.query(Node).filter(Node.id == customer.node_id).first() if customer.node_id else None
    usage = db.query(UsageRecord).filter(UsageRecord.customer_id == customer.id).first()
    tickets = db.query(Ticket).filter(Ticket.customer_id == customer.id).order_by(Ticket.created_at.desc()).limit(10).all()
    invoices = db.query(Invoice).filter(Invoice.customer_id == customer.id).order_by(Invoice.due_date.desc()).limit(10).all()

    # Calculate churn score and factor breakdown (reusing in-memory data)
    score, risk_lvl, conf, factors, action, _ = evaluate_customer_signals(
        customer, usage, tickets, invoices, node
    )

    # Calculate next-best-action directly for this customer (zero extra DB queries)
    customer_journey = evaluate_single_customer_journey(customer, db, usage=usage, tickets=tickets)
    nba_payload = {
        "action": customer_journey.next_best_action if customer_journey else "Deliver Monthly Digital Health Summary",
        "reason": customer_journey.action_reason if customer_journey else "Standard lifecycle cycle",
        "channel": customer_journey.suggested_channel if customer_journey else "WhatsApp",
        "confidence": customer_journey.confidence_score if customer_journey else 0.85
    }

    # Active recommendations for this customer
    recs = db.query(Recommendation).filter(
        Recommendation.target_entity_type == 'Customer',
        Recommendation.target_entity_id == customer.id
    ).order_by(Recommendation.created_at.desc()).all()

    rec_list = []
    for r in recs:
        rec_list.append({
            "id": r.id,
            "module": r.source_module,
            "title": r.title,
            "action": r.recommended_action,
            "status": r.status,
            "confidence": r.confidence_score,
            "created_at": r.created_at.isoformat()
        })

    cust_summary = CustomerListResponse(
        id=customer.id,
        customer_code=customer.customer_code,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        locality=customer.locality,
        segment=customer.segment,
        plan_name=customer.plan_name,
        arpu=customer.arpu,
        tenure_months=customer.tenure_months,
        status=customer.status,
        current_stage=customer.current_stage or 'Use',
        nps_score=customer.nps_score,
        node_id=customer.node_id,
        node_name=node.node_name if node else None
    )

    node_res = None
    if node:
        node_res = NodeResponse(
            id=node.id,
            node_code=node.node_code,
            node_name=node.node_name,
            area=node.area,
            node_type=node.node_type,
            utilization_pct=node.utilization_pct,
            packet_loss_pct=node.packet_loss_pct,
            latency_ms=node.latency_ms,
            optical_power_dbm=node.optical_power_dbm,
            alarm_count=node.alarm_count,
            health_score=node.health_score,
            status=node.status,
            last_telemetry_at=node.last_telemetry_at,
            impacted_customers_count=len(node.customers)
        )

    usage_res = None
    if usage:
        usage_res = UsageSummary(
            monthly_gb=usage.monthly_gb,
            quota_gb=usage.quota_gb,
            usage_trend=usage.usage_trend,
            trend_pct=usage.trend_pct,
            ott_streaming_flag=usage.ott_streaming_flag,
            gaming_flag=usage.gaming_flag
        )

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

    invoice_summaries = [
        InvoiceSummary(
            id=inv.id,
            invoice_code=inv.invoice_code,
            billed_amount=inv.billed_amount,
            due_date=inv.due_date,
            paid_date=inv.paid_date,
            status=inv.status,
            waiver_amount=inv.waiver_amount,
            anomaly_flag=inv.anomaly_flag,
            anomaly_type=inv.anomaly_type
        ) for inv in invoices
    ]

    return Customer360Response(
        customer=cust_summary,
        node=node_res,
        usage=usage_res,
        recent_tickets=ticket_summaries,
        recent_invoices=invoice_summaries,
        churn_risk_score=score,
        churn_risk_level=risk_lvl,
        churn_factors=factors,
        next_best_action=nba_payload,
        active_recommendations=rec_list
    )
