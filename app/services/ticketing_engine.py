from datetime import datetime, timedelta
import random
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import Ticket, Resource, Customer, Node, User, AuditLog, Recommendation
from app.schemas import TicketCreateRequest, TicketDetailResponse, TicketingStatsResponse
from app.services.voice_alert_service import dispatch_voice_alert_for_ticket

# Global / market-level toggle state for Autonomous P3/P4 Auto-Dispatch (POC demo mode starts OFF so pending tickets are visible)
AUTO_DISPATCH_P3_P4_ENABLED: Dict[str, bool] = {
    "mumbai": False,
    "kolkata": False
}


def get_auto_dispatch_p3_p4_status(db: Session, market_id: str = "mumbai") -> Dict[str, Any]:
    """Get current status of autonomous P3/P4 dispatch toggle and pending count."""
    enabled = AUTO_DISPATCH_P3_P4_ENABLED.get(market_id, False)
    unassigned_count = db.query(Ticket).filter(
        Ticket.market_id == market_id,
        Ticket.priority.in_(["P3", "P4"]),
        Ticket.assigned_resource_id.is_(None),
        Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
    ).count()
    return {
        "enabled": enabled,
        "unassigned_p3_p4_count": unassigned_count,
        "market": market_id
    }


def auto_dispatch_unassigned_p3_p4_tickets(db: Session, market_id: str = "mumbai") -> List[Ticket]:
    """
    Finds all unassigned P3 & P4 tickets/alerts in the market and auto-assigns them
    to optimal regional field engineers immediately with zero human authorization.
    """
    tickets = db.query(Ticket).filter(
        Ticket.market_id == market_id,
        Ticket.priority.in_(["P3", "P4"]),
        Ticket.assigned_resource_id.is_(None),
        Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
    ).all()

    dispatched = []
    for t in tickets:
        if t.source == "INTERNAL":
            res = find_best_internal_resource(db, market_id)
        else:
            res = find_best_regional_resource(db, market_id, t.region)

        if res:
            t.assigned_resource_id = res.id
            t.assigned_at = datetime.utcnow()
            t.approval_status = "NOT_REQUIRED"
            t.status = "Assigned"
            res.active_tickets_count += 1
            t.ai_triage_action = f"Auto-assigned ({t.priority} Zero-Touch) to {res.name} ({res.region})"[:100]
            dispatched.append(t)
        else:
            t.ai_triage_action = "Queued for regional field dispatch (High Capacity Load)"

    if dispatched:
        db.commit()
        for t in dispatched:
            db.refresh(t)

    return dispatched


def set_auto_dispatch_p3_p4(db: Session, market_id: str, enabled: bool) -> Dict[str, Any]:
    """
    Updates the P3/P4 auto-dispatch toggle.
    When switched to True, automatically dispatches all pending P3/P4 tickets without authorization!
    """
    AUTO_DISPATCH_P3_P4_ENABLED[market_id] = enabled
    dispatched_tickets = []
    if enabled:
        dispatched_tickets = auto_dispatch_unassigned_p3_p4_tickets(db, market_id=market_id)

    unassigned_count = db.query(Ticket).filter(
        Ticket.market_id == market_id,
        Ticket.priority.in_(["P3", "P4"]),
        Ticket.assigned_resource_id.is_(None),
        Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
    ).count()

    return {
        "enabled": enabled,
        "unassigned_p3_p4_count": unassigned_count,
        "market": market_id,
        "dispatched_count": len(dispatched_tickets),
        "dispatched_ticket_codes": [t.ticket_code for t in dispatched_tickets]
    }


def normalize_priority(priority: str) -> str:
    """Normalize priority strings to P1, P2, P3, P4."""
    p = priority.strip().upper()
    if p in ["CRITICAL", "P1"]:
        return "P1"
    if p in ["HIGH", "P2"]:
        return "P2"
    if p in ["MEDIUM", "P3"]:
        return "P3"
    if p in ["LOW", "P4"]:
        return "P4"
    return "P3"


