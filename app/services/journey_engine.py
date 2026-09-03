from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from app.models import Customer, UsageRecord, Ticket, Invoice, Recommendation
from app.schemas import JourneyCustomerItem, ContributingSignal, JourneyStageStat, JourneyFunnelSummaryResponse

LIFECYCLE_STAGES = ['Acquisition', 'Install', 'Use', 'Renewal', 'Complaint', 'Win-back']

def evaluate_single_customer_journey(
    customer: Customer,
    db: Session,
    usage: Optional[UsageRecord] = None,
    tickets: Optional[List[Ticket]] = None,
    has_pending: Optional[bool] = None
) -> JourneyCustomerItem:
    stage = customer.current_stage or 'Use'

    if usage is None:
        usage = db.query(UsageRecord).filter(UsageRecord.customer_id == customer.id).first()
    if tickets is None:
        tickets = db.query(Ticket).filter(Ticket.customer_id == customer.id).order_by(Ticket.created_at.desc()).all()
    if has_pending is None:
        has_pending = db.query(Recommendation.id).filter(
            Recommendation.source_module == 'Intelligent Customer Journeys',
            Recommendation.target_entity_type == 'Customer',
            Recommendation.target_entity_id == customer.id,
            Recommendation.status == 'PENDING'
        ).first() is not None

    nba = ""
    reason = ""
    channel = "WhatsApp & App Notification"
    confidence = 0.85
    urgency = "Medium"
    signals: List[ContributingSignal] = []

    # Build explainability signals
    signals.append(ContributingSignal(
        signal="Lifecycle Stage",
        value=stage,
        weight="+30 pts",
        detail=f"Customer current lifecycle phase: {stage}",
        impact_type="neutral"
    ))

    if usage:
        quota_pct = (usage.monthly_gb / usage.quota_gb * 100) if usage.quota_gb > 0 else 0
        signals.append(ContributingSignal(
            signal="Bandwidth Quota",
            value=f"{usage.monthly_gb:.0f}/{usage.quota_gb:.0f} GB ({quota_pct:.0f}%)",
            weight="+25 pts" if quota_pct > 80 else "+10 pts",
            detail=f"Usage trend: {usage.usage_trend} ({usage.trend_pct:+.1f}%)",
            impact_type="negative" if quota_pct > 85 else "positive" if quota_pct > 40 else "neutral"
        ))

    open_tickets = [t for t in tickets if t.status in ['Open', 'In-Progress']]
    if open_tickets:
        signals.append(ContributingSignal(
            signal="Active Tickets",
            value=f"{len(open_tickets)} open ({open_tickets[0].category})",
            weight="+35 pts",
            detail=f"Latest ticket #{open_tickets[0].ticket_code}: {open_tickets[0].description[:50]}...",
            impact_type="negative"
        ))

    signals.append(ContributingSignal(
        signal="NPS & Sentiment",
        value=f"{customer.nps_score}/10 NPS",
        weight="+20 pts" if customer.nps_score <= 4 or customer.nps_score >= 9 else "+10 pts",
        detail="High promoter" if customer.nps_score >= 9 else "Detractor" if customer.nps_score <= 6 else "Passive",
        impact_type="positive" if customer.nps_score >= 8 else "negative" if customer.nps_score <= 5 else "neutral"
    ))

    # Next-Best-Action Decision Logic
    is_prepaid = getattr(customer, 'customer_type', 'Prepaid') == 'Prepaid'
    if stage == 'Acquisition':
        if is_prepaid:
            nba = "Deliver 1-Click WhatsApp eSIM Activation & UPI First Recharge Offer"
            reason = "Prospect completed registration form; pending first recharge selection."
            channel = "WhatsApp Interactive Message"
        else:
            nba = "Send 1-Click Digital KYC & Fiber Installation Slot Scheduler"
            reason = "Prospect completed registration form; pending installation slot selection."
            channel = "WhatsApp Interactive Message"
        confidence = 0.94
        urgency = "High"
    elif stage == 'Install':
        if is_prepaid:
            nba = "Automated SIM Network Latency Test & Welcome 5GB Data Booster Voucher"
            reason = "SIM activated in last 48h; verify nominal 5G/4G coverage and latency."
            channel = "SMS & WhatsApp"
        else:
            nba = "Trigger Automated ONT Self-Test & Field Engineer Check-in"
            reason = "Installation completed in last 48h; verify nominal optical power levels."
            channel = "SMS & Automated IVR"
        confidence = 0.91
        urgency = "Medium"
    elif stage == 'Complaint':
        cat_name = open_tickets[0].category if open_tickets else 'connectivity'
        if is_prepaid:
            nba = f"Instant 5GB 5G High-Speed Compensation Pack + Priority Care Callback for {cat_name}"
            reason = f"Active {cat_name.lower()} incident impacting mobile streaming; NPS detractor risk."
            channel = "Direct Phone Call by Care Lead"
        else:
            nba = f"Proactive SLA Credit Guarantee (INR 250) + Priority VIP Technician Escalation for {cat_name}"
            reason = f"Active {cat_name.lower()} incident impacting user experience; NPS detractor risk."
            channel = "Direct Phone Call by Care Lead"
        confidence = 0.96
        urgency = "Critical"
    elif stage == 'Renewal':
        if is_prepaid:
            nba = f"Deliver 1-Click WhatsApp UPI Recharge Link with 5GB Bonus Voucher ({customer.plan_name})"
            reason = f"Prepaid pack expiring in {customer.days_to_expiry} days; facilitate frictionless renewal."
            channel = "WhatsApp Interactive UPI Link"
        else:
            nba = f"Offer 15-Month Annual Loyalty Plan (Pay for 10 months, get 5 free) for {customer.plan_name}"
            reason = f"Tenure {customer.tenure_months} months; contract renewal upcoming in next 15 days."
            channel = "Email & Customer Portal Banner"
        confidence = 0.89
        urgency = "High"
    elif stage == 'Win-back':
        historical_arpu = getattr(customer, 'actual_arpu', customer.arpu)
        if is_prepaid:
            nba = f"Deliver 'Reactivate SIM' Special Offer: 28 Days 1.5GB/Day at ₹199 (Save ₹100)"
            reason = f"Dormant/churned prepaid SIM with historical ARPU of INR {historical_arpu:,.0f}."
            channel = "WhatsApp & Direct Tele-Calling"
        else:
            nba = f"Offer 'Zero-Deposit Fiber Reconnect' with 300 Mbps trial speed + 50% discount"
            reason = f"Dormant/churned account with historical ARPU of INR {historical_arpu:,.0f}."
            channel = "Direct Tele-Sales & Executive Outreach"
        confidence = 0.82
        urgency = "High"
    else: # 'Use' stage
        if usage and usage.monthly_gb > usage.quota_gb * 0.85:
            if is_prepaid:
                nba = "Propose 5G Unlimited Daily Data Upgrade (2GB/Day Pack + 6GB Booster)"
                reason = f"Consuming {usage.monthly_gb:.0f}GB ({int(usage.monthly_gb/usage.quota_gb*100)}% of monthly quota)."
                channel = "MyJio / Airtel Thanks Push Notification"
            else:
                nba = "Propose Turbo Gigafiber Speed & Unlimited FUP Upgrade"
                reason = f"Consuming {usage.monthly_gb:.0f}GB ({int(usage.monthly_gb/usage.quota_gb*100)}% of quota)."
                channel = "PMRG Self-Care App"
            confidence = 0.88
            urgency = "Medium"
        elif customer.nps_score >= 9:
            nba = "Invite to 'PMRG 5G Ambassador' Referral Program (Earn INR 100 recharge credit per invite)"
            reason = f"High promoter score (NPS {customer.nps_score}/10)."
            channel = "WhatsApp Message"
            confidence = 0.92
            urgency = "Low"
        else:
            nba = "Deliver Monthly Digital Health Summary & Complimentary JioCinema/Hotstar OTT Voucher"
            reason = "Regular monthly engagement cycle."
            channel = "WhatsApp & App Notification"
            confidence = 0.80
            urgency = "Low"

    return JourneyCustomerItem(
        customer_id=customer.id,
        customer_code=customer.customer_code,
        name=customer.name,
        locality=customer.locality,
        segment=customer.segment,
        customer_type=getattr(customer, 'customer_type', 'Prepaid'),
        plan_name=customer.plan_name,
        plan_price=getattr(customer, 'plan_price', customer.arpu),
        revenue_30d=getattr(customer, 'revenue_30d', getattr(customer, 'actual_arpu', customer.arpu)),
        actual_arpu=getattr(customer, 'actual_arpu', customer.arpu),
        current_stage=stage,
        tenure_months=customer.tenure_months,
        nps_score=customer.nps_score,
        next_best_action=nba,
        action_reason=reason,
        suggested_channel=channel,
        confidence_score=confidence,
        urgency_level=urgency,
        contributing_signals=signals,
        has_pending_recommendation=has_pending
    )

