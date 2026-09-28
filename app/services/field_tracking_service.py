import math
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.config import settings
from app.models import (
    FieldAssignment, TrackingSession, LocationPing, Ticket, Resource, Customer, AuditLog, User
)
from app.services.event_publisher import event_publisher
import logging

logger = logging.getLogger(__name__)

# Legal state machine transitions
LEGAL_TRANSITIONS = {
    "ASSIGNED": {"ACCEPTED", "REJECTED", "CANCELLED"},
    "ACCEPTED": {"EN_ROUTE", "CANCELLED", "REASSIGNED"},
    "EN_ROUTE": {"ARRIVED", "CANCELLED", "ON_HOLD"},
    "ARRIVED": {"WORKING", "ON_HOLD"},
    "WORKING": {"OTP_REQUESTED", "ON_HOLD", "FAILED"},
    "OTP_REQUESTED": {"OTP_VERIFIED", "ON_HOLD"},
    "OTP_VERIFIED": {"COMPLETED"},
    "ON_HOLD": {"EN_ROUTE", "WORKING", "REASSIGNED"},
    "FAILED": {"REASSIGNED"},
    "REJECTED": {"ASSIGNED", "REASSIGNED"},
    "COMPLETED": set(),
    "CANCELLED": set()
}

# Coordinate bounding boxes for market validation
MARKET_BOUNDS = {
    "mumbai": {"lat_min": 18.0, "lat_max": 20.0, "lon_min": 72.0, "lon_max": 73.8},
    "kolkata": {"lat_min": 22.0, "lat_max": 23.2, "lon_min": 87.8, "lon_max": 89.2}
}


