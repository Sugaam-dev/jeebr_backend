from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session, selectinload, joinedload
from app.database import get_db
from app.models import Ticket, Resource, User, Customer
from app.schemas import (
    TicketCreateRequest, TicketDetailResponse, TicketApproveRequest,
    TicketRejectRequest, TicketResolveRequest, ResourceCreate, ResourceResponse,
    TicketingStatsResponse, ResourceTimelineResponse, ResourceTimelineItem,
    ApprovalCallLogResponse, AutoDispatchToggleRequest, AutoDispatchStatusResponse,
    SimulateAiAlertRequest
)
from app.auth import get_current_user, require_roles, require_not_viewer
from app.markets import get_current_market
from app.services.event_publisher import event_publisher
from app.services.workload_service import emit_workload_update_event, get_active_jobs_count
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
    """List tickets for current market with optional filters and resource scope enforcement."""
    query = db.query(Ticket).options(
        selectinload(Ticket.assigned_resource),
        selectinload(Ticket.customer),
        selectinload(Ticket.node),
        selectinload(Ticket.approved_by),
        selectinload(Ticket.call_logs)
    ).filter(Ticket.market_id == market)

    # Resource Scope Enforcement (Section 12)
    if current_user.role == "Customer":
        # Strict subscriber isolation: customer only sees their own registered tickets
        cust_records = db.query(Customer.id).filter(
            func.lower(Customer.email) == current_user.email.lower().strip()
        ).all()
        cust_ids = [c[0] for c in cust_records]
        if cust_ids:
            query = query.filter(Ticket.customer_id.in_(cust_ids))
        else:
            return []
    elif current_user.role == "Field Engineer":
        # Strict field engineer isolation: engineer only sees jobs assigned to them
        eng = db.query(Resource).filter(
            func.lower(Resource.email) == current_user.email.lower().strip()
        ).first()
        if eng:
            query = query.filter(Ticket.assigned_resource_id == eng.id)
        else:
            return []

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
        (Ticket.status.in_(['Open', 'Assigned', 'In-Progress', 'In Progress']), 1),
        else_=2
    )
    tickets = query.order_by(order_expr, Ticket.created_at.desc()).limit(limit).all()
    return [build_ticket_detail_response(t) for t in tickets]


@router.post("", response_model=TicketDetailResponse)
async def raise_ticket(
    req: TicketCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_not_viewer),
    market: str = Depends(get_current_market)
):
    """
    Raise a new ticket.
    Triggers automated dispatch and broadcasts SSE event across channels.
    """
    try:
        ticket = create_and_dispatch_ticket(db, req, market_id=market, user=current_user)
        # Broadcast real-time SSE creation event (Issue #2 & Section 17)
        create_event = {
            "event": "ticket_created",
            "ticket_id": ticket.id,
            "ticket_code": ticket.ticket_code,
            "priority": ticket.priority,
            "status": ticket.status,
            "region": ticket.region,
            "timestamp": datetime.utcnow().isoformat()
        }
        await event_publisher.publish(f"market:{market.lower()}", create_event)
        await event_publisher.publish(f"ticket:{ticket.id}", create_event)
        return build_ticket_detail_response(ticket)
    except HTTPException:
        raise
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
    # Self-healing: Ensure any active user with 'Field Engineer' role has a Resource entity in this market
    fe_users = db.query(User).filter(
        User.role == "Field Engineer",
        User.is_active == True
    ).all()
    if fe_users:
        existing_res_records = db.query(Resource.user_id, func.lower(Resource.email)).filter(
            Resource.market_id == market
        ).all()
        existing_uids = {r[0] for r in existing_res_records if r[0] is not None}
        existing_emails = {r[1] for r in existing_res_records if r[1] is not None}

        default_region = "Bandra West" if market == "mumbai" else "Salt Lake Sector V"
        default_coords = (19.0760, 72.8777) if market == "mumbai" else (22.5726, 88.3639)
        new_res_added = False

        for u in fe_users:
            if u.id not in existing_uids and u.email.lower() not in existing_emails:
                res = Resource(
                    market_id=market,
                    name=u.full_name or u.email.split("@")[0],
                    email=u.email.lower(),
                    phone=getattr(u, "phone", None) or "+91 98200 12345",
                    resource_type="FIELD",
                    region=default_region,
                    status="Available",
                    active_tickets_count=0,
                    max_capacity=5,
                    user_id=u.id,
                    current_latitude=default_coords[0],
                    current_longitude=default_coords[1],
                    last_ping_at=datetime.utcnow(),
                    location_status="ACTIVE"
                )
                db.add(res)
                new_res_added = True

        if new_res_added:
            db.commit()

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
    """Get single ticket details with resource-scope enforcement (Section 12)."""
    ticket = db.query(Ticket).options(
        selectinload(Ticket.assigned_resource),
        selectinload(Ticket.customer),
        selectinload(Ticket.node),
        selectinload(Ticket.approved_by),
        selectinload(Ticket.call_logs)
    ).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Resource-Scope Authorization
    if current_user.role == "Customer":
        cust_records = db.query(Customer.id).filter(
            func.lower(Customer.email) == current_user.email.lower().strip()
        ).all()
        cust_ids = [c[0] for c in cust_records]
        if not ticket.customer_id or ticket.customer_id not in cust_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You can only view your own tickets."
            )
    elif current_user.role == "Field Engineer":
        eng = db.query(Resource).filter(
            func.lower(Resource.email) == current_user.email.lower().strip()
        ).first()
        if not eng or ticket.assigned_resource_id != eng.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You can only view your own assigned jobs."
            )

    return build_ticket_detail_response(ticket)