def evaluate_customer_journeys(db: Session) -> List[JourneyCustomerItem]:
    customers = db.query(Customer).all()
    if not customers:
        return []

    # Batch fetch in single round-trips to eliminate N+1 latency
    all_usage = {u.customer_id: u for u in db.query(UsageRecord).all()}

    all_tickets: Dict[int, List[Ticket]] = {}
    for t in db.query(Ticket).order_by(Ticket.created_at.desc()).all():
        all_tickets.setdefault(t.customer_id, []).append(t)

    pending_rec_cust_ids = {
        r.target_entity_id for r in db.query(Recommendation.target_entity_id).filter(
            Recommendation.source_module == 'Intelligent Customer Journeys',
            Recommendation.target_entity_type == 'Customer',
            Recommendation.status == 'PENDING'
        ).all()
    }

    journey_items = []
    for c in customers:
        item = evaluate_single_customer_journey(
            customer=c,
            db=db,
            usage=all_usage.get(c.id),
            tickets=all_tickets.get(c.id, []),
            has_pending=(c.id in pending_rec_cust_ids)
        )
        journey_items.append(item)

    return journey_items

def get_journey_funnel_summary(db: Session) -> JourneyFunnelSummaryResponse:
    customers = db.query(Customer).all()
    total = len(customers) or 1

    stage_groups: Dict[str, List[Customer]] = {st: [] for st in LIFECYCLE_STAGES}
    for c in customers:
        st = c.current_stage if c.current_stage in stage_groups else 'Use'
        stage_groups[st].append(c)

    stage_stats = []
    for st in LIFECYCLE_STAGES:
        custs = stage_groups[st]
        cnt = len(custs)
        pct = round((cnt / total) * 100, 1)
        avg_nps = round(sum(c.nps_score for c in custs) / cnt, 1) if cnt > 0 else 8.0
        tot_arpu = round(sum(c.arpu for c in custs), 2)
        
        health = "Healthy"
        if st == 'Complaint':
            health = "Action Required" if cnt > 0 else "Nominal"
        elif st == 'Win-back':
            health = "Opportunity" if cnt > 0 else "Nominal"
        elif st in ['Acquisition', 'Install']:
            health = "Onboarding"

        stage_stats.append(JourneyStageStat(
            stage=st,
            count=cnt,
            percentage=pct,
            avg_nps=avg_nps,
            total_arpu=tot_arpu,
            health_status=health
        ))

    # Channel breakdown
    channel_counts = [
        {"channel": "WhatsApp Interactive", "share_pct": 38.5, "conversion_rate": "72%"},
        {"channel": "PMRG Self-Care App", "share_pct": 28.0, "conversion_rate": "64%"},
        {"channel": "Direct Phone Call", "share_pct": 18.2, "conversion_rate": "81%"},
        {"channel": "Email / Portal", "share_pct": 15.3, "conversion_rate": "42%"}
    ]

    active_proposals = db.query(Recommendation).filter(
        Recommendation.source_module == 'Intelligent Customer Journeys',
        Recommendation.status == 'PENDING'
    ).count()

    return JourneyFunnelSummaryResponse(
        total_customers=len(customers),
        stages=stage_stats,
        top_nba_channels=channel_counts,
        active_proposals_count=active_proposals
    )

