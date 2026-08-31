from typing import List
from sqlalchemy.orm import Session
from app.models import Customer, UsageRecord, Ticket, Recommendation
from app.schemas import JourneyCustomerItem

def evaluate_customer_journeys(db: Session) -> List[JourneyCustomerItem]:
    customers = db.query(Customer).all()
    journey_items = []

    for c in customers:
        stage = c.current_stage or 'Use'
        usage = db.query(UsageRecord).filter(UsageRecord.customer_id == c.id).first()
        tickets = db.query(Ticket).filter(Ticket.customer_id == c.id).all()

        nba = ""
        reason = ""
        channel = "WhatsApp & App Notification"
        confidence = 0.85

        if stage == 'Acquisition':
            nba = "Send 1-Click Digital KYC & Fiber Installation Slot Scheduler"
            reason = "Prospect completed registration form; pending installation slot selection."
            channel = "WhatsApp Interactive Message"
            confidence = 0.94
        elif stage == 'Install':
            nba = "Trigger Automated ONT Self-Test & Field Engineer Check-in"
            reason = "Installation completed in last 48h; verify nominal optical power levels."
            channel = "SMS & Automated IVR"
            confidence = 0.91
        elif stage == 'Complaint':
            nba = "Proactive SLA Credit Guarantee + VIP Technician Escalation"
            reason = f"Customer has active complaint regarding {tickets[-1].category if tickets else 'connectivity'}."
            channel = "Direct Phone Call by Care Lead"
            confidence = 0.96
        elif stage == 'Renewal':
            nba = "Offer 15-Month Annual Loyalty Plan (Pay for 10 months, get 5 free)"
            reason = f"Tenure {c.tenure_months}m; contract renewal upcoming in next 15 days."
            channel = "Email & Customer Portal Banner"
            confidence = 0.89
        elif stage == 'Win-back':
            nba = "Offer 'Zero-Deposit Fiber Reconnect' with 300 Mbps trial speed"
            reason = "Dormant/churned account with high historical ARPU."
            channel = "Direct Tele-Sales & Executive Outreach"
            confidence = 0.82
        else: # 'Use'
            if usage and usage.monthly_gb > usage.quota_gb * 0.85:
                nba = "Propose Turbo Gigafiber Speed & Unlimited FUP Upgrade"
                reason = f"Consuming {usage.monthly_gb:.0f}GB ({int(usage.monthly_gb/usage.quota_gb*100)}% of quota)."
                channel = "Jeebr Self-Care App"
                confidence = 0.88
            elif c.nps_score >= 9:
                nba = "Invite to 'Jeebr Fiber Ambassador' Referral Program (Earn ₹500 bill credit per invite)"
                reason = f"High promoter score (NPS {c.nps_score}/10)."
                channel = "WhatsApp Message"
                confidence = 0.92
            else:
                nba = "Deliver Monthly Digital Health Summary & Complimentary OTT Activation"
                reason = "Regular monthly engagement cycle."
                channel = "Email Newsletter"
                confidence = 0.80

        has_pending = db.query(Recommendation).filter(
            Recommendation.source_module == 'Intelligent Customer Journeys',
            Recommendation.target_entity_type == 'Customer',
            Recommendation.target_entity_id == c.id,
            Recommendation.status == 'PENDING'
        ).first() is not None

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
            has_pending_recommendation=has_pending
        ))

    return journey_items