def find_best_internal_resource(db: Session, market_id: str) -> Optional[Resource]:
    """Find the best available internal team resource with the lowest ticket count."""
    # Look for INTERNAL resource in this market or universal
    res = db.query(Resource).filter(
        Resource.market_id == market_id,
        Resource.resource_type == "INTERNAL",
        Resource.status != "Offline"
    ).order_by(Resource.active_tickets_count.asc()).first()

    if not res:
        # Fallback to any internal resource
        res = db.query(Resource).filter(
            Resource.resource_type == "INTERNAL",
            Resource.status != "Offline"
        ).order_by(Resource.active_tickets_count.asc()).first()

    return res


def find_best_regional_resource(db: Session, market_id: str, region: Optional[str]) -> Optional[Resource]:
    """
    Find best available FIELD resource for the given region/locality.
    Prioritizes exact region/locality match with available capacity, then least loaded.
    """
    query = db.query(Resource).filter(
        Resource.market_id == market_id,
        Resource.resource_type == "FIELD",
        Resource.status != "Offline"
    )

    if region:
        region_clean = region.strip().lower()
        exact_matches = [
            r for r in query.all()
            if r.region.strip().lower() in region_clean or region_clean in r.region.strip().lower()
        ]
        if exact_matches:
            under_cap = [r for r in exact_matches if r.active_tickets_count < r.max_capacity]
            if under_cap:
                under_cap.sort(key=lambda r: r.active_tickets_count)
                return under_cap[0]
            exact_matches.sort(key=lambda r: r.active_tickets_count)
            return exact_matches[0]

    # Fallback to any available FIELD resource with remaining capacity
    under_cap_market = query.filter(Resource.active_tickets_count < Resource.max_capacity).order_by(Resource.active_tickets_count.asc()).first()
    if under_cap_market:
        return under_cap_market

    return query.order_by(Resource.active_tickets_count.asc()).first()


def generate_ticket_code(db: Session, market_id: str, source: str) -> str:
    """Generate a unique ticket code."""
    m_code = "MUM" if market_id == "mumbai" else "KOL"
    s_code = "INT" if source == "INTERNAL" else "REG"
    rnd = random.randint(1000, 9999)
    code = f"TCK-{m_code}-{s_code}-{rnd}"
    while db.query(Ticket).filter(Ticket.ticket_code == code).first():
        rnd = random.randint(1000, 9999)
        code = f"TCK-{m_code}-{s_code}-{rnd}"
    return code


