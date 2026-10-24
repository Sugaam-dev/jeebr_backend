"""
app/services/survey_dispatch_service.py

SentinelOS CRM Automated Field-Survey Dispatch Service.
Orchestrates physical on-ground feasibility survey dispatch for unresolved customer
installation addresses (serviceability_status = PENDING_SURVEY or missing GPS coordinates).

Workflow:
1. Address Creation / Update: Evaluates whether physical survey is required.
2. Idempotency Check: Checks whether an active open survey ticket already exists
   for this customer and address (prevents duplicate dispatch on repeated saves).
3. Priority & SLA: Assigns priority P4 with standard 24h SLA.
4. Regional Engineer Assignment: Queries available field engineers in the customer's
   locality/market with available capacity.
5. Graceful Queueing: If no eligible engineer is available, leaves the ticket Open
   with an explicit audit reason ("Queued: All field engineers at maximum capacity").
6. Field Closure & Reconciliation: When field verification completes, updates
   verified GPS coordinates, marks address FEASIBLE/SERVICEABLE, and resolves the ticket.
"""

from datetime import datetime, timedelta
from typing import Optional, Tuple, Any
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Ticket, Resource, Customer, FieldAssignment, AuditLog
from app.models_crm import CRMCustomer, CRMAddress, CRMActivity, ServiceabilityStatus, ActivityType
from app.services.ticketing_engine import find_best_regional_resource, generate_ticket_code
from app.services.workload_service import get_active_jobs_count


def dispatch_field_survey_if_needed(
    db: Session,
    customer: Optional[Customer],
    crm_cust: CRMCustomer,
    address: CRMAddress,
    current_user: Any
) -> Optional[Ticket]:
    """
    Checks if an address requires a physical survey and transactionally creates
    an automated survey ticket if no active survey ticket exists.
    """
    if address.is_archived:
        return None

    # Check if survey is required: explicit PENDING_SURVEY or unverified coordinates
    requires_survey = (
        address.serviceability_status == ServiceabilityStatus.PENDING_SURVEY
        or address.latitude is None
        or address.longitude is None
    )

    if not requires_survey:
        return None

    # 1. Idempotency Check: Search for existing open survey ticket for this address
    addr_tag = f"[SURVEY-ADDR:{address.id}]"
    existing_ticket = db.query(Ticket).filter(
        Ticket.market_id == crm_cust.market_id,
        Ticket.description.contains(addr_tag),
        Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
    ).first()

    if existing_ticket:
        # Existing open survey ticket found; do not duplicate
        return existing_ticket

    # 2. Derive Customer identity
    resolved_customer_id = customer.id if customer else None
    if not resolved_customer_id and crm_cust.legacy_customer_id:
        resolved_customer_id = crm_cust.legacy_customer_id

    # 3. Derive locality/region
    region = address.city or (customer.locality if customer else "Regional Central")
    ticket_code = generate_ticket_code(db, crm_cust.market_id, "REG")
    sla_deadline = datetime.utcnow() + timedelta(hours=24)  # P4 standard SLA

    description = (
        f"{addr_tag} Physical on-ground feasibility survey required for "
        f"{crm_cust.legal_name} ({crm_cust.customer_number}). "
        f"Premise: {address.address_line1}, {address.city}, {address.postal_code}. "
        f"Serviceability: {address.serviceability_status}."
    )

    new_ticket = Ticket(
        market_id=crm_cust.market_id,
        ticket_code=ticket_code,
        source="CUSTOMER",
        region=region,
        customer_id=resolved_customer_id,
        category="Install",
        priority="P4",
        description=description,
        sla_deadline=sla_deadline,
        created_at=datetime.utcnow()
    )

    # 4. Regional Field Engineer Assignment
    assigned_res = find_best_regional_resource(db, crm_cust.market_id, region)

    if assigned_res:
        new_ticket.assigned_resource_id = assigned_res.id
        new_ticket.assigned_at = datetime.utcnow()
        new_ticket.approval_status = "NOT_REQUIRED"
        new_ticket.status = "Assigned"
        new_ticket.ai_triage_action = f"Auto-assigned survey to {assigned_res.name} ({assigned_res.region})"

        # Create authoritative FieldAssignment
        field_assignment = FieldAssignment(
            market_id=crm_cust.market_id,
            ticket_id=None,  # will set after ticket flush
            engineer_id=assigned_res.id,
            status="ASSIGNED",
            assigned_at=datetime.utcnow(),
            notes=f"Automated site survey dispatch for {crm_cust.legal_name}"
        )
        db.add(new_ticket)
        db.flush()

        field_assignment.ticket_id = new_ticket.id
        db.add(field_assignment)

        # Update engineer workload
        assigned_res.active_tickets_count = get_active_jobs_count(db, assigned_res.id)
        if assigned_res.active_tickets_count >= assigned_res.max_capacity:
            assigned_res.status = "Busy"
    else:
        # Graceful queueing when no regional technician is available
        new_ticket.assigned_resource_id = None
        new_ticket.approval_status = "NOT_REQUIRED"
        new_ticket.status = "Open"
        new_ticket.ai_triage_action = "Queued for regional field dispatch: All field engineers at maximum capacity or unavailable"
        db.add(new_ticket)
        db.flush()

    # 5. Activity & Audit Trail
    activity = CRMActivity(
        id=datetime.utcnow().timestamp(),  # will be assigned UUID if type demands
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.SYSTEM,
        summary=f"Dispatched Automated Survey Ticket {new_ticket.ticket_code}",
        details=(
            f"Ticket: {new_ticket.ticket_code} (P4). Status: {new_ticket.status}. "
            f"Assigned Engineer: {assigned_res.name if assigned_res else 'Queued (High Capacity)'}. "
            f"Address: {address.address_line1}, {address.city}."
        ),
        performed_by_id=current_user.id if hasattr(current_user, "id") else None
    )
    # Ensure activity id is valid uuid
    import uuid
    activity.id = uuid.uuid4()
    db.add(activity)

    db.commit()
    db.refresh(new_ticket)
    return new_ticket


