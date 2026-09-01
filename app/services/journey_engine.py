from typing import List, Dict, Any
from sqlalchemy.orm import Session
from app.models import Customer, UsageRecord, Ticket, Invoice, Recommendation
from app.schemas import JourneyCustomerItem, ContributingSignal, JourneyStageStat, JourneyFunnelSummaryResponse

LIFECYCLE_STAGES = ['Acquisition', 'Install', 'Use', 'Renewal', 'Complaint', 'Win-back']

def evaluate_customer_journeys(db: Session) -> List[JourneyCustomerItem]:
    customers = db.query(Customer).all()
    journey_items = []

    # Pre-fetch pending recommendations to optimize DB roundtrips
    pending_recs = {
        r.target_entity_id: r
        for r in db.query(Recommendation).filter(
            Recommendation.source_module == 'Intelligent Customer Journeys',
            Recommendation.target_entity_type == 'Customer',
            Recommendation.status == 'PENDING'
        ).all()
    }

    for c in customers:
        stage = c.current_stage or 'Use'
        usage = db.query(UsageRecord).filter(UsageRecord.customer_id == c.id).first()
        tickets = db.query(Ticket).filter(Ticket.customer_id == c.id).order_by(Ticket.created_at.desc()).all()

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
            value=f"{c.nps_score}/10 NPS",
            weight="+20 pts" if c.nps_score <= 4 or c.nps_score >= 9 else "+10 pts",
            detail="High promoter" if c.nps_score >= 9 else "Detractor" if c.nps_score <= 6 else "Passive",
            impact_type="positive" if c.nps_score >= 8 else "negative" if c.nps_score <= 5 else "neutral"
        ))

        # Next-Best-Action Decision Logic
        if stage == 'Acquisition':
            nba = "Send 1-Click Digital KYC & Fiber Installation Slot Scheduler"
            reason = "Prospect completed registration form; pending installation slot selection."
            channel = "WhatsApp Interactive Message"
            confidence = 0.94
            urgency = "High"
        elif stage == 'Install':
            nba = "Trigger Automated ONT Self-Test & Field Engineer Check-in"
            reason = "Installation completed in last 48h; verify nominal optical power levels."
            channel = "SMS & Automated IVR"
            confidence = 0.91
            urgency = "Medium"
        elif stage == 'Complaint':
            cat_name = open_tickets[0].category if open_tickets else 'connectivity'
            nba = f"Proactive SLA Credit Guarantee (INR 250) + Priority VIP Technician Escalation for {cat_name}"
            reason = f"Active {cat_name.lower()} incident impacting user experience; NPS detractor risk."
            channel = "Direct Phone Call by Care Lead"
            confidence = 0.96
            urgency = "Critical"
        elif stage == 'Renewal':
            nba = f"Offer 15-Month Annual Loyalty Plan (Pay for 10 months, get 5 free) for {c.plan_name}"
            reason = f"Tenure {c.tenure_months} months; contract renewal upcoming in next 15 days."
            channel = "Email & Customer Portal Banner"
            confidence = 0.89
            urgency = "High"
        elif stage == 'Win-back':
            nba = f"Offer 'Zero-Deposit Fiber Reconnect' with 300 Mbps trial speed + 50% discount"
            reason = f"Dormant/churned account with historical ARPU of INR {c.arpu:,.0f}."
            channel = "Direct Tele-Sales & Executive Outreach"
            confidence = 0.82
            urgency = "High"
        else: # 'Use' stage
            if usage and usage.monthly_gb > usage.quota_gb * 0.85:
                nba = "Propose Turbo Gigafiber Speed & Unlimited FUP Upgrade"
                reason = f"Consuming {usage.monthly_gb:.0f}GB ({int(usage.monthly_gb/usage.quota_gb*100)}% of quota)."
                channel = "Jeebr Self-Care App"
                confidence = 0.88
                urgency = "Medium"
            elif c.nps_score >= 9:
                nba = "Invite to 'Jeebr Fiber Ambassador' Referral Program (Earn INR 500 bill credit per invite)"
                reason = f"High promoter score (NPS {c.nps_score}/10)."
                channel = "WhatsApp Message"
                confidence = 0.92
                urgency = "Low"
            else:
                nba = "Deliver Monthly Digital Health Summary & Complimentary OTT Activation"
                reason = "Regular monthly engagement cycle."
                channel = "Email Newsletter"
                confidence = 0.80
                urgency = "Low"

        has_pending = c.id in pending_recs

        journey_items.append(JourneyCustomerItem(
            customer_id=c.id,
            customer_code=c.customer_code,
            name=c.name,
            locality=c.locality,
            segment=c.segment,
            plan_name=c.plan_name,
            current_stage=stage,
            tenure_months=c.tenure_months,
            nps_score=c.nps_score,
            next_best_action=nba,
            action_reason=reason,
            suggested_channel=channel,
            confidence_score=confidence,
            urgency_level=urgency,
            contributing_signals=signals,
            has_pending_recommendation=has_pending
        ))

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
        {"channel": "Jeebr Self-Care App", "share_pct": 28.0, "conversion_rate": "64%"},
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