def create_and_dispatch_ticket(
    db: Session,
    data: TicketCreateRequest,
    market_id: str = "mumbai",
    user: Optional[User] = None
) -> Ticket:
    """
    Core Automated Ticketing & Dispatch Engine:
    1. Internal ticket -> Auto-assign to Internal Team resource immediately.
    2. P3/P4 Customer ticket -> Auto-assign to Regional Field Resource without approval.
    3. P1/P2 Customer ticket -> Gated with PENDING_APPROVAL. Auto-assigned only AFTER approval.
    """
    p_norm = normalize_priority(data.priority)
    source = data.source.strip().upper() if data.source else "CUSTOMER"

    # Determine region from data, customer, or node
    region = data.region
    customer = None
    node = None

    if data.customer_id:
        customer = db.query(Customer).filter(Customer.id == data.customer_id).first()
        if customer and not region:
            region = customer.locality

    if data.node_id:
        node = db.query(Node).filter(Node.id == data.node_id).first()
        if node and not region:
            region = node.area

    if not region:
        region = "Internal Operations" if source == "INTERNAL" else "Regional Central"

    ticket_code = generate_ticket_code(db, market_id, source)

    # Calculate SLA deadline based on priority
    sla_hours = {"P1": 2, "P2": 4, "P3": 12, "P4": 24}.get(p_norm, 12)
    sla_deadline = datetime.utcnow() + timedelta(hours=sla_hours)

    ticket = Ticket(
        market_id=market_id,
        ticket_code=ticket_code,
        source=source,
        region=region,
        customer_id=data.customer_id,
        node_id=data.node_id,
        category=data.category,
        priority=p_norm,
        description=data.description,
        sla_deadline=sla_deadline,
        created_at=datetime.utcnow()
    )

    # 1. Internal Ticket -> Auto-assign to Internal Team resource
    if source == "INTERNAL":
        assigned_res = find_best_internal_resource(db, market_id)
        if assigned_res:
            ticket.assigned_resource_id = assigned_res.id
            ticket.assigned_at = datetime.utcnow()
            ticket.approval_status = "NOT_REQUIRED"
            ticket.status = "Assigned"
            assigned_res.active_tickets_count += 1
            ticket.ai_triage_action = f"Auto-assigned to Internal NOC Team: {assigned_res.name}"
        else:
            ticket.approval_status = "NOT_REQUIRED"
            ticket.status = "Open"
            ticket.ai_triage_action = "Queued for internal team allocation"

    # 2. Regional / Customer Ticket
    else:
        # P3 or P4 -> Auto-assign without approval to regional resource (if autonomous mode is ON)
        if p_norm in ["P3", "P4"]:
            is_auto_dispatch_on = AUTO_DISPATCH_P3_P4_ENABLED.get(market_id, False)
            if is_auto_dispatch_on:
                assigned_res = find_best_regional_resource(db, market_id, region)
                if assigned_res:
                    ticket.assigned_resource_id = assigned_res.id
                    ticket.assigned_at = datetime.utcnow()
                    ticket.approval_status = "NOT_REQUIRED"
                    ticket.status = "Assigned"
                    assigned_res.active_tickets_count += 1
                    ticket.ai_triage_action = f"Auto-assigned ({p_norm} Zero-Touch) to {assigned_res.name} ({assigned_res.region})"[:100]
                else:
                    ticket.approval_status = "NOT_REQUIRED"
                    ticket.status = "Open"
                    ticket.ai_triage_action = "Queued for regional field dispatch"
            else:
                ticket.assigned_resource_id = None
                ticket.approval_status = "NOT_REQUIRED"
                ticket.status = "Open"
                ticket.ai_triage_action = f"Pending Dispatch ({p_norm} Auto-Dispatch Paused)"[:100]

        # P1 or P2 -> Requires approval before assignment
        else:
            ticket.assigned_resource_id = None
            ticket.approval_status = "PENDING_APPROVAL"
            ticket.status = "Pending Approval"
            ticket.ai_triage_action = f"High-Impact Incident ({p_norm}): Managerial approval required before automated field dispatch"

    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    # If ticket requires managerial approval, synchronize with Governance & Audit layer
    if ticket.approval_status == "PENDING_APPROVAL":
        rec = Recommendation(
            market_id=market_id,
            source_module="Automatic Ticketing & Regional Dispatch",
            target_entity_type="Ticket",
            target_entity_id=ticket.id,
            target_entity_label=f"Ticket {ticket.ticket_code} ({ticket.priority} - {ticket.category})",
            title=f"Dispatch Approval Required: {ticket.ticket_code} ({ticket.priority})",
            description=f"High-impact incident ({ticket.priority}) reported in {ticket.region}. Requires supervisory sign-off before regional dispatch.",
            recommended_action=f"Approve automated field dispatch to optimal regional resource in {ticket.region}.",
            confidence_score=0.96,
            status="PENDING",
            created_at=datetime.utcnow()
        )
        db.add(rec)
        db.commit()

        # Trigger emergency automated voice call & alert dispatch to approving authority
        try:
            dispatch_voice_alert_for_ticket(db, ticket)
        except Exception as e:
            print(f"[WARN] Error dispatching automated voice alert: {e}")

    return ticket


