"""
app/routers/provisioning.py

SentinelOS Service Provisioning & Automated Service Delivery REST API Router.

Endpoints:
  GET    /api/provisioning/plans                           -> Enumerate canonical service plans
  GET    /api/provisioning/customer/{cust_id}/eligibility  -> Pre-flight customer eligibility validation
  GET    /api/provisioning/orders                          -> List activation orders with server-side pagination
  POST   /api/provisioning/orders                          -> Create activation request (idempotent)
  GET    /api/provisioning/orders/{id_or_code}             -> Order detail with transition history
  POST   /api/provisioning/orders/{id_or_code}/approve     -> Approve order (Admin/NOC only)
  POST   /api/provisioning/orders/{id_or_code}/provision   -> Execute automated delivery (Admin/NOC only)
  POST   /api/provisioning/orders/{id_or_code}/retry       -> Bounded retry for failed activation
  POST   /api/provisioning/orders/{id_or_code}/cancel      -> Cancel activation request with mandatory reason
  POST   /api/provisioning/orders/{id_or_code}/reconcile   -> Reconcile application state with hardware telemetry

RBAC & Tenant Security:
- Mutation operations restricted to Admin, NOC, Care (approval and execution restricted to Admin and NOC).
- Viewers and Customer roles denied mutation operations (HTTP 403 Forbidden).
- Strict multi-tenant market boundary enforcement via X-Market-Id header.
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc

from app.database import get_db
from app.models import User
from app.auth import get_current_user, require_roles
from app.markets import get_current_market
from app.models_provisioning import ServiceActivationOrder, ProvisioningLog, ProvisioningStatus, ExecutionMode
from app.schemas import (
    ServicePlanItem,
    ProvisioningOrderCreateRequest,
    ProvisioningOrderApproveRequest,
    ProvisioningOrderCancelRequest,
    ServiceActivationOrderResponse,
    CustomerEligibilityResponse
)
from app.services.provisioning_catalog import get_all_service_plans, get_service_plan_by_id
from app.services.provisioning_service import (
    validate_customer_eligibility,
    create_activation_order,
    approve_activation_order,
    execute_provisioning,
    retry_provisioning,
    cancel_activation_order,
    reconcile_activation_order,
    _get_order_by_id_or_code
)

router = APIRouter(prefix="/provisioning", tags=["Service Provisioning & Automated Delivery"])

# Role groups
OPERATIONAL_ROLES = ["SUPER_ADMIN", "Admin", "NOC", "Care"]
EXECUTION_ROLES = ["SUPER_ADMIN", "Admin", "NOC"]
READ_ROLES = ["SUPER_ADMIN", "Admin", "NOC", "Care", "Revenue", "Executive", "Viewer"]


@router.get("/plans", response_model=List[ServicePlanItem])
def list_service_plans(
    account_type: Optional[str] = Query(None, description="RESIDENTIAL, ENTERPRISE, SME, ALL"),
    billing_type: Optional[str] = Query(None, description="PREPAID, POSTPAID"),
    current_user: User = Depends(require_roles(READ_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Returns the canonical catalog of approved tariff and broadband plans.
    """
    plans = get_all_service_plans(account_type=account_type, billing_type=billing_type)
    return plans


