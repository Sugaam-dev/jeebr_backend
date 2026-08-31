from typing import List, Tuple
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import Ticket, Customer, Node, Recommendation
from app.schemas import OrchestrationTicketItem, ContributingSignal

def evaluate_ticket_triage(ticket: Ticket, db: Session) -> Tuple[float, str, str, str, float, str, List[ContributingSignal]]:
    customer = db.query(Customer).filter(Customer.id == ticket.customer_id).first()
    node = db.query(Node).filter(Node.id == ticket.node_id).first() if ticket.node_id else None

    score = 20.0
    factors: List[ContributingSignal] = []

    if customer and customer.segment == 'ILL-Corporate':
        score += 25.0
        factors.append(ContributingSignal(
            signal="Enterprise SLA Contract",
            value="ILL-Corporate (99.9% SLA)",
            weight="+25 pts",
            detail="Corporate leased-line account with stringent 4-hour MTTR contract penalty.",
            impact_type="negative"
        ))

    if ticket.repeat_flag:
        score += 20.0
        factors.append(ContributingSignal(
            signal="Repeat Incident Recurrence",
            value="Repeat Ticket Flag Active",
            weight="+20 pts",
            detail="Second complaint logged within 7 days for identical connectivity issue.",
            impact_type="negative"
        ))

    node_degraded = False
    if node and (node.status in ['Degraded', 'Critical'] or node.health_score < 70.0):
        node_degraded = True
        score += 25.0
        factors.append(ContributingSignal(
            signal="Upstream Node Alarm Correlation",
            value=f"{node.node_name} ({node.optical_power_dbm:.1f} dBm)",
            weight="+25 pts",
            detail=f"Ticket directly correlates with {node.alarm_count} optical alarms on node {node.node_code}.",
            impact_type="negative"
        ))

    if ticket.category == 'Speed':
        workflow_type = "Automated BRAS QoS Sync & TR-069 ONT Reset"
        rec_action = "Trigger TR-069 remote ONT reset and push QoS bandwidth provisioning profile to BRAS cluster."
        confidence = 0.94
        factors.append(ContributingSignal(
            signal="Diagnostic Triage Pattern",
            value="Speed degradation / QoS desync",
            weight="QoS Sync Path",
            detail="High probability of bandwidth profile desync between BSS billing and BRAS gateway.",
            impact_type="neutral"
        ))
    elif ticket.category == 'Outage':
        if node_degraded:
            workflow_type = "Emergency Field Splicing Dispatch"
            rec_action = f"Correlate ticket to Node {node.node_name} optical attenuation; dispatch emergency fiber splicing crew."
            confidence = 0.97
            factors.append(ContributingSignal(
                signal="Bulk Outage Incident Linking",
                value=f"Linked to {node.node_name}",
                weight="Field Dispatch Path",
                detail="Upstream fiber trunk attenuation requires physical OTDR test and splice repair.",
                impact_type="negative"
            ))
        else:
            workflow_type = "Last-Mile Drop Cable Inspection"
            rec_action = "Dispatch field technician for subscriber drop-fiber splicing and ONT connector check."
            confidence = 0.89
            factors.append(ContributingSignal(
                signal="Last-Mile Physical Triage",
                value="Isolated single-subscriber drop issue",
                weight="Field Visit Path",
                detail="Node telemetry healthy; root cause isolated to subscriber building drop cable.",
                impact_type="neutral"
            ))
    elif ticket.category == 'Billing':
        workflow_type = "Automated SLA Credit & Statement Re-issue"
        rec_action = "Apply ₹150 SLA downtime credit adjustment and re-issue electronic tax invoice."
        confidence = 0.92
        factors.append(ContributingSignal(
            signal="Billing SLA Policy Verification",
            value="Downtime SLA Adjustment",
            weight="Auto-Credit Path",
            detail="Calculated downtime duration verified against CRM threshold; eligible for automated credit.",
            impact_type="neutral"
        ))
    elif ticket.category == 'Hardware':
        workflow_type = "Automated RMA & Router Replacement"
        rec_action = "Approve Wi-Fi 6 dual-band gigabit router replacement order with doorstep swap."
        confidence = 0.90
        factors.append(ContributingSignal(
            signal="Hardware Telemetry Failure",
            value="ONT Flash / Port Diagnostic Fault",
            weight="RMA Replacement Path",
            detail="TR-069 diagnostics indicate recurring packet CRC errors on ONT Ethernet port.",
            impact_type="negative"
        ))
    else:
        workflow_type = "Priority Installation Fast-Track"
        rec_action = "Re-assign priority slot to Mumbai Zone 1 fast-track installation crew."
        confidence = 0.88
        factors.append(ContributingSignal(
            signal="Provisioning Stage Triage",
            value="Pending fiber drop installation",
            weight="Fast-Track Path",
            detail="Field booking prioritized to ensure under-24h subscriber onboarding.",
            impact_type="neutral"
        ))

    final_score = min(99.0, max(15.0, score))

    if final_score >= 70.0:
        priority_level = 'Critical'
        sla_risk = 'Imminent'
    elif final_score >= 45.0:
        priority_level = 'High'
        sla_risk = 'High'
    else:
        priority_level = 'Medium'
        sla_risk = 'Nominal'

    return round(final_score, 1), priority_level, workflow_type, rec_action, round(confidence, 2), sla_risk, factors

def evaluate_ticket_orchestrations(db: Session) -> List[OrchestrationTicketItem]:
    tickets = db.query(Ticket).filter(Ticket.status.in_(['Open', 'In-Progress'])).all()
    queue = []

    for t in tickets:
        customer = db.query(Customer).filter(Customer.id == t.customer_id).first()
        node = db.query(Node).filter(Node.id == t.node_id).first() if t.node_id else None

        cust_name = customer.name if customer else "Unknown"
        cust_segment = customer.segment if customer else "Home Broadband"
        locality = customer.locality if customer else (node.area if node else "Mumbai")

        score, priority_lvl, wf_type, rec_action, conf, sla_risk, factors = evaluate_ticket_triage(t, db)

        has_pending = db.query(Recommendation).filter(
            Recommendation.source_module == 'AI-driven OSS/BSS Orchestration',
            Recommendation.target_entity_type == 'Ticket',
            Recommendation.target_entity_id == t.id,
            Recommendation.status == 'PENDING'
        ).first() is not None

        queue.append(OrchestrationTicketItem(
            ticket_id=t.id,
            ticket_code=t.ticket_code,
            customer_id=t.customer_id,
            customer_name=cust_name,
            customer_segment=cust_segment,
            locality=locality,
            category=t.category,
            priority=t.priority,
            status=t.status,
            created_at=t.created_at,
            repeat_flag=t.repeat_flag,
            description=t.description,
            triage_priority_score=score,
            priority_level=priority_lvl,
            recommended_orchestration=rec_action,
            workflow_type=wf_type,
            confidence_score=conf,
            sla_deadline=t.sla_deadline,
            sla_breach_risk=sla_risk,
            contributing_signals=factors,
            has_pending_recommendation=has_pending
        ))

    queue.sort(key=lambda x: x.triage_priority_score, reverse=True)
    return queue
