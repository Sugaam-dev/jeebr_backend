import asyncio
import json
from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload, joinedload
from app.config import settings
from app.database import get_db
from app.models import (
    User, Resource, Ticket, Customer, FieldAssignment, TrackingSession, LocationPing, FieldOtp
)
from app.schemas import (
    FieldAssignmentResponse, FieldAssignmentTransitionRequest,
    LocationPingCreate, LocationPingResponse,
    OtpRequestResponse, OtpVerifyRequest, OtpVerifyResponse,
    FieldOperationsSummaryResponse, EngineerWorkloadResponse,
    CustomerTrackingResponse, RouteResponse, RouteWaypoint, OtpDetail,
    ConfigureCapacityRequest, CompleteJobRequest
)
from app.auth import get_current_user, require_roles, require_not_viewer
from app.markets import get_current_market
from app.services.field_tracking_service import (
    transition_assignment_state,
    record_location_ping_async,
    execute_ticket_completion_transaction,
    get_field_operations_summary,
    calculate_eta_minutes
)
from app.services.otp_service import (
    generate_and_dispatch_customer_otp,
    verify_customer_submitted_otp,
    get_active_otp_for_customer
)
from app.services.event_publisher import event_publisher
from app.services.routing_service import routing_service
from app.services.rate_limiter import location_rate_limiter
from app.services.workload_service import calculate_engineer_workload, emit_workload_update_event

router = APIRouter(tags=["Field Operations & Live Tracking"])


def _resolve_engineer_resource(db: Session, user: User) -> Optional[Resource]:
    """
    Finds the Resource entity mapped to a field engineer User using authoritative relational identity (User.id -> Resource.user_id).
    """
    by_user = db.query(Resource).filter(Resource.user_id == user.id).first()
    if by_user:
        return by_user
    # Fallback to case-insensitive email matching and bind user_id permanently if unbound
    by_email = db.query(Resource).filter(func.lower(Resource.email) == user.email.lower().strip()).first()
    if by_email:
        if by_email.user_id is None:
            by_email.user_id = user.id
            db.commit()
        return by_email

    # Self-healing: If user is an active Field Engineer, auto-provision and link a Resource record
    if user.role == "Field Engineer":
        new_res = Resource(
            market_id="mumbai",
            name=user.full_name,
            email=user.email.lower().strip(),
            phone=getattr(user, "phone", None) or "+91 98200 12345",
            resource_type="FIELD",
            region="Bandra West",
            status="Available" if user.is_active else "Offline",
            active_tickets_count=0,
            max_capacity=5,
            user_id=user.id,
            current_latitude=19.0760,
            current_longitude=72.8777,
            last_ping_at=datetime.utcnow(),
            location_status="ACTIVE"
        )
        db.add(new_res)
        db.commit()
        db.refresh(new_res)
        return new_res

    return None


def _build_field_assignment_response(assignment: FieldAssignment) -> FieldAssignmentResponse:
    ticket = assignment.ticket
    engineer = assignment.engineer
    customer = ticket.customer if ticket else None

    eta = None
    if engineer and customer and customer.service_latitude and customer.service_longitude:
        eta = calculate_eta_minutes(
            engineer.current_latitude,
            engineer.current_longitude,
            customer.service_latitude,
            customer.service_longitude
        )

    return FieldAssignmentResponse(
        id=assignment.id,
        market_id=assignment.market_id,
        ticket_id=assignment.ticket_id,
        ticket_code=ticket.ticket_code if ticket else None,
        engineer_id=assignment.engineer_id,
        engineer_name=engineer.name if engineer else None,
        engineer_phone=engineer.phone if engineer else None,
        status=assignment.status,
        assigned_at=assignment.assigned_at,
        accepted_at=assignment.accepted_at,
        en_route_at=assignment.en_route_at,
        arrived_at=assignment.arrived_at,
        work_started_at=assignment.work_started_at,
        otp_requested_at=assignment.otp_requested_at,
        otp_verified_at=assignment.otp_verified_at,
        completed_at=assignment.completed_at,
        notes=assignment.notes,
        customer_id=customer.id if customer else None,
        customer_name=customer.name if customer else None,
        customer_phone=customer.phone if customer else None,
        customer_locality=customer.locality if customer else None,
        service_address=customer.service_address if customer else None,
        service_latitude=customer.service_latitude if customer else None,
        service_longitude=customer.service_longitude if customer else None,
        ticket_category=ticket.category if ticket else None,
        ticket_priority=ticket.priority if ticket else None,
        ticket_description=ticket.description if ticket else None,
        sla_deadline=ticket.sla_deadline if ticket else None,
        current_latitude=engineer.current_latitude if engineer else None,
        current_longitude=engineer.current_longitude if engineer else None,
        location_status=engineer.location_status if engineer else "UNAVAILABLE",
        eta_minutes=eta
    )