def approve_and_assign_ticket(
    db: Session,
    ticket_id: int,
    user: User,
    notes: Optional[str] = None,
    resource_id: Optional[int] = None
) -> Ticket:
    """
    Approves a P1/P2 ticket.
    Supports either manual technician assignment (if resource_id provided) or AI auto-dispatch.
    """
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise ValueError("Ticket not found")

    if ticket.approval_status == "APPROVED" and ticket.assigned_resource_id:
        return ticket  # Already approved & assigned

    ticket.approval_status = "APPROVED"
    ticket.approved_by_id = user.id
    ticket.approved_at = datetime.utcnow()
    ticket.approval_notes = notes or f"Approved by {user.full_name} ({user.role})"

    assigned_res = None
    if resource_id:
        # Manual Technician Assignment option selected by approving authority
        assigned_res = db.query(Resource).filter(
            Resource.id == resource_id,
            Resource.market_id == ticket.market_id
        ).first()
        if not assigned_res:
            raise ValueError(f"Selected technician (ID: {resource_id}) was not found in market '{ticket.market_id}'")
        
        ticket.assigned_resource_id = assigned_res.id
        ticket.assigned_at = datetime.utcnow()
        ticket.status = "Assigned"
        assigned_res.active_tickets_count += 1
        ticket.ai_triage_action = f"Approved by {user.full_name} & manually assigned to {assigned_res.name} ({assigned_res.region})"
    else:
        # AI Auto-Dispatch to regional resource
        if ticket.source == "INTERNAL":
            assigned_res = find_best_internal_resource(db, ticket.market_id)
        else:
            assigned_res = find_best_regional_resource(db, ticket.market_id, ticket.region)

        if assigned_res:
            ticket.assigned_resource_id = assigned_res.id
            ticket.assigned_at = datetime.utcnow()
            ticket.status = "Assigned"
            assigned_res.active_tickets_count += 1
            ticket.ai_triage_action = f"Approved by {user.full_name} & auto-dispatched to {assigned_res.name} ({assigned_res.region})"
        else:
            ticket.status = "Approved"
            ticket.ai_triage_action = f"Approved by {user.full_name} — waiting for resource availability"

    # Update matching Recommendation in Governance
    recs = db.query(Recommendation).filter(
        Recommendation.target_entity_type == "Ticket",
        Recommendation.target_entity_id == ticket.id,
        Recommendation.status == "PENDING"
    ).all()
    for r in recs:
        r.status = "APPROVED"
        r.reviewed_by_id = user.id
        r.reviewed_at = datetime.utcnow()
        r.review_notes = notes or f"Approved via Auto-Ticketing by {user.full_name} ({user.role})"

    # Audit Log
    dispatch_type = f"Manually Assigned to {assigned_res.name}" if resource_id else (f"Auto-Dispatched to {assigned_res.name}" if assigned_res else "Awaiting Resource")
    audit = AuditLog(
        market_id=ticket.market_id,
        source_module="Automatic Ticketing & Regional Dispatch",
        action_taken=f"Approved & {dispatch_type} for Ticket {ticket.ticket_code} ({ticket.priority})",
        decision="APPROVED",
        user_id=user.id,
        user_name=user.full_name,
        user_role=user.role,
        confidence_score=0.98,
        original_signals={"ticket_code": ticket.ticket_code, "priority": ticket.priority, "region": ticket.region, "manual_override": bool(resource_id)},
        execution_result={
            "ticket_code": ticket.ticket_code,
            "assigned_resource": assigned_res.name if assigned_res else None,
            "resource_region": assigned_res.region if assigned_res else None,
            "manual_assignment": bool(resource_id),
            "approval_notes": ticket.approval_notes,
            "status": ticket.status
        },
        timestamp=datetime.utcnow()
    )
    db.add(audit)
    db.commit()
    db.refresh(ticket)
    return ticket


def reject_ticket(
    db: Session,
    ticket_id: int,
    user: User,
    notes: Optional[str] = None
) -> Ticket:
    """Rejects ticket approval."""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise ValueError("Ticket not found")

    ticket.approval_status = "REJECTED"
    ticket.status = "Rejected"
    ticket.approved_by_id = user.id
    ticket.approved_at = datetime.utcnow()
    ticket.approval_notes = notes or f"Rejected by {user.full_name} ({user.role})"

    # Update matching Recommendation in Governance
    recs = db.query(Recommendation).filter(
        Recommendation.target_entity_type == "Ticket",
        Recommendation.target_entity_id == ticket.id,
        Recommendation.status == "PENDING"
    ).all()
    for r in recs:
        r.status = "REJECTED"
        r.reviewed_by_id = user.id
        r.reviewed_at = datetime.utcnow()
        r.review_notes = notes or f"Rejected via Auto-Ticketing by {user.full_name} ({user.role})"

    # Audit Log
    audit = AuditLog(
        market_id=ticket.market_id,
        source_module="Automatic Ticketing & Regional Dispatch",
        action_taken=f"Rejected Ticket Approval for {ticket.ticket_code} ({ticket.priority})",
        decision="REJECTED",
        user_id=user.id,
        user_name=user.full_name,
        user_role=user.role,
        confidence_score=0.98,
        original_signals={"ticket_code": ticket.ticket_code, "priority": ticket.priority, "region": ticket.region},
        execution_result={"status": "Rejected", "notes": notes},
        timestamp=datetime.utcnow()
    )
    db.add(audit)
    db.commit()
    db.refresh(ticket)
    return ticket


