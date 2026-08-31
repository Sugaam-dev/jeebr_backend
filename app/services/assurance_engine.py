from typing import List
from sqlalchemy.orm import Session
from app.models import Node, Customer, Ticket, Recommendation
from app.schemas import NodeDegradationPrediction, ContributingSignal

def evaluate_node_degradations(db: Session) -> List[NodeDegradationPrediction]:
    nodes = db.query(Node).all()
    predictions = []

    for node in nodes:
        impacted_customers = db.query(Customer).filter(Customer.node_id == node.id).all()
        impacted_count = len(impacted_customers)
        corp_count = sum(1 for c in impacted_customers if c.segment == 'ILL-Corporate')
        open_tickets = db.query(Ticket).filter(
            Ticket.node_id == node.id,
            Ticket.status.in_(['Open', 'In-Progress'])
        ).all()
        open_ticket_count = len(open_tickets)

        opt_penalty = 0.0
        if node.optical_power_dbm < -26.5:
            opt_penalty = min(35.0, abs(node.optical_power_dbm - (-24.0)) * 6.5)
        
        util_penalty = 0.0
        if node.utilization_pct > 75.0:
            util_penalty = min(25.0, (node.utilization_pct - 75.0) * 1.6)
        
        loss_penalty = min(25.0, node.packet_loss_pct * 8.5)
        alarm_penalty = min(20.0, node.alarm_count * 4.0)
        ticket_penalty = min(20.0, open_ticket_count * 3.5)

        degradation_score = min(99.0, opt_penalty + util_penalty + loss_penalty + alarm_penalty + ticket_penalty)
        health_score = max(5.0, 100.0 - degradation_score)

        if degradation_score >= 60.0:
            risk_level = 'Critical'
            suggested_action = f"Authorize emergency Tier-2 field technician dispatch to {node.node_name} ({node.area}) for optical transceiver calibration and splice enclosure inspection."
            suggested_eta = "2 Hours"
            confidence = 0.96
        elif degradation_score >= 35.0:
            risk_level = 'High'
            suggested_action = f"Schedule preventive backhaul port rebalancing and OTDR trace test on feeder line for {node.node_name}."
            suggested_eta = "6 Hours"
            confidence = 0.91
        elif degradation_score >= 15.0:
            risk_level = 'Medium'
            suggested_action = f"Flag node {node.node_name} for routine optical loss telemetry monitoring over next 24 hours."
            suggested_eta = "18 Hours"
            confidence = 0.88
        else:
            risk_level = 'Low'
            suggested_action = f"Telemetry nominal. Node operational within standard optical margin."
            suggested_eta = "24 Hours"
            confidence = 0.84

        contributing_signals = [
            ContributingSignal(
                signal="Optical Rx Power Attenuation",
                value=f"{node.optical_power_dbm:.1f} dBm",
                weight=f"+{opt_penalty:.1f} pts" if opt_penalty > 0 else "Nominal",
                detail=f"Threshold is -26.5 dBm. Current attenuation indicates fiber micro-bending." if opt_penalty > 0 else "Within nominal threshold (-18 to -24 dBm)",
                impact_type="negative" if opt_penalty > 0 else "neutral"
            ),
            ContributingSignal(
                signal="PON / Backhaul Utilization",
                value=f"{node.utilization_pct:.1f}%",
                weight=f"+{util_penalty:.1f} pts" if util_penalty > 0 else "Nominal",
                detail="High peak-hour saturation causing queue buffering." if util_penalty > 0 else "Traffic load within safe limits (<75%)",
                impact_type="negative" if util_penalty > 0 else "neutral"
            ),
            ContributingSignal(
                signal="Downlink Packet Loss Rate",
                value=f"{node.packet_loss_pct:.2f}%",
                weight=f"+{loss_penalty:.1f} pts" if loss_penalty > 0 else "Nominal",
                detail="Intermittent frame drops observed on uplink SFP interface." if loss_penalty > 0 else "Zero packet loss detected",
                impact_type="negative" if loss_penalty > 0 else "neutral"
            ),
            ContributingSignal(
                signal="Active Critical Alarms (24h)",
                value=f"{node.alarm_count} alarms",
                weight=f"+{alarm_penalty:.1f} pts" if alarm_penalty > 0 else "Nominal",
                detail=f"{node.alarm_count} SNMP telemetry trap alerts recorded in past 24 hours." if alarm_penalty > 0 else "Clean alarm log",
                impact_type="negative" if alarm_penalty > 0 else "neutral"
            ),
            ContributingSignal(
                signal="Downstream Incident Complaints",
                value=f"{open_ticket_count} active tickets",
                weight=f"+{ticket_penalty:.1f} pts" if ticket_penalty > 0 else "Nominal",
                detail=f"{impacted_count} subscribers ({corp_count} enterprise ILL) connected to this distribution segment.",
                impact_type="negative" if ticket_penalty > 0 else "neutral"
            )
        ]

        has_pending = db.query(Recommendation).filter(
            Recommendation.source_module == 'Predictive Service Assurance',
            Recommendation.target_entity_type == 'Node',
            Recommendation.target_entity_id == node.id,
            Recommendation.status == 'PENDING'
        ).first() is not None

        predictions.append(NodeDegradationPrediction(
            node_id=node.id,
            node_code=node.node_code,
            node_name=node.node_name,
            area=node.area,
            node_type=node.node_type,
            health_score=round(health_score, 1),
            degradation_risk_score=round(degradation_score, 1),
            risk_level=risk_level,
            confidence_score=round(confidence, 2),
            optical_power_dbm=node.optical_power_dbm,
            utilization_pct=node.utilization_pct,
            packet_loss_pct=node.packet_loss_pct,
            alarm_count=node.alarm_count,
            impacted_customers_count=impacted_count,
            impacted_corporate_count=corp_count,
            open_tickets_count=open_ticket_count,
            contributing_signals=contributing_signals,
            suggested_action=suggested_action,
            suggested_eta=suggested_eta,
            has_pending_recommendation=has_pending
        ))

    predictions.sort(key=lambda x: x.degradation_risk_score, reverse=True)
    return predictions