# --- 1. Operations Summary & Monitoring ---

@router.get("/summary", response_model=FieldOperationsSummaryResponse)
def get_ops_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    Get aggregated operational KPIs (En Route, On Site, Available, Active Jobs, SLA At Risk).
    Accessible to all authenticated roles including Viewer (aggregated only).
    """
    return get_field_operations_summary(db, market_id=market)


@router.get("/engineers", response_model=List[EngineerWorkloadResponse])
def get_field_engineers(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """
    List active field engineers, workload, current job, and latest coordinates for current market.
    Restricted to NOC and Admin; Viewer is forbidden from viewing individual employee GPS locations.
    """
    resources = db.query(Resource).filter(
        Resource.market_id == market,
        Resource.resource_type == "FIELD"
    ).all()

    # Active assignments for these resources
    active_assignments = {
        a.engineer_id: a for a in db.query(FieldAssignment).options(
            joinedload(FieldAssignment.ticket)
        ).filter(
            FieldAssignment.market_id == market,
            FieldAssignment.status.notin_(["COMPLETED", "CANCELLED", "REJECTED"])
        ).all()
    }

    results = []
    for r in resources:
        active_a = active_assignments.get(r.id)
        current_job_id = active_a.id if active_a else None
        current_job_ticket = active_a.ticket.ticket_code if active_a and active_a.ticket else None
        current_job_status = active_a.status if active_a else None

        eta = None
        if active_a and active_a.ticket and active_a.ticket.customer:
            cust = active_a.ticket.customer
            eta = calculate_eta_minutes(
                r.current_latitude, r.current_longitude,
                cust.service_latitude, cust.service_longitude
            )

        workload = calculate_engineer_workload(db, r)
        results.append(EngineerWorkloadResponse(
            id=r.id,
            engineer_id=r.id,
            name=r.name,
            email=r.email,
            phone=r.phone,
            market_id=r.market_id,
            region=r.region,
            status=r.status,
            location_status=r.location_status or "UNAVAILABLE",
            current_latitude=r.current_latitude,
            current_longitude=r.current_longitude,
            last_ping_at=r.last_ping_at,
            active_tickets_count=workload["active_jobs"],
            active_jobs=workload["active_jobs"],
            max_capacity=workload["max_active_jobs"],
            max_active_jobs=workload["max_active_jobs"],
            available_capacity=workload["available_capacity"],
            capacity_status=workload["capacity_status"],
            current_job_id=current_job_id,
            current_job_ticket=current_job_ticket,
            current_job_status=current_job_status,
            current_job_eta_minutes=eta
        ))

    return results


@router.get("/engineers/{engineer_id}/workload", response_model=EngineerWorkloadResponse)
def get_engineer_workload_endpoint(
    engineer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get backend-calculated workload object adhering to Section 22.
    Restricted to NOC, Admin, Super Admin, or Field Engineer viewing own workload.
    """
    r = db.query(Resource).filter(Resource.id == engineer_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Engineer resource not found")

    from app.services.rbac_service import is_admin_or_super
    if not is_admin_or_super(current_user.role) and current_user.role != "NOC":
        if current_user.role == "Field Engineer":
            if not r.email or r.email.lower().strip() != current_user.email.lower().strip():
                raise HTTPException(status_code=403, detail="Forbidden: You can only view your own workload.")
        else:
            raise HTTPException(status_code=403, detail="Forbidden: Insufficient privileges to view engineer workload.")

    workload = calculate_engineer_workload(db, r)
    return EngineerWorkloadResponse(
        id=r.id,
        engineer_id=r.id,
        name=r.name,
        email=r.email,
        phone=r.phone,
        market_id=r.market_id,
        region=r.region,
        status=r.status,
        location_status=r.location_status or "UNAVAILABLE",
        current_latitude=r.current_latitude,
        current_longitude=r.current_longitude,
        last_ping_at=r.last_ping_at,
        active_tickets_count=workload["active_jobs"],
        active_jobs=workload["active_jobs"],
        max_capacity=workload["max_active_jobs"],
        max_active_jobs=workload["max_active_jobs"],
        available_capacity=workload["available_capacity"],
        capacity_status=workload["capacity_status"]
    )


@router.put("/engineers/{engineer_id}/capacity", response_model=EngineerWorkloadResponse)
async def configure_engineer_capacity(
    engineer_id: int,
    req: ConfigureCapacityRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    Configure maximum active job capacity for an engineer (Section 25).
    Authorized for Super Admin and Admin.
    Emits real-time SSE workload event.
    """
    r = db.query(Resource).filter(Resource.id == engineer_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Engineer resource not found")

    r.max_capacity = req.max_capacity
    db.commit()
    db.refresh(r)

    workload = calculate_engineer_workload(db, r)
    await emit_workload_update_event(r.market_id, r.id, workload["active_jobs"], r.max_capacity)

    return EngineerWorkloadResponse(
        id=r.id,
        engineer_id=r.id,
        name=r.name,
        email=r.email,
        phone=r.phone,
        market_id=r.market_id,
        region=r.region,
        status=r.status,
        location_status=r.location_status or "UNAVAILABLE",
        current_latitude=r.current_latitude,
        current_longitude=r.current_longitude,
        last_ping_at=r.last_ping_at,
        active_tickets_count=workload["active_jobs"],
        active_jobs=workload["active_jobs"],
        max_capacity=workload["max_active_jobs"],
        max_active_jobs=workload["max_active_jobs"],
        available_capacity=workload["available_capacity"],
        capacity_status=workload["capacity_status"]
    )



@router.get("/jobs", response_model=List[FieldAssignmentResponse])
def get_field_jobs(
    status: Optional[str] = None,
    engineer_id: Optional[int] = None,
    limit: int = Query(100, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Care", "Executive"])),
    market: str = Depends(get_current_market)
):
    """List field assignments for the current market with optional filters."""
    # Auto-synchronize unlinked assigned tickets in market
    unlinked_tickets = db.query(Ticket).filter(
        Ticket.market_id == market,
        Ticket.assigned_resource_id.isnot(None),
        Ticket.status.notin_(["Resolved", "Closed"])
    ).all()
    for t in unlinked_tickets:
        existing_fa = db.query(FieldAssignment).filter(FieldAssignment.ticket_id == t.id).first()
        if not existing_fa:
            new_fa = FieldAssignment(
                ticket_id=t.id,
                engineer_id=t.assigned_resource_id,
                market_id=t.market_id or market,
                status="ASSIGNED",
                created_at=t.created_at or datetime.utcnow(),
                notes=f"Auto-synchronized field dispatch for ticket {t.ticket_code}"
            )
            db.add(new_fa)
    db.commit()

    query = db.query(FieldAssignment).options(
        joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
        joinedload(FieldAssignment.engineer)
    ).filter(FieldAssignment.market_id == market)

    if status:
        query = query.filter(FieldAssignment.status == status.upper())
    if engineer_id:
        query = query.filter(FieldAssignment.engineer_id == engineer_id)

    assignments = query.order_by(FieldAssignment.created_at.desc()).limit(limit).all()
    return [_build_field_assignment_response(a) for a in assignments]


@router.get("/jobs/{assignment_id}", response_model=FieldAssignmentResponse)
def get_single_field_job(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """Retrieve single authoritative field workorder assignment by ID."""
    assignment = db.query(FieldAssignment).options(
        joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
        joinedload(FieldAssignment.engineer)
    ).filter(FieldAssignment.id == assignment_id).first()

    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    # Resource Scope Enforcement
    if current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or assignment.engineer_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: You can only view your own assigned jobs.")
    elif current_user.role == "Customer":
        if not assignment.ticket or not assignment.ticket.customer or assignment.ticket.customer.email.lower().strip() != current_user.email.lower().strip():
            raise HTTPException(status_code=403, detail="Forbidden: You can only view jobs for your own tickets.")

    return _build_field_assignment_response(assignment)


# --- 2. Field Engineer Workbench Endpoints ---

@router.get("/my-jobs", response_model=List[FieldAssignmentResponse])
def get_my_assigned_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Field Engineer", "NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """List jobs assigned strictly to the authenticated field engineer."""
    engineer = _resolve_engineer_resource(db, current_user)
    if not engineer:
        # If Admin or NOC without mapped Resource, return all active market jobs
        if current_user.role in ("Admin", "NOC"):
            jobs = db.query(FieldAssignment).options(
                joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
                joinedload(FieldAssignment.engineer)
            ).filter(
                FieldAssignment.market_id == market,
                FieldAssignment.status.notin_(["COMPLETED", "CANCELLED", "REJECTED"])
            ).all()
            return [_build_field_assignment_response(j) for j in jobs]
        return []

    # Auto-synchronize: ensure all tickets assigned to this engineer have active FieldAssignments
    active_assigned_tickets = db.query(Ticket).filter(
        Ticket.assigned_resource_id == engineer.id,
        Ticket.status.notin_(["Resolved", "Closed"])
    ).all()

    for t in active_assigned_tickets:
        existing_fa = db.query(FieldAssignment).filter(FieldAssignment.ticket_id == t.id).first()
        if not existing_fa:
            new_fa = FieldAssignment(
                ticket_id=t.id,
                engineer_id=engineer.id,
                market_id=t.market_id or market,
                status="ASSIGNED",
                created_at=t.created_at or datetime.utcnow(),
                notes=f"Auto-synchronized field dispatch for ticket {t.ticket_code}"
            )
            db.add(new_fa)
    db.commit()

    jobs = db.query(FieldAssignment).options(
        joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
        joinedload(FieldAssignment.engineer)
    ).filter(
        FieldAssignment.engineer_id == engineer.id,
        FieldAssignment.status.notin_(["COMPLETED", "CANCELLED", "REJECTED"])
    ).order_by(FieldAssignment.created_at.desc()).all()

    return [_build_field_assignment_response(j) for j in jobs]


@router.post("/jobs/{assignment_id}/transition", response_model=FieldAssignmentResponse)
async def update_job_status(
    assignment_id: int,
    req: FieldAssignmentTransitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Field Engineer", "NOC", "Admin"]))
):
    """
    Perform a state machine transition on a field assignment.
    Verifies that the caller owns the assignment (or is Admin/NOC).
    """
    assignment = db.query(FieldAssignment).options(
        joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
        joinedload(FieldAssignment.engineer)
    ).filter(FieldAssignment.id == assignment_id).first()

    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    # Engineer authorization check
    if current_user.role == "Field Engineer":
        engineer = _resolve_engineer_resource(db, current_user)
        if not engineer or assignment.engineer_id != engineer.id:
            raise HTTPException(status_code=403, detail="Forbidden: You can only update your own assigned jobs.")

    updated = transition_assignment_state(
        db=db,
        assignment=assignment,
        target_status=req.target_status,
        user=current_user,
        notes=req.notes
    )

    # Publish real-time status_transition event across channels
    trans_event = {
        "event": "status_transition",
        "assignment_id": updated.id,
        "ticket_id": updated.ticket_id,
        "status": updated.status,
        "timestamp": datetime.utcnow().isoformat()
    }
    active_session = db.query(TrackingSession).filter(
        TrackingSession.field_assignment_id == updated.id
    ).order_by(TrackingSession.created_at.desc()).first()
    if active_session:
        await event_publisher.publish(f"session:{active_session.id}", trans_event)
    await event_publisher.publish(f"ticket:{updated.ticket_id}", trans_event)
    await event_publisher.publish(f"market:{updated.market_id.lower()}", trans_event)

    return _build_field_assignment_response(updated)


@router.post("/jobs/{assignment_id}/location", response_model=LocationPingResponse)
@router.post("/jobs/{assignment_id}/ping", response_model=LocationPingResponse)
async def submit_location_ping(
    assignment_id: int,
    req: LocationPingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Field Engineer", "NOC", "Admin"]))
):
    """
    Receive and persist a live GPS location ping from the field engineer.
    Verifies coordinate ranges, active tracking session, and emits real-time event.
    """
    assignment = db.query(FieldAssignment).filter(FieldAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    engineer = assignment.engineer
    if not engineer:
        raise HTTPException(status_code=404, detail="Assigned engineer resource not found")

    if current_user.role == "Field Engineer":
        user_engineer = _resolve_engineer_resource(db, current_user)
        if not user_engineer or assignment.engineer_id != user_engineer.id:
            raise HTTPException(status_code=403, detail="Forbidden: Cannot send location for another engineer's job.")

    # Security & Integrity: Block simulated/mock pings in production
    if settings.ENVIRONMENT.lower() in ("production", "prod") and req.is_mock:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Simulated or mock location telemetry is strictly prohibited in production environments."
        )

    # Server-Side Rate Limiting (1 accepted operational ping per 3 seconds per engineer)
    throttled, retry_after = await location_rate_limiter.is_throttled(engineer_id=engineer.id)
    if throttled:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too Many Requests: Rate limit exceeded. Maximum 1 GPS ping per 3 seconds per engineer. Retry after {retry_after}s."
        )

    ping = await record_location_ping_async(
        db=db,
        assignment=assignment,
        engineer=engineer,
        ping_data=req.model_dump()
    )
    await location_rate_limiter.record_accepted_ping(engineer_id=engineer.id)
    return ping


# --- 3. Customer OTP Verification Endpoints ---

@router.post("/jobs/{assignment_id}/request-otp", response_model=OtpRequestResponse)
async def request_customer_otp(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Field Engineer", "NOC", "Admin"]))
):
    """
    Triggers generation and dispatch of customer verification OTP.
    Only allowed when the job has reached WORKING state.
    The response strictly NEVER contains the raw OTP.
    Publishes an SSE otp_requested event (metadata only, no raw OTP/hash).
    """
    assignment = db.query(FieldAssignment).options(
        joinedload(FieldAssignment.ticket).joinedload(Ticket.customer)
    ).filter(FieldAssignment.id == assignment_id).first()

    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    if current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or assignment.engineer_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: Cannot request OTP for another engineer's job.")

    ticket = assignment.ticket
    if not ticket or not ticket.customer:
        raise HTTPException(status_code=400, detail="Assignment has no associated customer record.")

    if assignment.status not in ("ARRIVED", "WORKING", "OTP_REQUESTED"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot request customer OTP while in state '{assignment.status}'. Job must be in 'ARRIVED' or 'WORKING' state."
        )

    if assignment.status == "ARRIVED" and not assignment.work_started_at:
        assignment.work_started_at = datetime.utcnow()

    otp_record, _ = generate_and_dispatch_customer_otp(db, assignment, ticket.customer)

    assignment.status = "OTP_REQUESTED"
    assignment.otp_requested_at = datetime.utcnow()
    if ticket.status in ("Resolved", "Closed", "Open"):
        ticket.status = "In Progress"
    db.commit()

    # Find active tracking session for event routing
    active_session = db.query(TrackingSession).filter(
        TrackingSession.field_assignment_id == assignment.id
    ).order_by(TrackingSession.created_at.desc()).first()

    # Publish real-time otp_requested event (Metadata ONLY: NO raw OTP, NO hash, NO salt)
    otp_event = {
        "event": "otp_requested",
        "assignment_id": assignment.id,
        "ticket_id": assignment.ticket_id,
        "session_id": active_session.id if active_session else None,
        "status": "OTP_REQUESTED",
        "otp_expires_at": otp_record.expires_at.isoformat()
    }
    if active_session:
        await event_publisher.publish(f"session:{active_session.id}", otp_event)
    await event_publisher.publish(f"ticket:{assignment.ticket_id}", otp_event)
    await event_publisher.publish(f"market:{assignment.market_id.lower()}", otp_event)

    return OtpRequestResponse(
        assignment_id=assignment.id,
        id=assignment.id,
        status="OTP_REQUESTED",
        expires_in_seconds=settings.FIELD_OTP_EXPIRY_SECONDS,
        message="Verification OTP successfully generated and dispatched to the customer's authenticated channel."
    )


@router.post("/jobs/{assignment_id}/verify-otp", response_model=OtpVerifyResponse)
async def verify_customer_otp(
    assignment_id: int,
    req: OtpVerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Field Engineer", "NOC", "Admin"]))
):
    """
    Validates the customer-provided OTP against the stored PBKDF2 hash.
    Enforces maximum attempts and expiry. On success, transitions assignment to OTP_VERIFIED.
    """
    assignment = db.query(FieldAssignment).filter(FieldAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    if current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or assignment.engineer_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: Cannot verify OTP for another engineer's job.")

    if assignment.status not in ("OTP_REQUESTED", "WORKING"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot verify OTP in status '{assignment.status}'. Job must be in 'OTP_REQUESTED' state."
        )

    otp_val = req.get_otp_code()
    if not otp_val:
        raise HTTPException(status_code=400, detail="OTP code is required.")

    is_valid, msg, attempts_left = verify_customer_submitted_otp(db, assignment, otp_val)

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=msg
        )

    # Publish otp_verified event
    verified_event = {
        "event": "otp_verified",
        "assignment_id": assignment.id,
        "ticket_id": assignment.ticket_id,
        "status": "OTP_VERIFIED",
        "timestamp": datetime.utcnow().isoformat()
    }
    active_session = db.query(TrackingSession).filter(
        TrackingSession.field_assignment_id == assignment.id
    ).order_by(TrackingSession.created_at.desc()).first()
    if active_session:
        await event_publisher.publish(f"session:{active_session.id}", verified_event)
    await event_publisher.publish(f"ticket:{assignment.ticket_id}", verified_event)
    await event_publisher.publish(f"market:{assignment.market_id.lower()}", verified_event)

    return OtpVerifyResponse(
        verified=True,
        assignment_id=assignment.id,
        id=assignment.id,
        status=assignment.status,
        message=msg,
        attempts_remaining=attempts_left
    )


@router.post("/jobs/{assignment_id}/complete", response_model=FieldAssignmentResponse)
async def complete_job_and_close_ticket(
    assignment_id: int,
    req: Optional[CompleteJobRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Field Engineer", "NOC", "Admin"]))
):
    """
    Transactional completion of the field job and associated ticket.
    Strictly verifies that customer OTP was verified beforehand.
    """
    assignment = db.query(FieldAssignment).populate_existing().with_for_update().filter(FieldAssignment.id == assignment_id).first()

    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    if current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or assignment.engineer_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: Cannot complete another engineer's job.")

    closure_notes = (req.resolution_notes or req.notes) if req else None
    completed_assignment, _ = execute_ticket_completion_transaction(db, assignment, current_user, notes=closure_notes)

    # Broadcast job_completed event
    comp_event = {
        "event": "job_completed",
        "assignment_id": completed_assignment.id,
        "ticket_id": completed_assignment.ticket_id,
        "status": "COMPLETED",
        "timestamp": datetime.utcnow().isoformat()
    }
    active_session = db.query(TrackingSession).filter(
        TrackingSession.field_assignment_id == completed_assignment.id
    ).order_by(TrackingSession.created_at.desc()).first()
    if active_session:
        await event_publisher.publish(f"session:{active_session.id}", comp_event)
    await event_publisher.publish(f"ticket:{completed_assignment.ticket_id}", comp_event)
    await event_publisher.publish(f"market:{completed_assignment.market_id.lower()}", comp_event)

    return _build_field_assignment_response(completed_assignment)


# --- 4. Customer Live Tracking & IDOR Protection ---

@router.get("/customer/tickets/{ticket_id}/tracking", response_model=CustomerTrackingResponse)
@router.get("/tracking/{ticket_id}", response_model=CustomerTrackingResponse)
def get_customer_ticket_tracking(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    Customer Live Tracking endpoint with strict IDOR protection.
    A Customer role user can ONLY track their own ticket.
    If the job is in OTP_REQUESTED state, the authenticated customer receives their OTP code.
    """
    ticket = db.query(Ticket).options(
        joinedload(Ticket.customer),
        joinedload(Ticket.assigned_resource),
        joinedload(Ticket.field_assignment)
    ).filter(Ticket.id == ticket_id).first()

    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Strict IDOR validation for Customer role
    if current_user.role == "Customer":
        if not ticket.customer or ticket.customer.email.lower().strip() != current_user.email.lower().strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access forbidden: You can only view tracking for your own registered tickets."
            )
    elif current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or ticket.assigned_resource_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: You can only track your own assigned jobs.")
    elif current_user.role == "Viewer":
        # Viewer gets generalized tracking info without raw coordinates
        pass
    elif current_user.role not in ("NOC", "Admin", "Care", "Executive"):
        raise HTTPException(status_code=403, detail="Unauthorized to view tracking.")

    # Market isolation validation
    if current_user.role in ("NOC", "Care", "Viewer") and ticket.market_id != market:
        raise HTTPException(status_code=403, detail=f"Ticket does not belong to active market '{market}'.")

    assignment = ticket.field_assignment
    engineer = ticket.assigned_resource
    customer = ticket.customer

    eta = None
    if engineer and customer and customer.service_latitude and customer.service_longitude:
        eta = calculate_eta_minutes(
            engineer.current_latitude,
            engineer.current_longitude,
            customer.service_latitude,
            customer.service_longitude
        )

    # Deliver OTP to authenticated customer or authorized staff viewing customer tracking
    otp_code = None
    otp_ttl = None
    if assignment and assignment.status in ("OTP_REQUESTED", "WORKING"):
        if current_user.role in ("Customer", "Admin", "NOC", "Executive", "Care"):
            active_otp = get_active_otp_for_customer(db, ticket.id, customer.id if customer else None)
            if not active_otp and assignment.status == "OTP_REQUESTED" and customer:
                # If OTP record was missing or expired while engineer is actively awaiting handshake, regenerate securely
                otp_rec, fresh_raw = generate_and_dispatch_customer_otp(db, assignment, customer)
                active_otp = {
                    "otp_code": fresh_raw,
                    "expires_in_seconds": settings.FIELD_OTP_EXPIRY_SECONDS
                }
            if active_otp:
                otp_code = active_otp["otp_code"]
                otp_ttl = active_otp["expires_in_seconds"]

    # Tracking active state
    active_session = db.query(TrackingSession).filter(
        TrackingSession.field_assignment_id == assignment.id,
        TrackingSession.status == "ACTIVE"
    ).first() if assignment else None
    tracking_active = bool(active_session and assignment and assignment.status in ("EN_ROUTE", "ARRIVED", "WORKING", "OTP_REQUESTED"))

    otp_detail = None
    if otp_code:
        otp_detail = OtpDetail(
            available=True,
            code=otp_code,
            expires_at=(datetime.utcnow() + timedelta(seconds=otp_ttl)).isoformat() if otp_ttl else None,
            remaining_seconds=otp_ttl
        )

    # Privacy mask: Viewer cannot see precise engineer GPS coordinates
    lat = engineer.current_latitude if (engineer and current_user.role != "Viewer") else None
    lng = engineer.current_longitude if (engineer and current_user.role != "Viewer") else None

    # Ticket status override if field job is actively in progress
    ticket_status = ticket.status
    if assignment and assignment.status in ("EN_ROUTE", "ARRIVED", "WORKING", "OTP_REQUESTED") and ticket_status in ("Resolved", "Closed"):
        ticket_status = "In Progress"

    return CustomerTrackingResponse(
        ticket_id=ticket.id,
        ticket_code=ticket.ticket_code,
        status=ticket_status,
        category=ticket.category,
        priority=ticket.priority,
        assigned_engineer_name=engineer.name if engineer else None,
        assigned_engineer_phone=engineer.phone if engineer else None,
        engineer_status=assignment.status if assignment else None,
        location_status=engineer.location_status if engineer else "UNAVAILABLE",
        engineer_latitude=lat,
        engineer_longitude=lng,
        service_latitude=customer.service_latitude if customer else None,
        service_longitude=customer.service_longitude if customer else None,
        service_address=customer.service_address if customer else None,
        eta_minutes=eta,
        otp_code=otp_code,
        otp_expires_in_seconds=otp_ttl,
        notes=assignment.notes if assignment else None,
        assignment_id=assignment.id if assignment else None,
        tracking_active=tracking_active,
        job_status=assignment.status if assignment else None,
        otp=otp_detail
    )


# --- 5. Real-Time Streaming & History Endpoints ---

@router.get("/tracking/{session_id}/stream")
async def stream_session_tracking(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    SSE stream for real-time location pings and status events of a specific tracking session.
    Strictly validates subscriber authorization and boundaries (Section 18).
    """
    if current_user.role == "Viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Viewers cannot subscribe to live telemetry streams."
        )

    session = db.query(TrackingSession).options(
        joinedload(TrackingSession.field_assignment).joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
        joinedload(TrackingSession.engineer)
    ).filter(TrackingSession.id == session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Tracking session not found")

    assignment = session.field_assignment
    ticket = assignment.ticket if assignment else None
    customer = ticket.customer if ticket else None
    engineer = session.engineer

    # Authorization validation
    if current_user.role == "Customer":
        if not customer or customer.email.lower().strip() != current_user.email.lower().strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You cannot subscribe to another customer's tracking session."
            )
    elif current_user.role == "Field Engineer":
        user_eng = _resolve_engineer_resource(db, current_user)
        if not user_eng or session.engineer_id != user_eng.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You cannot subscribe to another engineer's tracking session."
            )
    elif current_user.role in ("NOC", "Care"):
        if session.market_id.lower() != market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Session does not belong to active market '{market}'."
            )
    elif current_user.role != "Admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized stream access.")

    async def event_generator():
        channel = f"session:{session_id}"
        yield f"data: {json.dumps({'event': 'connected', 'session_id': session_id})}\n\n"
        async for msg in event_publisher.subscribe(channel):
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/tickets/{ticket_id}/stream")
@router.get("/tracking/tickets/{ticket_id}/stream")
async def stream_ticket_tracking(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    SSE stream for real-time tracking events for a specific ticket.
    Provides direct subscription for customer portal and ticket watchers with strict authorization.
    """
    if current_user.role == "Viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Viewers cannot subscribe to live telemetry streams."
        )

    ticket = db.query(Ticket).options(
        joinedload(Ticket.customer),
        joinedload(Ticket.assigned_resource)
    ).filter(Ticket.id == ticket_id).first()

    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    customer = ticket.customer
    engineer = ticket.assigned_resource

    # Authorization validation
    if current_user.role == "Customer":
        if not customer or customer.email.lower().strip() != current_user.email.lower().strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You cannot subscribe to another customer's ticket stream."
            )
    elif current_user.role == "Field Engineer":
        user_eng = _resolve_engineer_resource(db, current_user)
        if not user_eng or ticket.assigned_resource_id != user_eng.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You cannot subscribe to another engineer's ticket stream."
            )
    elif current_user.role in ("NOC", "Care"):
        if ticket.market_id.lower() != market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Ticket does not belong to active market '{market}'."
            )
    elif current_user.role not in ("Admin", "Executive"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized stream access.")

    async def event_generator():
        channel = f"ticket:{ticket_id}"
        yield f"data: {json.dumps({'event': 'connected', 'ticket_id': ticket_id})}\n\n"
        async for msg in event_publisher.subscribe(channel):
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/stream")
async def stream_market_operations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """
    SSE stream for the Operations map broadcasting all active engineer location updates in current market.
    """
    async def event_generator():
        channel = f"market:{market.lower()}"
        yield f"data: {json.dumps({'event': 'connected', 'market': market})}\n\n"
        async for msg in event_publisher.subscribe(channel):
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- 6. Google Directions & Road Routing ---

@router.get("/jobs/{assignment_id}/route", response_model=RouteResponse)
@router.get("/route/{assignment_id}", response_model=RouteResponse)
def get_job_route(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    market: str = Depends(get_current_market)
):
    """
    Computes road route geometry, distance, and ETA using RoutingService.
    Supports caching, rate limiting, and automatic fallback.
    """
    assignment = db.query(FieldAssignment).options(
        joinedload(FieldAssignment.ticket).joinedload(Ticket.customer),
        joinedload(FieldAssignment.engineer)
    ).filter(FieldAssignment.id == assignment_id).first()

    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    ticket = assignment.ticket
    customer = ticket.customer if ticket else None
    engineer = assignment.engineer

    # Authorization
    if current_user.role == "Customer":
        if not customer or customer.email.lower().strip() != current_user.email.lower().strip():
            raise HTTPException(status_code=403, detail="Forbidden: You can only view route for your own ticket.")
    elif current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or assignment.engineer_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: You can only view route for your own assigned job.")
    elif current_user.role == "Viewer":
        raise HTTPException(status_code=403, detail="Forbidden: Viewer cannot access detailed route coordinates.")
    elif current_user.role not in ("NOC", "Admin", "Care", "Executive"):
        raise HTTPException(status_code=403, detail="Unauthorized")

    if not engineer or engineer.current_latitude is None or engineer.current_longitude is None:
        raise HTTPException(status_code=400, detail="Engineer location currently unavailable.")

    if not customer or customer.service_latitude is None or customer.service_longitude is None:
        raise HTTPException(status_code=400, detail="Customer service location not configured.")

    route_res = routing_service.calculate_route(
        origin_lat=engineer.current_latitude,
        origin_lng=engineer.current_longitude,
        dest_lat=customer.service_latitude,
        dest_lng=customer.service_longitude
    )

    return RouteResponse(
        distance_meters=route_res.distance_meters,
        distance_km=route_res.distance_meters / 1000.0,
        duration_seconds=route_res.duration_seconds,
        eta_minutes=route_res.eta_minutes,
        provider=route_res.provider,
        polyline=route_res.polyline,
        waypoints=[RouteWaypoint(lat=w["lat"], lng=w["lng"]) for w in route_res.waypoints],
        cached=route_res.cached
    )


@router.get("/jobs/{assignment_id}/tracking-history", response_model=List[LocationPingResponse])
def get_job_tracking_history(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Field Engineer"])),
    market: str = Depends(get_current_market)
):
    """
    Retrieves historical location breadcrumbs for SLA analysis and audit investigation.
    Viewer is strictly forbidden from accessing raw GPS history.
    """
    assignment = db.query(FieldAssignment).filter(FieldAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found")

    if current_user.role == "Field Engineer":
        eng = _resolve_engineer_resource(db, current_user)
        if not eng or assignment.engineer_id != eng.id:
            raise HTTPException(status_code=403, detail="Forbidden: You can only view history for your own jobs.")

    pings = db.query(LocationPing).join(
        TrackingSession, LocationPing.tracking_session_id == TrackingSession.id
    ).filter(
        TrackingSession.field_assignment_id == assignment_id
    ).order_by(LocationPing.recorded_at.asc()).limit(500).all()

    return pings