def resolve_ticket(
    db: Session,
    ticket_id: int,
    user: User,
    notes: Optional[str] = None
) -> Ticket:
    """Marks a ticket as resolved and decrements active ticket load for the assigned resource."""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise ValueError("Ticket not found")

    if ticket.status == "Resolved":
        return ticket

    ticket.status = "Resolved"
    ticket.resolved_at = datetime.utcnow()

    # Decrement resource load and restore Available status if capacity freed
    if ticket.assigned_resource_id:
        res = db.query(Resource).filter(Resource.id == ticket.assigned_resource_id).first()
        if res and res.active_tickets_count > 0:
            res.active_tickets_count -= 1
            if res.active_tickets_count < res.max_capacity and res.status == 'Busy':
                res.status = 'Available'

    db.commit()
    db.refresh(ticket)
    return ticket


def build_ticket_detail_response(ticket: Ticket) -> TicketDetailResponse:
    """Helper to convert Ticket ORM to TicketDetailResponse with populated relations."""
    res_name = ticket.assigned_resource.name if ticket.assigned_resource else None
    res_region = ticket.assigned_resource.region if ticket.assigned_resource else None
    res_type = ticket.assigned_resource.resource_type if ticket.assigned_resource else None
    appr_name = ticket.approved_by.full_name if ticket.approved_by else None
    cust_name = ticket.customer.name if ticket.customer else None
    node_code = ticket.node.node_code if ticket.node else None

    return TicketDetailResponse(
        id=ticket.id,
        market_id=ticket.market_id,
        ticket_code=ticket.ticket_code,
        source=ticket.source or "CUSTOMER",
        region=ticket.region,
        customer_id=ticket.customer_id,
        customer_name=cust_name,
        node_id=ticket.node_id,
        node_code=node_code,
        category=ticket.category,
        priority=ticket.priority,
        status=ticket.status,
        created_at=ticket.created_at,
        resolved_at=ticket.resolved_at,
        repeat_flag=ticket.repeat_flag,
        description=ticket.description,
        ai_triage_action=ticket.ai_triage_action,
        sla_deadline=ticket.sla_deadline,
        assigned_resource_id=ticket.assigned_resource_id,
        assigned_resource_name=res_name,
        assigned_resource_region=res_region,
        assigned_resource_type=res_type,
        assigned_at=ticket.assigned_at,
        approval_status=ticket.approval_status or "NOT_REQUIRED",
        approved_by_id=ticket.approved_by_id,
        approved_by_name=appr_name,
        approved_at=ticket.approved_at,
        approval_notes=ticket.approval_notes,
        voice_call_dispatched=bool(ticket.call_logs),
        last_call_recipient=f"{ticket.call_logs[-1].recipient_name} ({ticket.call_logs[-1].recipient_role})" if ticket.call_logs else None,
        last_call_script=ticket.call_logs[-1].voice_script if ticket.call_logs else None,
        last_call_status=ticket.call_logs[-1].status if ticket.call_logs else None
    )


def get_ticketing_stats(db: Session, market_id: str) -> TicketingStatsResponse:
    """Calculate aggregate stats for ticketing cockpit using lightweight SQL scalar aggregation."""
    from sqlalchemy import case

    stats_row = db.query(
        func.count(Ticket.id),
        func.count(case((Ticket.priority.in_(["P3", "P4", "Medium", "Low"]) & (Ticket.source != "INTERNAL") & (Ticket.assigned_resource_id.isnot(None)), 1))),
        func.count(case((Ticket.approval_status == "PENDING_APPROVAL", 1))),
        func.count(case((Ticket.source == "INTERNAL", 1))),
        func.count(case((Ticket.status == "Resolved", 1)))
    ).filter(Ticket.market_id == market_id).first()

    total, p3_p4, pending_appr, internal, resolved = stats_row if stats_row else (0, 0, 0, 0, 0)

    res_row = db.query(
        func.count(Resource.id),
        func.count(case((Resource.status == "Available", 1)))
    ).filter(Resource.market_id == market_id).first()

    total_res, avail_res = res_row if res_row else (0, 0)

    return TicketingStatsResponse(
        total_tickets=total or 0,
        auto_assigned_p3_p4=p3_p4 or 0,
        pending_approval_p1_p2=pending_appr or 0,
        internal_auto_assigned=internal or 0,
        resolved_tickets=resolved or 0,
        total_resources=total_res or 0,
        available_resources=avail_res or 0
    )


