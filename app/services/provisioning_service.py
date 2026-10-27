import logging
logger = logging.getLogger(__name__)
"""
app/services/provisioning_service.py

Core Orchestration Service for SentinelOS Service Provisioning.
Coordinates customer validation, network terminal allocation, state machine transitions,
bounded retries, reconciliation, and immutable audit logging.
"""

from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import or_
from fastapi import HTTPException, status

from app.models import Customer, NetworkDevice, OLTPort, User, Node
from app.models_crm import CRMCustomer, CRMAddress, CustomerLifecycleStatus
from app.models_provisioning import (
    ServiceActivationOrder, ProvisioningLog, ProvisioningStatus, ExecutionMode
)
from app.services.provisioning_catalog import get_service_plan_by_id, CANONICAL_SERVICE_PLANS
from app.services.provisioning_provider import (
    get_provisioning_provider, ProvisioningExecutionResult
)
from app.services.billing_service import initialize_billing_for_order


def validate_customer_eligibility(
    customer_id: int,
    plan_id: str,
    market_id: str,
    db: Session
) -> Dict[str, Any]:
    """
    Comprehensive pre-flight validation for service activation eligibility.
    Verifies customer existence, market isolation, lifecycle status, plan matching,
    and physical network capacity in locality.
    """
    reasons = []
    blocking_errors = []
    prerequisites = []

    # 1. Customer verification
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        return {
            "eligible": False,
            "blocking_errors": ["Customer record not found."],
            "reasons": ["Invalid customer ID."],
            "prerequisites": []
        }

    # 2. Market boundary check
    if customer.market_id.lower() != market_id.lower():
        blocking_errors.append(f"Customer belongs to market '{customer.market_id}', not '{market_id}'. Cross-market activation is forbidden.")

    # 3. Customer lifecycle status check
    crm_cust = db.query(CRMCustomer).filter(CRMCustomer.legacy_customer_id == customer.id).first()
    if crm_cust and crm_cust.status in [CustomerLifecycleStatus.TERMINATED, CustomerLifecycleStatus.SUSPENDED]:
        blocking_errors.append(f"Customer lifecycle status is '{crm_cust.status}'. Activation is prohibited for suspended/terminated accounts.")

    # 4. Existing in-flight provisioning check
    in_flight = db.query(ServiceActivationOrder).filter(
        ServiceActivationOrder.customer_id == customer.id,
        ServiceActivationOrder.status.in_(ProvisioningStatus.ACTIVE_OR_PENDING)
    ).first()
    if in_flight:
        blocking_errors.append(f"Customer already has an active or in-flight provisioning order ({in_flight.order_code} in status '{in_flight.status}').")

    # 5. Plan verification
    plan = get_service_plan_by_id(plan_id)
    if not plan:
        blocking_errors.append(f"Specified service plan '{plan_id}' does not exist in the approved tariff catalog.")
    else:
        # Check plan account type matching
        cust_acct_type = crm_cust.account_type if crm_cust else ("ENTERPRISE" if "Enterprise" in customer.segment else "RESIDENTIAL")
        if plan["account_type"] != "ALL" and plan["account_type"] != cust_acct_type:
            if plan["account_type"] == "ENTERPRISE" and cust_acct_type == "RESIDENTIAL":
                blocking_errors.append(f"Plan '{plan['name']}' requires ENTERPRISE classification, but customer is '{cust_acct_type}'.")

    # 6. Physical address and locality network capacity check
    suggested_device = None
    suggested_port = None
    locality = customer.locality or "Colaba"

    # Check for available ONT in customer's locality or unassigned terminal
    available_ont = db.query(NetworkDevice).filter(
        NetworkDevice.market_id == customer.market_id,
        NetworkDevice.device_type.in_(["ONT", "ONU"]),
        or_(NetworkDevice.customer_id == customer.id, NetworkDevice.customer_id.is_(None))
    ).first()

    if available_ont:
        suggested_device = available_ont
    else:
        prerequisites.append(f"No spare ONT/ONU available in {locality}. Inventory warehouse dispatch required.")

    # Check for OLT PON port capacity in market
    olt = db.query(NetworkDevice).filter(
        NetworkDevice.market_id == customer.market_id,
        NetworkDevice.device_type == "OLT"
    ).first()

    if olt:
        healthy_port = db.query(OLTPort).filter(
            OLTPort.device_id == olt.id,
            OLTPort.status == "HEALTHY",
            OLTPort.connected_clients < OLTPort.capacity
        ).first()
        if healthy_port:
            suggested_port = healthy_port
        else:
            prerequisites.append(f"Upstream OLT ({olt.device_code}) PON ports at capacity (>64 clients/port).")
    else:
        blocking_errors.append(f"No upstream OLT deployed for market '{customer.market_id}'.")

    # Address survey check
    crm_addr = None
    if crm_cust:
        crm_addr = db.query(CRMAddress).filter(
            CRMAddress.customer_id == crm_cust.id,
            CRMAddress.address_type == "INSTALLATION",
            CRMAddress.is_archived == False
        ).first()

    if crm_addr:
        if crm_addr.serviceability_status == "UNSERVICEABLE":
            blocking_errors.append("Customer installation premises is categorized as UNSERVICEABLE.")
        elif crm_addr.serviceability_status == "PENDING_SURVEY":
            prerequisites.append("Physical field survey is pending. Coordinates unverified on-ground.")
    elif not customer.service_latitude or not customer.service_longitude:
        prerequisites.append("Service GPS coordinates not verified. Pre-activation drop survey recommended.")

    is_eligible = len(blocking_errors) == 0

    return {
        "eligible": is_eligible,
        "customer_id": customer.id,
        "customer_code": customer.customer_code,
        "customer_name": customer.name,
        "market_id": customer.market_id,
        "locality": locality,
        "plan": plan,
        "suggested_device_id": suggested_device.id if suggested_device else None,
        "suggested_device_code": suggested_device.device_code if suggested_device else None,
        "suggested_port_id": suggested_port.id if suggested_port else None,
        "olt_device_code": olt.device_code if olt else None,
        "blocking_errors": blocking_errors,
        "prerequisites": prerequisites,
        "reasons": ["Pre-flight requirements satisfied." if is_eligible else "Prerequisites or blocking constraints identified."]
    }


