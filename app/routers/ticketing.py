from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session, selectinload, joinedload
from app.database import get_db
from app.models import Ticket, Resource, User
from app.schemas import (
    TicketCreateRequest, TicketDetailResponse, TicketApproveRequest,
    TicketRejectRequest, TicketResolveRequest, ResourceCreate, ResourceResponse,
    TicketingStatsResponse, ResourceTimelineResponse, ResourceTimelineItem,
    ApprovalCallLogResponse, AutoDispatchToggleRequest, AutoDispatchStatusResponse,
    SimulateAiAlertRequest
)
from app.auth import get_current_user, require_roles, require_not_viewer
from app.markets import get_current_market
from app.services.ticketing_engine import (
    create_and_dispatch_ticket,
    approve_and_assign_ticket,
    reject_ticket,
    resolve_ticket,
    build_ticket_detail_response,
    get_ticketing_stats,
    get_auto_dispatch_p3_p4_status,
    set_auto_dispatch_p3_p4,
    auto_dispatch_unassigned_p3_p4_tickets,
    simulate_ai_predicted_p3_p4_ticket,
    reset_p3_p4_demo_state
)
from app.services.voice_alert_service import (
    get_ticket_call_logs,
    get_recent_call_logs,
    dispatch_voice_alert_for_ticket
)
from datetime import datetime

router = APIRouter(prefix="/tickets", tags=["Automatic Ticketing & Regional Dispatch"])


