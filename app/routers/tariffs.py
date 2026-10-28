"""
app/routers/tariffs.py

SentinelOS Tariff Plan & Dynamic Pricing Structure REST API Router.

Endpoints:
  GET    /api/tariffs                   -> List tariff plans (filtered by plan, market, status)
  POST   /api/tariffs                   -> Create new tariff plan (Admin / Super Admin / Billing)
  GET    /api/tariffs/{tariff_id}       -> Get tariff plan details
  PUT    /api/tariffs/{tariff_id}       -> Update tariff plan (Admin / Super Admin / Billing)
  POST   /api/tariffs/{tariff_id}/deactivate -> Deactivate tariff plan
  POST   /api/tariffs/calculate         -> Calculate financial charges breakdown
  POST   /api/tariffs/seed              -> Seed canonical tariffs from service plans
"""

from typing import List, Optional, Dict, Any
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user, require_roles
from app.services.rbac_service import is_super_admin
from app.markets import get_current_market
from app.schemas_tariff import (
    TariffPlanCreate, TariffPlanUpdate, TariffPlanResponse,
    TariffCalculationRequest, TariffCalculationResponse
)
from app.services import tariff_service

router = APIRouter(prefix="/tariffs", tags=["Tariff Plans & Pricing"])


@router.get("", response_model=List[TariffPlanResponse])
def get_tariffs(
    service_plan_code: Optional[str] = Query(None, description="Filter by service plan code"),
    billing_type: Optional[str] = Query(None, description="PREPAID or POSTPAID"),
    billing_frequency: Optional[str] = Query(None, description="MONTHLY, QUARTERLY, etc."),
    status: Optional[str] = Query(None, description="ACTIVE, INACTIVE, ARCHIVED"),
    market: Optional[str] = Query(None, description="Market filter (ALL, mumbai, kolkata)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    market_filter = market or current_market
    return tariff_service.list_tariffs(
        db,
        service_plan_code=service_plan_code,
        market_id=market_filter,
        billing_type=billing_type,
        billing_frequency=billing_frequency,
        status=status
    )


@router.post("", response_model=TariffPlanResponse, status_code=status.HTTP_201_CREATED)
def create_tariff(
    tariff_in: TariffPlanCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "Billing"])),
    current_market: str = Depends(get_current_market)
):
    target_market = getattr(tariff_in, 'market_id', None)
    if not is_super_admin(current_user.role):
        if not target_market:
            tariff_in.market_id = current_market
            target_market = current_market
        elif target_market.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can create global-market (ALL) tariff plans."
            )
        elif target_market.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot create tariff plans for market '{target_market}'."
            )
    return tariff_service.create_tariff(db, tariff_in, actor=current_user)


@router.get("/{tariff_id}", response_model=TariffPlanResponse)
def get_tariff(
    tariff_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return tariff_service.get_tariff(db, tariff_id)


@router.put("/{tariff_id}", response_model=TariffPlanResponse)
def update_tariff(
    tariff_id: str,
    tariff_in: TariffPlanUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "Billing"])),
    current_market: str = Depends(get_current_market)
):
    tariff = tariff_service.get_tariff(db, tariff_id)
    if not is_super_admin(current_user.role):
        if tariff.market_id.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can modify global-market (ALL) tariff plans."
            )
        if tariff.market_id.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot modify tariff plan in market '{tariff.market_id}'."
            )
        if tariff_in.market_id is not None and tariff_in.market_id.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator cannot reassign tariff plan to market '{tariff_in.market_id}'."
            )
    return tariff_service.update_tariff(db, tariff_id, tariff_in, actor=current_user)


@router.post("/{tariff_id}/deactivate", response_model=TariffPlanResponse)
def deactivate_tariff(
    tariff_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"])),
    current_market: str = Depends(get_current_market)
):
    tariff = tariff_service.get_tariff(db, tariff_id)
    if not is_super_admin(current_user.role):
        if tariff.market_id.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can deactivate global-market (ALL) tariff plans."
            )
        if tariff.market_id.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot deactivate tariff plan in market '{tariff.market_id}'."
            )
    return tariff_service.deactivate_tariff(db, tariff_id, actor=current_user)


@router.post("/calculate", response_model=TariffCalculationResponse)
def calculate_charges(
    calc_in: TariffCalculationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return tariff_service.calculate_tariff_breakdown(
        db=db,
        tariff_code=calc_in.tariff_code,
        usage_gb=calc_in.usage_gb or 0.0,
        apply_setup_fee=calc_in.apply_setup_fee or False,
        voucher_code=calc_in.voucher_code
    )


@router.post("/seed", status_code=status.HTTP_200_OK)
def seed_tariffs(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"]))
):
    count = tariff_service.seed_canonical_tariffs(db)
    return {"success": True, "seeded_tariffs_count": count}