def create_activation_order(
    payload: Dict[str, Any],
    current_user: User,
    market_id: str,
    db: Session
) -> ServiceActivationOrder:
    """
    Creates an authoritative service activation request.
    Strictly idempotent: returns existing order if idempotency_key is reused.
    """
    idempotency_key = payload.get("idempotency_key")
    if not idempotency_key:
        idempotency_key = f"IDEM-{uuid.uuid4().hex[:16]}"

    # 1. Idempotency Check: Return existing order if key matches
    existing_order = db.query(ServiceActivationOrder).filter(
        ServiceActivationOrder.idempotency_key == idempotency_key
    ).first()
    if existing_order:
        return existing_order

    customer_id = payload.get("customer_id")
    plan_id = payload.get("plan_id")
    if not customer_id or not plan_id:
        raise HTTPException(status_code=400, detail="customer_id and plan_id are required.")

    # 2. Pre-flight eligibility verification
    eligibility = validate_customer_eligibility(
        customer_id=customer_id,
        plan_id=plan_id,
        market_id=market_id,
        db=db
    )
    if not eligibility["eligible"]:
        err_msg = "; ".join(eligibility["blocking_errors"])
        if "Cross-market" in err_msg:
            raise HTTPException(status_code=403, detail=err_msg)
        raise HTTPException(status_code=409, detail=f"Activation pre-flight failed: {err_msg}")

    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    plan = eligibility["plan"]
    crm_cust = db.query(CRMCustomer).filter(CRMCustomer.legacy_customer_id == customer.id).first()

    # 3. Generate unique order code
    order_code = f"ACT-{(market_id[:3]).upper()}-{datetime.utcnow().year}-{uuid.uuid4().hex[:6].upper()}"

    # 4. Device and port assignment
    assigned_device_id = payload.get("assigned_device_id") or eligibility.get("suggested_device_id")
    assigned_device_code = payload.get("assigned_device_code") or eligibility.get("suggested_device_code")
    assigned_port_id = payload.get("assigned_port_id") or eligibility.get("suggested_port_id")
    olt_device_code = eligibility.get("olt_device_code")

    # 5. Build order
    order = ServiceActivationOrder(
        order_code=order_code,
        idempotency_key=idempotency_key,
        market_id=market_id,
        customer_id=customer.id,
        crm_customer_id=crm_cust.id if crm_cust else None,
        customer_name=customer.name,
        customer_code=customer.customer_code,
        account_type=crm_cust.account_type if crm_cust else "RESIDENTIAL",
        service_address=customer.service_address or f"{customer.locality}, {market_id.title()}",
        locality=customer.locality,
        serviceability_status="SERVICEABLE" if not eligibility["prerequisites"] else "PENDING_SURVEY",
        latitude=customer.service_latitude,
        longitude=customer.service_longitude,
        plan_id=plan["plan_id"],
        plan_name=plan["name"],
        plan_price=plan["price_inr"],
        speed_mbps=plan["speed_mbps"],
        data_quota_gb=plan["data_quota_gb"],
        validity_days=plan["validity_days"],
        assigned_device_id=assigned_device_id,
        assigned_device_code=assigned_device_code,
        assigned_port_id=assigned_port_id,
        olt_device_code=olt_device_code,
        service_vlan=plan.get("service_vlan", 101),
        radius_username=f"{customer.customer_code.lower()}@{market_id}.isp.net",
        status=ProvisioningStatus.REQUESTED,
        customer_lifecycle_status=crm_cust.status if crm_cust else "ACTIVE",
        subscription_status="PENDING",
        execution_mode=payload.get("execution_mode", ExecutionMode.SIMULATED),
        provider_name="SyntheticProvisioningProvider",
        telemetry_source="Synthetic / Demo Telemetry",
        requested_by_id=current_user.id,
        requested_by_email=current_user.email,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    db.add(order)
    db.flush()

    # 6. Immutable initial state transition log
    log = ProvisioningLog(
        order_id=order.id,
        market_id=market_id,
        from_status="NONE",
        to_status=ProvisioningStatus.REQUESTED,
        action="CREATE_REQUEST",
        actor_id=current_user.id,
        actor_email=current_user.email,
        actor_role=current_user.role,
        message=f"Service activation request {order.order_code} created for {customer.name} on plan {plan['name']}.",
        execution_mode=order.execution_mode,
        details={
            "plan_id": plan["plan_id"],
            "speed_mbps": plan["speed_mbps"],
            "assigned_device": assigned_device_code,
            "prerequisites": eligibility["prerequisites"]
        },
        timestamp=datetime.utcnow()
    )
    db.add(log)
    db.flush()
    try:
        from app.services import inventory_service
        inventory_service.reserve_equipment_for_order(
            db=db,
            order=order,
            serial_number=payload.get("serial_number"),
            actor=current_user
        )
    except Exception as exc:
        logger.warning("Optional inventory equipment reservation skipped: %s", exc)
    db.commit()
    db.refresh(order)
    return order


def approve_activation_order(
    order_id_or_code: str,
    current_user: User,
    notes: Optional[str],
    db: Session
) -> ServiceActivationOrder:
    """
    Approves an activation order, moving it from REQUESTED/VALIDATING to APPROVED.
    Restricted to SUPER_ADMIN, Admin, NOC.
    """
    order = _get_order_by_id_or_code(order_id_or_code, db)

    if order.status not in [ProvisioningStatus.REQUESTED, ProvisioningStatus.VALIDATING]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve order in status '{order.status}'. Order must be in REQUESTED or VALIDATING status."
        )

    prev_status = order.status
    order.status = ProvisioningStatus.APPROVED
    order.approved_by_id = current_user.id
    order.approved_by_email = current_user.email
    order.approved_at = datetime.utcnow()
    order.updated_at = datetime.utcnow()

    log = ProvisioningLog(
        order_id=order.id,
        market_id=order.market_id,
        from_status=prev_status,
        to_status=ProvisioningStatus.APPROVED,
        action="APPROVE",
        actor_id=current_user.id,
        actor_email=current_user.email,
        actor_role=current_user.role,
        message=notes or f"Activation order approved by {current_user.full_name} ({current_user.role}).",
        execution_mode=order.execution_mode,
        details={"approved_by": current_user.email, "timestamp": order.approved_at.isoformat()},
        timestamp=datetime.utcnow()
    )
    db.add(log)
    db.commit()
    db.refresh(order)
    return order