@router.get("", response_model=List[TicketDetailResponse])
def get_tickets(
    source: Optional[str] = None,
    priority: Optional[str] = None,
    approval_status: Optional[str] = None,
    status: Optional[str] = None,
    region: Optional[str] = None,
    resource_id: Optional[int] = None,
    limit: int = Query(250, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List tickets for current market with optional filters."""
    query = db.query(Ticket).options(
        selectinload(Ticket.assigned_resource),
        selectinload(Ticket.customer),
        selectinload(Ticket.node),
        selectinload(Ticket.approved_by),
        selectinload(Ticket.call_logs)
    ).filter(Ticket.market_id == market)

    if resource_id:
        query = query.filter(Ticket.assigned_resource_id == resource_id)
    if source:
        query = query.filter(Ticket.source == source.upper())
    if priority:
        query = query.filter(Ticket.priority == priority.upper())
    if approval_status:
        query = query.filter(Ticket.approval_status == approval_status.upper())
    if status:
        query = query.filter(Ticket.status == status)
    if region:
        query = query.filter(Ticket.region.ilike(f"%{region}%"))

    order_expr = case(
        (Ticket.approval_status == 'PENDING_APPROVAL', 0),
        (Ticket.status.in_(['Open', 'Assigned', 'In-Progress']), 1),
        else_=2
    )
    tickets = query.order_by(order_expr, Ticket.created_at.desc()).limit(limit).all()
    return [build_ticket_detail_response(t) for t in tickets]


@router.post("", response_model=TicketDetailResponse)
def raise_ticket(
    req: TicketCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_not_viewer),
    market: str = Depends(get_current_market)
):
    """
    Raise a new ticket.
    Triggers automated dispatch:
    - Internal tickets auto-assigned to internal team.
    - P3/P4 tickets auto-assigned to matching regional resource without approval.
    - P1/P2 tickets placed in PENDING_APPROVAL.
    """
    try:
        ticket = create_and_dispatch_ticket(db, req, market_id=market, user=current_user)
        return build_ticket_detail_response(ticket)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stats", response_model=TicketingStatsResponse)
def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Get aggregate ticketing & resource workload statistics."""
    return get_ticketing_stats(db, market_id=market)


@router.get("/auto-dispatch-settings", response_model=AutoDispatchStatusResponse)
def get_auto_dispatch_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Get current toggle status for P3/P4 Autonomous Auto-Dispatch in active market."""
    return get_auto_dispatch_p3_p4_status(db, market_id=market)


@router.post("/auto-dispatch-settings", response_model=AutoDispatchStatusResponse)
def update_auto_dispatch_settings(
    req: AutoDispatchToggleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """
    Toggle P3/P4 Autonomous Auto-Dispatch mode for active market.
    When set to True: Automatically assigns all unassigned P3 & P4 tickets to regional resources immediately!
    """
    return set_auto_dispatch_p3_p4(db, market_id=market, enabled=req.enabled)


@router.post("/auto-dispatch-now", response_model=AutoDispatchStatusResponse)
def force_auto_dispatch_unassigned(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """Directly trigger batch auto-dispatch for all pending P3/P4 tickets in current market."""
    dispatched = auto_dispatch_unassigned_p3_p4_tickets(db, market_id=market)
    status = get_auto_dispatch_p3_p4_status(db, market_id=market)
    status["dispatched_count"] = len(dispatched)
    status["dispatched_ticket_codes"] = [t.ticket_code for t in dispatched]
    return status


@router.post("/simulate-ai-alert", response_model=TicketDetailResponse)
def trigger_ai_predicted_alert(
    req: SimulateAiAlertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """
    Simulate an incoming AI-predicted P3/P4 incident or telemetry alert.
    If autonomous mode is ON, it will be automatically dispatched immediately!
    If autonomous mode is OFF, it will wait as 'Pending Dispatch' to demonstrate the toggle effect.
    """
    try:
        ticket = simulate_ai_predicted_p3_p4_ticket(
            db=db,
            market_id=market,
            priority=req.priority or "P3",
            category=req.category or "Optical Telemetry",
            description=req.description,
            region=req.region
        )
        return build_ticket_detail_response(ticket)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reset-demo-state", response_model=AutoDispatchStatusResponse)
def reset_demo_state_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"])),
    market: str = Depends(get_current_market)
):
    """
    Reset demo state for client demonstrations:
    Turns OFF auto-dispatch, unassigns 6 P3/P4 tickets back to 'Pending Dispatch',
    so the client demonstration can be replayed repeatedly.
    """
    return reset_p3_p4_demo_state(db, market_id=market, unassign_count=6)


@router.get("/resources", response_model=List[ResourceResponse])
def get_resources(
    resource_type: Optional[str] = None,
    region: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """List team resources for the current market (Field Engineers & Internal NOC)."""
    query = db.query(Resource).filter(Resource.market_id == market)
    if resource_type:
        query = query.filter(Resource.resource_type == resource_type.upper())
    if region:
        query = query.filter(Resource.region.ilike(f"%{region}%"))

    # Compute live active ticket counts per resource to guarantee 100% data consistency
    active_counts = dict(
        db.query(
            Ticket.assigned_resource_id,
            func.count(Ticket.id)
        ).filter(
            Ticket.assigned_resource_id.isnot(None),
            Ticket.status.notin_(['Resolved', 'Closed', 'Rejected'])
        ).group_by(Ticket.assigned_resource_id).all()
    )

    resources = query.order_by(Resource.region.asc(), Resource.name.asc()).all()
    needs_commit = False
    for r in resources:
        real_count = active_counts.get(r.id, 0)
        if r.active_tickets_count != real_count:
            r.active_tickets_count = real_count
            if real_count >= r.max_capacity:
                r.status = 'Busy'
            elif r.status == 'Busy' and real_count < r.max_capacity:
                r.status = 'Available'
            needs_commit = True

    if needs_commit:
        db.commit()

    return resources


@router.post("/resources", response_model=ResourceResponse)
def create_resource(
    req: ResourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """Create a new resource (field engineer or internal staff)."""
    res = Resource(
        market_id=req.market_id,
        name=req.name,
        email=req.email,
        phone=req.phone,
        resource_type=req.resource_type.upper(),
        region=req.region,
        status=req.status,
        max_capacity=req.max_capacity
    )
    db.add(res)
    db.commit()
    db.refresh(res)
    return res


@router.get("/resources/{resource_id}/timeline", response_model=ResourceTimelineResponse)
def get_resource_timeline(
    resource_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed ticket workload timeline for a specific resource:
    Shows all assigned tickets sorted chronologically by SLA deadline,
    time remaining to resolve, urgency level, and past resolution history.
    """
    res = db.query(Resource).filter(Resource.id == resource_id).first()
    if not res:
        raise HTTPException(status_code=404, detail="Resource not found")

    # Fetch active tickets assigned to this resource (ordered by SLA deadline)
    active_tickets = db.query(Ticket).options(
        joinedload(Ticket.customer)
    ).filter(
        Ticket.assigned_resource_id == resource_id,
        Ticket.status.notin_(['Resolved', 'Closed', 'Rejected'])
    ).order_by(Ticket.sla_deadline.asc().nulls_last()).all()

    # Self-heal resource active_tickets_count if out of sync
    if res.active_tickets_count != len(active_tickets):
        res.active_tickets_count = len(active_tickets)
        if res.active_tickets_count >= res.max_capacity:
            res.status = 'Busy'
        elif res.status == 'Busy' and res.active_tickets_count < res.max_capacity:
            res.status = 'Available'
        db.commit()
        db.refresh(res)

    # Fetch resolved tickets (ordered by resolved_at DESC nulls_last, then id DESC)
    resolved_tickets = db.query(Ticket).options(
        joinedload(Ticket.customer)
    ).filter(
        Ticket.assigned_resource_id == resource_id,
        Ticket.status == 'Resolved'
    ).order_by(Ticket.resolved_at.desc().nulls_last(), Ticket.id.desc()).limit(50).all()

    now = datetime.utcnow()
    active_items = []
    urgent_count = 0

    for t in active_tickets:
        mins_left = None
        urgency = "Nominal"
        if t.sla_deadline:
            diff_secs = (t.sla_deadline - now).total_seconds()
            mins_left = int(diff_secs / 60)
            if mins_left < 0:
                urgency = "Overdue"
                urgent_count += 1
            elif mins_left < 120:  # less than 2 hours
                urgency = "Critical"
                urgent_count += 1
            elif mins_left < 360:  # less than 6 hours
                urgency = "Warning"

        cust_name = t.customer.name if t.customer else ("Internal Core NOC" if t.source == "INTERNAL" else "Subscriber")
        locality = t.customer.locality if t.customer else (t.region or "Regional Central")

        active_items.append(ResourceTimelineItem(
            ticket_id=t.id,
            ticket_code=t.ticket_code,
            category=t.category,
            priority=t.priority,
            status=t.status,
            customer_name=cust_name,
            locality=locality,
            description=t.description,
            assigned_at=t.assigned_at,
            resolved_at=t.resolved_at,
            sla_deadline=t.sla_deadline,
            minutes_to_sla=mins_left,
            urgency_level=urgency
        ))

    resolved_items = []
    for t in resolved_tickets:
        cust_name = t.customer.name if t.customer else ("Internal Core NOC" if t.source == "INTERNAL" else "Subscriber")
        locality = t.customer.locality if t.customer else (t.region or "Regional Central")
        resolved_items.append(ResourceTimelineItem(
            ticket_id=t.id,
            ticket_code=t.ticket_code,
            category=t.category,
            priority=t.priority,
            status=t.status,
            customer_name=cust_name,
            locality=locality,
            description=t.description,
            assigned_at=t.assigned_at,
            resolved_at=t.resolved_at,
            sla_deadline=t.sla_deadline,
            minutes_to_sla=None,
            urgency_level="Nominal"
        ))

    next_deadline = active_tickets[0].sla_deadline if (active_tickets and active_tickets[0].sla_deadline) else None

    return ResourceTimelineResponse(
        resource=res,
        active_tickets=active_items,
        resolved_tickets=resolved_items,
        total_active=len(active_items),
        total_resolved=len(resolved_items),
        urgent_count=urgent_count,
        next_sla_deadline=next_deadline
    )


@router.get("/{ticket_id}", response_model=TicketDetailResponse)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get single ticket details."""
    ticket = db.query(Ticket).options(
        selectinload(Ticket.assigned_resource),
        selectinload(Ticket.customer),
        selectinload(Ticket.node),
        selectinload(Ticket.approved_by),
        selectinload(Ticket.call_logs)
    ).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return build_ticket_detail_response(ticket)


@router.post("/{ticket_id}/approve", response_model=TicketDetailResponse)
def approve_ticket(
    ticket_id: int,
    req: TicketApproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"]))
):
    """
    Approve a P1/P2 ticket.
    Supports manual technician override or automated regional field dispatch.
    """
    try:
        updated = approve_and_assign_ticket(
            db,
            ticket_id,
            current_user,
            notes=req.notes,
            resource_id=req.resource_id
        )
        return build_ticket_detail_response(updated)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{ticket_id}/reject", response_model=TicketDetailResponse)
def reject_ticket_endpoint(
    ticket_id: int,
    req: TicketRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"]))
):
    """Reject approval for a ticket."""
    try:
        updated = reject_ticket(db, ticket_id, current_user, notes=req.notes)
        return build_ticket_detail_response(updated)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{ticket_id}/resolve", response_model=TicketDetailResponse)
def resolve_ticket_endpoint(
    ticket_id: int,
    req: TicketResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Care", "Admin"]))
):
    """Mark ticket as resolved and release resource workload capacity."""
    try:
        updated = resolve_ticket(db, ticket_id, current_user, notes=req.notes)
        return build_ticket_detail_response(updated)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{ticket_id}/call-logs", response_model=List[ApprovalCallLogResponse])
def get_ticket_call_history(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all automated voice alert calls dispatched for a specific ticket."""
    return get_ticket_call_logs(db, ticket_id)


@router.get("/calls/recent", response_model=List[ApprovalCallLogResponse])
def get_recent_calls(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Get recent automated emergency voice dispatch calls in current market."""
    return get_recent_call_logs(db, market)


@router.post("/{ticket_id}/simulate-call", response_model=ApprovalCallLogResponse)
def simulate_voice_call(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"]))
):
    """Re-trigger or simulate an automated voice alert call to the approving authority."""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    call_log = dispatch_voice_alert_for_ticket(db, ticket, override_approver=current_user)
    return call_log