def complete_field_survey(
    db: Session,
    customer: Optional[Customer],
    crm_cust: CRMCustomer,
    address: CRMAddress,
    latitude: float,
    longitude: float,
    serviceability_status: str = ServiceabilityStatus.SERVICEABLE,
    verified_by_user: Any = None,
    ticket_id: Optional[int] = None,
    candidate_node_id: Optional[int] = None,
    distance_to_node_meters: Optional[float] = None,
    notes: Optional[str] = None
) -> Tuple[CRMAddress, Optional[Ticket]]:
    """
    Applies verified on-ground GPS coordinates and serviceability status to address,
    reconciles linked operational customer record, and resolves open survey ticket.
    """
    # 1. Coordinate range validation
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        raise ValueError("Invalid GPS coordinates. Latitude must be in [-90, 90] and Longitude in [-180, 180].")

    if serviceability_status not in ServiceabilityStatus.ALL:
        raise ValueError(f"Invalid serviceability status '{serviceability_status}'. Allowed: {ServiceabilityStatus.ALL}")

    # 2. Update CRM Address
    address.latitude = latitude
    address.longitude = longitude
    address.serviceability_status = serviceability_status
    if candidate_node_id:
        address.candidate_node_id = candidate_node_id
    if distance_to_node_meters is not None:
        address.distance_to_node_meters = distance_to_node_meters
    address.updated_at = datetime.utcnow()

    # 3. Update legacy operational Customer if linked
    if customer:
        customer.service_latitude = latitude
        customer.service_longitude = longitude
        if address.address_line1:
            customer.service_address = address.address_line1

    # 4. Find linked survey ticket
    survey_ticket: Optional[Ticket] = None
    if ticket_id:
        survey_ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    else:
        addr_tag = f"[SURVEY-ADDR:{address.id}]"
        survey_ticket = db.query(Ticket).filter(
            Ticket.market_id == crm_cust.market_id,
            Ticket.description.contains(addr_tag),
            Ticket.status.notin_(["Resolved", "Closed", "Rejected"])
        ).first()

    # 5. Resolve survey ticket and complete FieldAssignment
    if survey_ticket:
        survey_ticket.status = "Resolved"
        survey_ticket.resolved_at = datetime.utcnow()
        survey_ticket.ai_triage_action = f"Survey completed & verified: GPS ({latitude:.4f}, {longitude:.4f})"

        fa = db.query(FieldAssignment).filter(FieldAssignment.ticket_id == survey_ticket.id).first()
        if fa:
            fa.status = "COMPLETED"
            fa.completed_at = datetime.utcnow()
            if notes:
                fa.notes = (fa.notes or "") + f" | Survey Verified: {notes}"

        # Decrement engineer active tickets count
        if survey_ticket.assigned_resource:
            survey_ticket.assigned_resource.active_tickets_count = get_active_jobs_count(
                db, survey_ticket.assigned_resource.id
            )

    # 6. Compliance Audit Activity
    import uuid
    verifier_name = getattr(verified_by_user, "full_name", None) or getattr(verified_by_user, "email", "Authorized User")
    activity = CRMActivity(
        id=uuid.uuid4(),
        crm_customer_id=crm_cust.id,
        activity_type=ActivityType.ADDRESS_UPDATED,
        summary="Physical Survey Completed & GPS Coordinates Verified",
        details=(
            f"Verified GPS: ({latitude:.5f}, {longitude:.5f}). Status: {serviceability_status}. "
            f"Survey Ticket: {survey_ticket.ticket_code if survey_ticket else 'N/A'}. "
            f"Verified by: {verifier_name}. Notes: {notes or 'Ground survey verified on premise.'}"
        ),
        performed_by_id=getattr(verified_by_user, "id", None)
    )
    db.add(activity)

    db.commit()
    db.refresh(address)
    if survey_ticket:
        db.refresh(survey_ticket)

    return address, survey_ticket