@router.post("/{ticket_id}/approve", response_model=TicketDetailResponse)
async def approve_ticket(
    ticket_id: int,
    req: TicketApproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"]))
):
    """
    Approve a P1/P2 ticket.
    Supports manual technician override or automated regional field dispatch.
    Strictly enforces capacity limits (409 Conflict if at capacity) and emits SSE events.
    """
    try:
        updated = approve_and_assign_ticket(
            db,
            ticket_id,
            current_user,
            notes=req.notes,
            resource_id=req.resource_id
        )

        # Broadcast SSE event across channels (Section 17)
        appr_event = {
            "event": "ticket_approved",
            "ticket_id": updated.id,
            "ticket_code": updated.ticket_code,
            "status": updated.status,
            "assigned_resource_id": updated.assigned_resource_id,
            "assigned_resource_name": updated.assigned_resource.name if updated.assigned_resource else None,
            "region": updated.region,
            "timestamp": datetime.utcnow().isoformat()
        }
        await event_publisher.publish(f"ticket:{updated.id}", appr_event)
        await event_publisher.publish(f"market:{updated.market_id.lower()}", appr_event)

        if updated.assigned_resource_id:
            res = updated.assigned_resource
            if res:
                await emit_workload_update_event(updated.market_id, res.id, res.active_tickets_count, res.max_capacity)

        return build_ticket_detail_response(updated)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{ticket_id}/reject", response_model=TicketDetailResponse)
async def reject_ticket_endpoint(
    ticket_id: int,
    req: TicketRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"]))
):
    """Reject approval for a ticket."""
    try:
        updated = reject_ticket(db, ticket_id, current_user, notes=req.notes)
        rej_event = {
            "event": "ticket_rejected",
            "ticket_id": updated.id,
            "ticket_code": updated.ticket_code,
            "status": updated.status,
            "timestamp": datetime.utcnow().isoformat()
        }
        await event_publisher.publish(f"ticket:{updated.id}", rej_event)
        await event_publisher.publish(f"market:{updated.market_id.lower()}", rej_event)
        return build_ticket_detail_response(updated)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{ticket_id}/resolve", response_model=TicketDetailResponse)
async def resolve_ticket_endpoint(
    ticket_id: int,
    req: TicketResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Care", "Admin"]))
):
    """Mark ticket as resolved and release resource workload capacity."""
    try:
        res_id = None
        ticket_before = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket_before:
            res_id = ticket_before.assigned_resource_id

        updated = resolve_ticket(db, ticket_id, current_user, notes=req.notes)
        res_event = {
            "event": "ticket_resolved",
            "ticket_id": updated.id,
            "ticket_code": updated.ticket_code,
            "status": updated.status,
            "timestamp": datetime.utcnow().isoformat()
        }
        await event_publisher.publish(f"ticket:{updated.id}", res_event)
        await event_publisher.publish(f"market:{updated.market_id.lower()}", res_event)

        if res_id:
            res = db.query(Resource).filter(Resource.id == res_id).first()
            if res:
                await emit_workload_update_event(updated.market_id, res.id, res.active_tickets_count, res.max_capacity)

        return build_ticket_detail_response(updated)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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