def simulate_ai_predicted_p3_p4_ticket(
    db: Session,
    market_id: str = "mumbai",
    priority: str = "P3",
    category: Optional[str] = "Optical Telemetry",
    description: Optional[str] = None,
    region: Optional[str] = None
) -> Ticket:
    """
    Simulate an incoming AI-predicted incident / alert for POC demonstration.
    Demonstrates instant auto-dispatch when toggle is ON, or queuing when toggle is OFF.
    """
    p_norm = normalize_priority(priority)
    if p_norm not in ["P3", "P4"]:
        p_norm = "P3"

    chosen_region = region
    node_id = None
    if not chosen_region:
        node = db.query(Node).filter(Node.market_id == market_id).first()
        if node:
            chosen_region = node.area
            node_id = node.id
        else:
            chosen_region = "Bandra West" if market_id == "mumbai" else "Park Street"
    else:
        node = db.query(Node).filter(Node.market_id == market_id, Node.area.ilike(f"%{chosen_region}%")).first()
        if node:
            node_id = node.id

    customer = db.query(Customer).filter(
        Customer.market_id == market_id,
        Customer.locality.ilike(f"%{chosen_region}%")
    ).first()
    customer_id = customer.id if customer else None

    if not description:
        if category == "Optical Telemetry":
            desc = f"AI Optical Telemetry Alert: Attenuation spike (-25.8 dBm) flagged on regional line in {chosen_region}. Preventive calibration required."
        elif category == "Speed":
            desc = f"AI Traffic Saturation Alert: PON buffer latency elevation (84.5% load) in {chosen_region}. Proactive line dispatch recommended."
        else:
            desc = f"AI Proactive Prediction ({p_norm}): Anomaly threshold exceeded in {chosen_region}. Automated dispatch recommended."
    else:
        desc = description

    req = TicketCreateRequest(
        source="CUSTOMER",
        category=category or "Optical Telemetry",
        priority=p_norm,
        description=desc,
        region=chosen_region,
        customer_id=customer_id,
        node_id=node_id
    )
    return create_and_dispatch_ticket(db, req, market_id=market_id)


def reset_p3_p4_demo_state(db: Session, market_id: str = "mumbai", unassign_count: int = 6) -> Dict[str, Any]:
    """
    Resets the POC demo state for client demonstrations:
    1. Turns OFF the auto-dispatch toggle for this market.
    2. Unassigns a batch of P3/P4 tickets (setting status='Open', assigned_resource_id=None).
    3. Decrements the active ticket count on the freed resources.
    4. Returns updated toggle status and count.
    """
    AUTO_DISPATCH_P3_P4_ENABLED[market_id] = False

    # Find active assigned P3/P4 tickets to unassign
    assigned_p34 = db.query(Ticket).filter(
        Ticket.market_id == market_id,
        Ticket.priority.in_(["P3", "P4"]),
        Ticket.status == "Assigned",
        Ticket.assigned_resource_id.isnot(None)
    ).order_by(Ticket.id.desc()).limit(unassign_count).all()

    for t in assigned_p34:
        if t.assigned_resource_id:
            res = db.query(Resource).filter(Resource.id == t.assigned_resource_id).first()
            if res and res.active_tickets_count > 0:
                res.active_tickets_count -= 1
                if res.status == 'Busy' and res.active_tickets_count < res.max_capacity:
                    res.status = 'Available'
        t.assigned_resource_id = None
        t.assigned_at = None
        t.status = "Open"
        t.approval_status = "NOT_REQUIRED"
        t.ai_triage_action = f"Pending Dispatch ({t.priority} Auto-Dispatch Paused)"

    db.commit()

    unassigned_count = db.query(Ticket).filter(
        Ticket.market_id == market_id,
        Ticket.priority.in_(["P3", "P4"]),
        Ticket.assigned_resource_id.is_(None),
        Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
    ).count()

    return {
        "enabled": False,
        "unassigned_p3_p4_count": unassigned_count,
        "market": market_id,
        "dispatched_count": 0,
        "dispatched_ticket_codes": []
    }