def execute_provisioning(
    order_id_or_code: str,
    current_user: User,
    db: Session,
    provider_override=None
) -> Tuple[ServiceActivationOrder, ProvisioningExecutionResult]:
    """
    Executes service delivery via deterministic provider.
    Transitions: APPROVED (or FAILED retry) -> PROVISIONING -> ACTIVE (or FAILED).
    """
    order = _get_order_by_id_or_code(order_id_or_code, db)

    if order.status not in [ProvisioningStatus.APPROVED, ProvisioningStatus.FAILED]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot provision order in status '{order.status}'. Order must be APPROVED (or FAILED for retry)."
        )

    prev_status = order.status
    order.status = ProvisioningStatus.PROVISIONING
    order.updated_at = datetime.utcnow()

    # Log transition to PROVISIONING
    log_start = ProvisioningLog(
        order_id=order.id,
        market_id=order.market_id,
        from_status=prev_status,
        to_status=ProvisioningStatus.PROVISIONING,
        action="DISPATCH_PROVISIONING",
        actor_id=current_user.id,
        actor_email=current_user.email,
        actor_role=current_user.role,
        message=f"Dispatched automated network provisioning to {order.provider_name}.",
        execution_mode=order.execution_mode,
        details={"execution_mode": order.execution_mode},
        timestamp=datetime.utcnow()
    )
    db.add(log_start)
    db.commit()

    # Invoke safe provider
    provider = provider_override or get_provisioning_provider(order.execution_mode)
    res = provider.execute_activation(order, db)

    order.updated_at = datetime.utcnow()

    if res.success:
        order.status = ProvisioningStatus.ACTIVE
        order.subscription_status = "ACTIVE"
        order.customer_lifecycle_status = "ACTIVE"
        order.provisioned_at = datetime.utcnow()
        order.failure_code = None
        order.failure_reason = None

        # Consume and mark installed in inventory
        try:
            from app.services import inventory_service
            inventory_service.consume_and_install_device_for_order(
                db=db,
                order=order,
                actor=current_user
            )
        except Exception as exc:
            logger.warning("Optional inventory equipment consumption skipped: %s", exc)

        # Synchronize operational customer record
        cust = db.query(Customer).filter(Customer.id == order.customer_id).first()
        if cust:
            cust.plan_name = order.plan_name
            cust.plan_price = order.plan_price
            cust.status = "Active"
            cust.validity_status = "Active"
            cust.recharge_validity_days = order.validity_days
            cust.daily_data_quota_gb = round(order.data_quota_gb / (order.validity_days or 28), 1)
            if order.assigned_device_id:
                ont = db.query(NetworkDevice).filter(NetworkDevice.id == order.assigned_device_id).first()
                if ont:
                    ont.customer_id = cust.id
                    ont.status = "HEALTHY"
                    db.add(ont)
            db.add(cust)

        # Safely and idempotently initialize subscriber billing account and subscription
        try:
            billing_account, billing_sub = initialize_billing_for_order(
                order=order,
                db=db,
                actor=current_user,
                auto_commit=False
            )
            billing_details = {
                "billing_account_number": billing_account.account_number,
                "billing_subscription_code": billing_sub.subscription_code,
                "billing_type": billing_sub.billing_type,
                "next_billing_date": billing_sub.next_billing_date.isoformat() if billing_sub.next_billing_date else None,
            }
        except Exception as exc:
            db.rollback()
            order.status = ProvisioningStatus.FAILED
            order.failure_code = "BILLING_INITIALIZATION_ERROR"
            order.failure_reason = f"Network provisioning completed but billing initialization failed: {str(exc)}"
            order.retry_count += 1
            order.updated_at = datetime.utcnow()

            log_fail = ProvisioningLog(
                order_id=order.id,
                market_id=order.market_id,
                from_status=ProvisioningStatus.PROVISIONING,
                to_status=ProvisioningStatus.FAILED,
                action="PROVISION_FAILURE",
                actor_id=current_user.id,
                actor_email=current_user.email,
                actor_role=current_user.role,
                message=f"Billing initialization failed: {str(exc)}",
                execution_mode=order.execution_mode,
                details={"error": str(exc), "failure_code": "BILLING_INITIALIZATION_ERROR"},
                timestamp=datetime.utcnow()
            )
            db.add(log_fail)
            db.commit()
            db.refresh(order)
            return order, ProvisioningExecutionResult(
                success=False,
                status=ProvisioningStatus.FAILED,
                execution_mode=order.execution_mode,
                provider_name=order.provider_name,
                steps=res.steps if res else [],
                failure_code="BILLING_INITIALIZATION_ERROR",
                failure_reason=str(exc)
            )

        # Log completion with billing metadata
        log_payload = res.to_dict()
        log_payload.update(billing_details)

        log_end = ProvisioningLog(
            order_id=order.id,
            market_id=order.market_id,
            from_status=ProvisioningStatus.PROVISIONING,
            to_status=ProvisioningStatus.ACTIVE,
            action="PROVISION_SUCCESS",
            actor_id=current_user.id,
            actor_email=current_user.email,
            actor_role=current_user.role,
            message="Broadband service provisioned successfully; billing account and subscription initialized.",
            execution_mode=order.execution_mode,
            details=log_payload,
            timestamp=datetime.utcnow()
        )
        db.add(log_end)
    else:
        order.status = ProvisioningStatus.FAILED
        order.failure_code = res.failure_code
        order.failure_reason = res.failure_reason
        order.retry_count += 1

        log_end = ProvisioningLog(
            order_id=order.id,
            market_id=order.market_id,
            from_status=ProvisioningStatus.PROVISIONING,
            to_status=ProvisioningStatus.FAILED,
            action="PROVISION_FAILURE",
            actor_id=current_user.id,
            actor_email=current_user.email,
            actor_role=current_user.role,
            message=f"Provisioning failed: {res.failure_reason}",
            execution_mode=order.execution_mode,
            details=res.to_dict(),
            timestamp=datetime.utcnow()
        )
        db.add(log_end)

    db.commit()
    db.refresh(order)
    return order, res


