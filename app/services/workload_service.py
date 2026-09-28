"""
SentinelOS Engineer Workload & Capacity Management Service
Authoritative, server-side capacity calculation, atomic transaction locking to prevent race conditions,
and real-time event notifications.
"""
from typing import Dict, Any, List, Optional
import asyncio
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import Resource, FieldAssignment, Ticket
from app.services.event_publisher import event_publisher

# Section 21: Workload definition
ACTIVE_FIELD_STATES = [
    "ASSIGNED", "ACCEPTED", "EN_ROUTE", "ARRIVED", "WORKING", "OTP_REQUESTED", "OTP_VERIFIED"
]
TERMINAL_FIELD_STATES = [
    "COMPLETED", "CANCELLED", "REJECTED", "FAILED"
]

def get_active_jobs_count(db: Session, engineer_id: int) -> int:
    """Authoritative active job calculation from database state machine."""
    count = db.query(FieldAssignment).filter(
        FieldAssignment.engineer_id == engineer_id,
        FieldAssignment.status.in_(ACTIVE_FIELD_STATES)
    ).count()
    return count

def calculate_engineer_workload(db: Session, resource: Resource) -> Dict[str, Any]:
    """Expose a backend-calculated workload object adhering to Section 22."""
    active_count = get_active_jobs_count(db, resource.id)
    max_cap = resource.max_capacity if (resource.max_capacity and resource.max_capacity > 0) else 10
    available = max(0, max_cap - active_count)
    capacity_status = "AT_CAPACITY" if active_count >= max_cap else "AVAILABLE"

    # Sync counter on resource model if drifted
    if resource.active_tickets_count != active_count:
        resource.active_tickets_count = active_count
        if active_count >= max_cap:
            resource.status = "Busy"
        elif resource.status == "Busy" and active_count < max_cap:
            resource.status = "Available"

    return {
        "id": resource.id,
        "engineer_id": resource.id,
        "name": resource.name,
        "email": resource.email,
        "phone": resource.phone,
        "market_id": resource.market_id,
        "region": resource.region,
        "status": resource.status,
        "location_status": resource.location_status or "UNAVAILABLE",
        "current_latitude": resource.current_latitude,
        "current_longitude": resource.current_longitude,
        "last_ping_at": resource.last_ping_at,
        "active_tickets_count": active_count,
        "active_jobs": active_count,
        "max_capacity": max_cap,
        "max_active_jobs": max_cap,
        "available_capacity": available,
        "capacity_status": capacity_status
    }

def reserve_engineer_capacity(db: Session, resource_id: int) -> Resource:
    """
    Atomically reserve capacity for an engineer (Section 23 & 24).
    Acquires database row lock via SELECT ... FOR UPDATE to eliminate assignment race conditions.
    Raises HTTP 409 Conflict if active_jobs >= max_active_jobs.
    """
    engineer = db.query(Resource).populate_existing().with_for_update().filter(
        Resource.id == resource_id
    ).first()

    if not engineer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Field engineer resource with ID {resource_id} not found."
        )

    active_count = get_active_jobs_count(db, engineer.id)
    max_cap = engineer.max_capacity if (engineer.max_capacity and engineer.max_capacity > 0) else 10

    if active_count >= max_cap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ENGINEER_AT_CAPACITY",
                "message": f"Engineer '{engineer.name}' has reached maximum active job capacity ({active_count}/{max_cap}).",
                "active_jobs": active_count,
                "max_active_jobs": max_cap
            }
        )

    return engineer

async def emit_workload_update_event(market_id: str, engineer_id: int, active_jobs: int, max_jobs: int) -> None:
    """Broadcast workload state transition over SSE to all authorized clients."""
    payload = {
        "event": "workload_updated",
        "engineer_id": engineer_id,
        "active_jobs": active_jobs,
        "max_active_jobs": max_jobs,
        "available_capacity": max(0, max_jobs - active_jobs),
        "status": "AT_CAPACITY" if active_jobs >= max_jobs else "AVAILABLE"
    }
    await event_publisher.publish(f"market:{market_id.lower()}", payload)