@router.get("/customer/{customer_id}/eligibility", response_model=CustomerEligibilityResponse)
def get_customer_provisioning_eligibility(
    customer_id: int,
    plan_id: str = Query(..., description="Target service plan ID from catalog"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Executes pre-flight verification on customer eligibility, market boundary,
    lifecycle status, and network inventory in locality.
    """
    result = validate_customer_eligibility(
        customer_id=customer_id,
        plan_id=plan_id,
        market_id=market,
        db=db
    )
    return result


@router.get("/orders", response_model=List[ServiceActivationOrderResponse])
def list_provisioning_orders(
    response: Response,
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by ProvisioningStatus"),
    customer_id: Optional[int] = Query(None, description="Filter by customer ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Page size"),
    sort_order: str = Query("desc", regex="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(READ_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Lists provisioning orders within caller's market with server-side pagination headers.
    """
    query = db.query(ServiceActivationOrder).filter(
        ServiceActivationOrder.market_id.ilike(market)
    )

    if status_filter:
        query = query.filter(ServiceActivationOrder.status == status_filter.upper())
    if customer_id:
        query = query.filter(ServiceActivationOrder.customer_id == customer_id)

    total_count = query.count()
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

    if sort_order == "desc":
        query = query.order_by(desc(ServiceActivationOrder.created_at))
    else:
        query = query.order_by(asc(ServiceActivationOrder.created_at))

    orders = query.offset((page - 1) * page_size).limit(page_size).all()

    # Server-side pagination headers
    response.headers["X-Total-Count"] = str(total_count)
    response.headers["X-Page"] = str(page)
    response.headers["X-Page-Size"] = str(page_size)
    response.headers["X-Total-Pages"] = str(total_pages)

    return orders


@router.post("/orders", response_model=ServiceActivationOrderResponse, status_code=status.HTTP_201_CREATED)
def create_service_activation(
    payload: ProvisioningOrderCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Creates a new service activation request with idempotency protection.
    """
    order = create_activation_order(
        payload=payload.model_dump(),
        current_user=current_user,
        market_id=market,
        db=db
    )
    return order


@router.get("/orders/{id_or_code}", response_model=ServiceActivationOrderResponse)
def get_provisioning_order_detail(
    id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(READ_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Fetches full detail of a provisioning order with immutable transition logs.
    Enforces market isolation.
    """
    order = _get_order_by_id_or_code(id_or_code, db)
    if order.market_id.lower() != market.lower():
        raise HTTPException(
            status_code=403,
            detail=f"Order belongs to market '{order.market_id}'. Cross-market access denied."
        )
    return order


@router.post("/orders/{id_or_code}/approve", response_model=ServiceActivationOrderResponse)
def approve_provisioning_order(
    id_or_code: str,
    payload: Optional[ProvisioningOrderApproveRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(EXECUTION_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Approves an activation request.
    Restricted to SUPER_ADMIN, Admin, NOC.
    """
    order = _get_order_by_id_or_code(id_or_code, db)
    if order.market_id.lower() != market.lower():
        raise HTTPException(status_code=403, detail="Cross-market approval denied.")

    notes = payload.notes if payload else None
    approved = approve_activation_order(
        order_id_or_code=id_or_code,
        current_user=current_user,
        notes=notes,
        db=db
    )
    return approved


@router.post("/orders/{id_or_code}/provision", response_model=ServiceActivationOrderResponse)
def trigger_automated_provisioning(
    id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(EXECUTION_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Triggers deterministic automated network service provisioning on OLT/BNG/ONT.
    Restricted to SUPER_ADMIN, Admin, NOC.
    """
    order = _get_order_by_id_or_code(id_or_code, db)
    if order.market_id.lower() != market.lower():
        raise HTTPException(status_code=403, detail="Cross-market execution denied.")

    order, res = execute_provisioning(
        order_id_or_code=id_or_code,
        current_user=current_user,
        db=db
    )
    return order


@router.post("/orders/{id_or_code}/retry", response_model=ServiceActivationOrderResponse)
def retry_failed_provisioning(
    id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(EXECUTION_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Safely retries a failed activation order within bounded retry limits.
    Restricted to SUPER_ADMIN, Admin, NOC.
    """
    order = _get_order_by_id_or_code(id_or_code, db)
    if order.market_id.lower() != market.lower():
        raise HTTPException(status_code=403, detail="Cross-market retry denied.")

    order, res = retry_provisioning(
        order_id_or_code=id_or_code,
        current_user=current_user,
        db=db
    )
    return order


@router.post("/orders/{id_or_code}/cancel", response_model=ServiceActivationOrderResponse)
def cancel_provisioning_order(
    id_or_code: str,
    payload: ProvisioningOrderCancelRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Cancels an activation order with mandatory reason and de-allocates resources.
    Restricted to SUPER_ADMIN, Admin, NOC, Care.
    """
    order = _get_order_by_id_or_code(id_or_code, db)
    if order.market_id.lower() != market.lower():
        raise HTTPException(status_code=403, detail="Cross-market cancellation denied.")

    cancelled = cancel_activation_order(
        order_id_or_code=id_or_code,
        reason=payload.reason,
        current_user=current_user,
        db=db
    )
    return cancelled


@router.post("/orders/{id_or_code}/reconcile", response_model=ServiceActivationOrderResponse)
def reconcile_provisioning_order(
    id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(EXECUTION_ROLES)),
    market: str = Depends(get_current_market)
):
    """
    Reconciles application ledger state with downstream network provider telemetry.
    Self-heals if network operation succeeded but response failed to record.
    """
    order = _get_order_by_id_or_code(id_or_code, db)
    if order.market_id.lower() != market.lower():
        raise HTTPException(status_code=403, detail="Cross-market reconciliation denied.")

    order, res = reconcile_activation_order(
        order_id_or_code=id_or_code,
        current_user=current_user,
        db=db
    )
    return order