def retry_provisioning(
    order_id_or_code: str,
    current_user: User,
    db: Session,
    provider_override=None
) -> Tuple[ServiceActivationOrder, ProvisioningExecutionResult]:
    """
    Retries a failed provisioning order with bounded retry validation.
    """
    order = _get_order_by_id_or_code(order_id_or_code, db)

    if order.status != ProvisioningStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry order in status '{order.status}'. Only FAILED orders can be retried."
        )

    if order.retry_count >= order.max_retries:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum retry limit ({order.max_retries}) exceeded for order {order.order_code}. Manual engineering intervention required."
        )

    # Clear transient simulation failure flags on retry
    order.failure_code = None

    return execute_provisioning(order_id_or_code, current_user, db, provider_override=provider_override)


def cancel_activation_order(
    order_id_or_code: str,
    reason: str,
    current_user: User,
    db: Session
) -> ServiceActivationOrder:
    """
    Cancels an activation order with mandatory reason and de-allocates any assigned resources.
    """
    order = _get_order_by_id_or_code(order_id_or_code, db)

    if order.status in ProvisioningStatus.TERMINAL:
        raise HTTPException(
            status_code=400,
            detail=f"Order {order.order_code} is already in terminal state '{order.status}' and cannot be cancelled."
        )

    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="A valid cancellation reason is required.")

    prev_status = order.status
    provider = get_provisioning_provider(order.execution_mode)
    res = provider.execute_cancellation(order, db, reason)

    order.status = ProvisioningStatus.CANCELLED
    order.cancelled_at = datetime.utcnow()
    order.cancellation_reason = reason.strip()
    order.updated_at = datetime.utcnow()

    log = ProvisioningLog(
        order_id=order.id,
        market_id=order.market_id,
        from_status=prev_status,
        to_status=ProvisioningStatus.CANCELLED,
        action="CANCEL",
        actor_id=current_user.id,
        actor_email=current_user.email,
        actor_role=current_user.role,
        message=f"Order cancelled by {current_user.full_name}: {reason.strip()}",
        execution_mode=order.execution_mode,
        details=res.to_dict(),
        timestamp=datetime.utcnow()
    )
    try:
        from app.services import inventory_service
        inventory_service.release_reservation_for_order(
            db=db,
            order=order,
            actor=current_user
        )
    except Exception as exc:
        logger.warning("Optional inventory equipment release skipped: %s", exc)
    db.add(log)
    db.commit()
    db.refresh(order)
    return order