def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance between two points in kilometers."""
    R = 6371.0  # Earth's radius in kilometers
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


from app.services.routing_service import routing_service


def calculate_eta_minutes(
    eng_lat: Optional[float],
    eng_lon: Optional[float],
    dest_lat: Optional[float],
    dest_lon: Optional[float],
    speed_kmh: float = 25.0
) -> Optional[int]:
    """
    Computes road-aware ETA minutes using the unified RoutingService (Google Routing with SentinelOS fallback).
    """
    if eng_lat is None or eng_lon is None or dest_lat is None or dest_lon is None:
        return None
    try:
        route = routing_service.calculate_route(eng_lat, eng_lon, dest_lat, dest_lon)
        return route.eta_minutes
    except Exception as e:
        logger.warning(f"Error calculating ETA via routing_service: {e}")
        crow_dist_km = calculate_haversine_distance(eng_lat, eng_lon, dest_lat, dest_lon)
        if crow_dist_km < 0.05:
            return 0
        road_dist_km = crow_dist_km * 1.35
        hours = road_dist_km / max(speed_kmh, 5.0)
        return max(1, int(round(hours * 60)))


def validate_coordinates(lat: float, lon: float, market_id: Optional[str] = None) -> None:
    """Validate latitude/longitude ranges and market sanity bounds."""
    if not (-90.0 <= lat <= 90.0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid latitude: {lat}. Must be between -90 and 90."
        )
    if not (-180.0 <= lon <= 180.0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid longitude: {lon}. Must be between -180 and 180."
        )

    if market_id and market_id.lower() in MARKET_BOUNDS:
        bounds = MARKET_BOUNDS[market_id.lower()]
        if not (bounds["lat_min"] <= lat <= bounds["lat_max"] and bounds["lon_min"] <= lon <= bounds["lon_max"]):
            logger.warning(f"Coordinate ({lat}, {lon}) is outside typical bounds for market '{market_id}'")


def transition_assignment_state(
    db: Session,
    assignment: FieldAssignment,
    target_status: str,
    user: User,
    notes: Optional[str] = None
) -> FieldAssignment:
    """
    Enforces legal state machine transitions for field assignments.
    Updates timestamps, manages tracking sessions, and logs governance audit events.
    """
    target = target_status.upper().strip()
    current = assignment.status.upper().strip()

    if target not in LEGAL_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Illegal state transition from '{current}' to '{target}'. Legal transitions: {list(LEGAL_TRANSITIONS.get(current, set()))}"
        )

    now = datetime.utcnow()
    assignment.status = target
    if notes:
        assignment.notes = f"{assignment.notes or ''}\n[{now.strftime('%Y-%m-%d %H:%M')}] {notes}".strip()

    # Handle state-specific side effects
    if target in ("ACCEPTED", "EN_ROUTE", "ARRIVED", "WORKING", "OTP_REQUESTED"):
        if assignment.ticket and assignment.ticket.status in ("Resolved", "Closed", "Open"):
            assignment.ticket.status = "In Progress"

    if target == "ACCEPTED":
        assignment.accepted_at = now
    elif target == "EN_ROUTE":
        assignment.en_route_at = now
        # Start tracking session if not already active
        active_session = db.query(TrackingSession).filter(
            TrackingSession.field_assignment_id == assignment.id,
            TrackingSession.status == "ACTIVE"
        ).first()
        if not active_session:
            new_session = TrackingSession(
                market_id=assignment.market_id,
                field_assignment_id=assignment.id,
                engineer_id=assignment.engineer_id,
                status="ACTIVE",
                started_at=now
            )
            db.add(new_session)
    elif target == "ARRIVED":
        assignment.arrived_at = now
    elif target == "WORKING":
        assignment.work_started_at = now
    elif target == "COMPLETED":
        if assignment.ticket:
            assignment.ticket.status = "Resolved"
    elif target in ("ON_HOLD", "FAILED", "CANCELLED", "REJECTED"):
        # Stop tracking session if moving away from active work
        active_session = db.query(TrackingSession).filter(
            TrackingSession.field_assignment_id == assignment.id,
            TrackingSession.status == "ACTIVE"
        ).first()
        if active_session:
            active_session.status = "STOPPED"
            active_session.ended_at = now

    db.commit()
    db.refresh(assignment)

    # Log Governance Audit Event
    audit = AuditLog(
        market_id=assignment.market_id,
        recommendation_id=None,
        source_module="Field Operations",
        action_taken=f"Field assignment #{assignment.id} transitioned from {current} to {target}",
        decision=f"FIELD_{target}",
        user_id=user.id,
        user_name=user.full_name,
        user_role=user.role,
        confidence_score=1.0,
        original_signals={"assignment_id": assignment.id, "ticket_id": assignment.ticket_id, "previous_status": current},
        execution_result={"new_status": target, "notes": notes},
        timestamp=now
    )
    db.add(audit)
    db.commit()

    return assignment


async def record_location_ping_async(
    db: Session,
    assignment: FieldAssignment,
    engineer: Resource,
    ping_data: dict
) -> LocationPing:
    """
    Validates and persists a location ping from the field engineer.
    Broadcasts real-time SSE updates to tracking subscribers and the Operations dashboard.
    """
    # Coordinate and metric validations
    lat = ping_data["latitude"]
    lon = ping_data["longitude"]
    validate_coordinates(lat, lon, assignment.market_id)

    accuracy = ping_data.get("accuracy")
    if accuracy is None:
        accuracy = 10.0
    if accuracy < 0.0:
        raise HTTPException(status_code=400, detail="Accuracy cannot be negative.")
    if accuracy > 5000.0:
        raise HTTPException(status_code=400, detail="Accuracy reading exceeds plausible bounds (max 5000 meters).")

    speed = ping_data.get("speed")
    if speed is None:
        speed = 0.0
    elif speed < 0.0:
        raise HTTPException(status_code=400, detail="Speed cannot be negative.")
    elif speed > 100.0:
        raise HTTPException(status_code=400, detail="Speed exceeds plausible bounds (max 100 m/s).")

    heading = ping_data.get("heading")
    if heading is None:
        heading = 0.0
    elif not (0.0 <= heading <= 360.0):
        raise HTTPException(status_code=400, detail="Heading must be between 0 and 360 degrees.")

    is_mock = bool(ping_data.get("is_mock", False))

    now = datetime.utcnow()
    recorded_at = ping_data.get("recorded_at") or now
    if isinstance(recorded_at, str):
        try:
            recorded_at = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except Exception:
            recorded_at = now
    if hasattr(recorded_at, "tzinfo") and recorded_at.tzinfo is not None:
        recorded_at = recorded_at.replace(tzinfo=None)

    # Timestamp sanity check: reject future > 60s or older than 1 hour
    if (recorded_at - now).total_seconds() > 60:
        raise HTTPException(status_code=400, detail="Recorded timestamp cannot be in the future.")
    if (now - recorded_at).total_seconds() > 3600:
        raise HTTPException(status_code=400, detail="Recorded timestamp is too old (> 1 hour).")

    # Locate active tracking session (or auto-instantiate if job is in active execution)
    session = db.query(TrackingSession).filter(
        TrackingSession.field_assignment_id == assignment.id,
        TrackingSession.status == "ACTIVE"
    ).order_by(TrackingSession.created_at.desc()).first()

    if not session:
        if assignment.status in ("ASSIGNED", "ACCEPTED", "EN_ROUTE", "ARRIVED", "WORKING", "OTP_REQUESTED"):
            session = TrackingSession(
                field_assignment_id=assignment.id,
                engineer_id=engineer.id,
                market_id=assignment.market_id,
                status="ACTIVE",
                started_at=now
            )
            db.add(session)
            db.flush()
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No active tracking session found for job in state '{assignment.status}'. Accept job or start transit first."
            )

    # Teleportation sanity check
    if engineer.current_latitude and engineer.current_longitude and engineer.last_ping_at:
        delta_sec = max(1.0, (now - engineer.last_ping_at).total_seconds())
        distance_km = calculate_haversine_distance(engineer.current_latitude, engineer.current_longitude, lat, lon)
        speed_kmh = (distance_km / delta_sec) * 3600
        if speed_kmh > 200.0:
            logger.warning(
                f"[SUSPICIOUS_LOCATION] Impossible speed ({speed_kmh:.1f} km/h) for engineer #{engineer.id}. "
                f"Delta: {distance_km:.2f} km in {delta_sec:.1f}s."
            )

    # Persist location ping
    ping = LocationPing(
        tracking_session_id=session.id,
        engineer_id=engineer.id,
        market_id=assignment.market_id,
        latitude=lat,
        longitude=lon,
        accuracy=accuracy,
        speed=speed,
        heading=heading,
        is_mock=is_mock,
        recorded_at=recorded_at,
        received_at=now
    )
    db.add(ping)

    # Update TrackingSession and Resource
    session.last_location_at = now
    engineer.current_latitude = lat
    engineer.current_longitude = lon
    engineer.last_ping_at = now
    engineer.location_status = "ACTIVE"
    db.commit()
    db.refresh(ping)

    # Calculate live ETA if customer has service coordinates
    ticket = assignment.ticket
    eta_mins = None
    if ticket and ticket.customer:
        eta_mins = calculate_eta_minutes(
            lat, lon,
            ticket.customer.service_latitude,
            ticket.customer.service_longitude
        )

    # Broadcast location update via abstracted publisher
    event_payload = {
        "event": "location_update",
        "session_id": session.id,
        "assignment_id": assignment.id,
        "engineer_id": engineer.id,
        "engineer_name": engineer.name,
        "market_id": assignment.market_id,
        "latitude": lat,
        "longitude": lon,
        "accuracy": ping.accuracy,
        "speed": ping.speed,
        "heading": ping.heading,
        "is_mock": ping.is_mock,
        "status": assignment.status,
        "eta_minutes": eta_mins,
        "timestamp": now.isoformat()
    }

    # Session channel (for customer & engineer), Ticket channel, and Market channel (for operations)
    await event_publisher.publish(f"session:{session.id}", event_payload)
    await event_publisher.publish(f"ticket:{assignment.ticket_id}", event_payload)
    await event_publisher.publish(f"market:{assignment.market_id.lower()}", event_payload)

    return ping


def execute_ticket_completion_transaction(
    db: Session,
    assignment: FieldAssignment,
    user: User,
    notes: Optional[str] = None
) -> Tuple[FieldAssignment, Ticket]:
    """
    Executes atomic ticket completion.
    Strictly enforces that customer OTP was verified beforehand (backend check on otp_verified_at).
    Closes ticket, marks assignment COMPLETED, stops tracking session, and logs audit record.
    """
    # Atomically lock assignment row to prevent concurrent completion race conditions
    locked_assignment = db.query(FieldAssignment).filter(
        FieldAssignment.id == assignment.id
    ).populate_existing().with_for_update().first()
    if not locked_assignment:
        raise HTTPException(status_code=404, detail="Field assignment not found.")
    assignment = locked_assignment

    # Reject duplicate completion if already completed (race condition protection)
    if assignment.status == "COMPLETED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot complete job: Job is already completed and closed."
        )

    if assignment.status != "OTP_VERIFIED" or not assignment.otp_verified_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot complete job: Customer confirmation OTP has not been verified. "
                   "Engineer must verify customer OTP before closure."
        )

    now = datetime.utcnow()
    ticket = assignment.ticket
    if not ticket:
        raise HTTPException(status_code=404, detail="Associated ticket not found.")

    try:
        # 1. Complete Field Assignment
        assignment.status = "COMPLETED"
        assignment.completed_at = now
        if notes:
            assignment.notes = (assignment.notes or "") + f"\n[{now.strftime('%Y-%m-%d %H:%M')}] Closure: {notes}"

        # 2. Stop Tracking Session
        active_session = db.query(TrackingSession).filter(
            TrackingSession.field_assignment_id == assignment.id,
            TrackingSession.status == "ACTIVE"
        ).first()
        if active_session:
            active_session.status = "STOPPED"
            active_session.ended_at = now

        # 3. Resolve Ticket
        ticket.status = "Resolved"
        ticket.resolved_at = now

        # 4. Release Resource Workload Capacity
        engineer = assignment.engineer
        if engineer:
            engineer.location_status = "UNAVAILABLE"
            active_count = db.query(func.count(Ticket.id)).filter(
                Ticket.assigned_resource_id == engineer.id,
                Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
            ).scalar() or 0
            engineer.active_tickets_count = max(0, active_count - 1)
            if engineer.active_tickets_count < engineer.max_capacity:
                engineer.status = "Available"

        # 5. Commit Governance Audit Event
        audit = AuditLog(
            market_id=assignment.market_id,
            recommendation_id=None,
            source_module="Field Operations",
            action_taken=f"Ticket {ticket.ticket_code} completed via verified Customer OTP",
            decision="JOB_COMPLETED",
            user_id=user.id,
            user_name=user.full_name,
            user_role=user.role,
            confidence_score=1.0,
            original_signals={
                "ticket_id": ticket.id,
                "assignment_id": assignment.id,
                "engineer_id": engineer.id if engineer else None,
                "otp_verified_at": assignment.otp_verified_at.isoformat()
            },
            execution_result={
                "ticket_status": "Resolved",
                "assignment_status": "COMPLETED",
                "tracking_status": "STOPPED"
            },
            timestamp=now
        )
        db.add(audit)
        db.commit()

        db.refresh(assignment)
        db.refresh(ticket)
        return assignment, ticket

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to complete ticket transaction: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Transactional ticket closure failed: {str(e)}"
        )


def get_field_operations_summary(db: Session, market_id: str) -> dict:
    """Computes aggregate operational summary metrics for the given market."""
    assignments = db.query(FieldAssignment).filter(
        FieldAssignment.market_id == market_id,
        FieldAssignment.status.notin_(["COMPLETED", "CANCELLED", "REJECTED"])
    ).all()

    en_route_count = sum(1 for a in assignments if a.status == "EN_ROUTE")
    on_site_count = sum(1 for a in assignments if a.status in ("ARRIVED", "WORKING", "OTP_REQUESTED", "OTP_VERIFIED"))
    active_jobs_count = len(assignments)

    resources = db.query(Resource).filter(
        Resource.market_id == market_id,
        Resource.resource_type == "FIELD"
    ).all()

    available_count = sum(1 for r in resources if r.status == "Available")
    offline_count = sum(1 for r in resources if r.status == "Offline")
    active_engineers = len(resources) - offline_count

    # SLA at risk calculation: active tickets with < 2 hours remaining
    now = datetime.utcnow()
    sla_at_risk_count = db.query(func.count(Ticket.id)).filter(
        Ticket.market_id == market_id,
        Ticket.status.in_(["Assigned", "In-Progress", "Open"]),
        Ticket.sla_deadline.isnot(None),
        Ticket.sla_deadline < (now + timedelta(hours=2))
    ).scalar() or 0

    return {
        "market_id": market_id,
        "active_engineers": active_engineers,
        "en_route": en_route_count,
        "on_site": on_site_count,
        "available": available_count,
        "offline": offline_count,
        "active_jobs": active_jobs_count,
        "sla_at_risk": sla_at_risk_count
    }