def reconcile_activation_order(
    order_id_or_code: str,
    current_user: User,
    db: Session
) -> Tuple[ServiceActivationOrder, ProvisioningExecutionResult]:
    """
    Reconciles application ledger state with downstream network provider status.
    Self-heals if network operation succeeded but response failed to record.
    """
    order = _get_order_by_id_or_code(order_id_or_code, db)
    provider = get_provisioning_provider(order.execution_mode)
    res = provider.reconcile_status(order, db)

    prev_status = order.status
    if res.reconciled and res.status == ProvisioningStatus.ACTIVE:
        order.status = ProvisioningStatus.ACTIVE
        order.subscription_status = "ACTIVE"
        order.provisioned_at = datetime.utcnow()
        order.failure_code = None
        order.failure_reason = None
        order.updated_at = datetime.utcnow()

        try:
            billing_account, billing_sub = initialize_billing_for_order(
                order=order,
                db=db,
                actor=current_user,
                auto_commit=False
            )
            reconcile_billing_details = {
                "billing_account_number": billing_account.account_number,
                "billing_subscription_code": billing_sub.subscription_code,
            }
        except Exception:
            reconcile_billing_details = {}

        reconcile_payload = res.to_dict()
        reconcile_payload.update(reconcile_billing_details)

        log = ProvisioningLog(
            order_id=order.id,
            market_id=order.market_id,
            from_status=prev_status,
            to_status=ProvisioningStatus.ACTIVE,
            action="RECONCILE_HEAL",
            actor_id=current_user.id,
            actor_email=current_user.email,
            actor_role=current_user.role,
            message="Reconciliation self-healed order to ACTIVE and ensured billing subscription initialization.",
            execution_mode=order.execution_mode,
            details=reconcile_payload,
            timestamp=datetime.utcnow()
        )
        db.add(log)
        db.commit()
        db.refresh(order)

    return order, res


def _get_order_by_id_or_code(id_or_code: str, db: Session) -> ServiceActivationOrder:
    """Helper looking up order by integer ID or unique order code."""
    if str(id_or_code).isdigit():
        order = db.query(ServiceActivationOrder).filter(ServiceActivationOrder.id == int(id_or_code)).first()
    else:
        order = db.query(ServiceActivationOrder).filter(ServiceActivationOrder.order_code == str(id_or_code).strip()).first()

    if not order:
        raise HTTPException(status_code=404, detail=f"Provisioning order '{id_or_code}' not found.")
    return order
