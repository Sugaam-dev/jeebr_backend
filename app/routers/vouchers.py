"""
app/routers/vouchers.py

SentinelOS Voucher Campaign & Coupon Code Management REST API Router.

Endpoints:
  GET    /api/vouchers                   -> List voucher definitions (filtered by market, status, type)
  POST   /api/vouchers                   -> Create voucher definition (Admin / Super Admin / Billing)
  POST   /api/vouchers/bulk              -> Bulk generate vouchers (Admin / Super Admin)
  GET    /api/vouchers/{voucher_id}      -> Get voucher details
  POST   /api/vouchers/{voucher_id}/pause -> Pause voucher
  POST   /api/vouchers/{voucher_id}/activate -> Activate voucher
  POST   /api/vouchers/{voucher_id}/revoke   -> Revoke voucher
  POST   /api/vouchers/validate          -> Validate voucher eligibility & calculate discount
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user, require_roles
from app.services.rbac_service import is_super_admin
from app.markets import get_current_market
from app.schemas_voucher import (
    VoucherCreate, VoucherBulkCreate, VoucherUpdate, VoucherResponse,
    VoucherValidationRequest, VoucherValidationResponse
)
from app.services import voucher_management_service

router = APIRouter(prefix="/vouchers", tags=["Vouchers & Promotional Campaigns"])


@router.get("", response_model=List[VoucherResponse])
def get_vouchers(
    market: Optional[str] = Query(None, description="Filter by market (ALL, mumbai, kolkata)"),
    status: Optional[str] = Query(None, description="ACTIVE, PAUSED, EXPIRED, EXHAUSTED, REVOKED"),
    voucher_type: Optional[str] = Query(None, description="BILL_DISCOUNT, PERCENT_DISCOUNT, etc."),
    search: Optional[str] = Query(None, description="Search code or campaign name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    market_filter = market or current_market
    return voucher_management_service.list_vouchers(
        db,
        market_id=market_filter,
        status=status,
        voucher_type=voucher_type,
        search=search,
        skip=skip,
        limit=limit
    )


@router.post("", response_model=VoucherResponse, status_code=status.HTTP_201_CREATED)
def create_voucher(
    voucher_in: VoucherCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "Billing"])),
    current_market: str = Depends(get_current_market)
):
    target_market = getattr(voucher_in, 'market_id', 'ALL') or 'ALL'
    if not is_super_admin(current_user.role):
        if target_market.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can create global-market (ALL) vouchers."
            )
        if target_market.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot create vouchers for market '{target_market}'."
            )
    return voucher_management_service.create_voucher(db, voucher_in, actor=current_user)


@router.post("/bulk", response_model=List[VoucherResponse], status_code=status.HTTP_201_CREATED)
def bulk_create_vouchers(
    bulk_in: VoucherBulkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"])),
    current_market: str = Depends(get_current_market)
):
    target_market = getattr(bulk_in, 'market_id', 'ALL') or 'ALL'
    if not is_super_admin(current_user.role):
        if target_market.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can bulk create global-market (ALL) vouchers."
            )
        if target_market.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot bulk create vouchers for market '{target_market}'."
            )
    return voucher_management_service.bulk_create_vouchers(db, bulk_in, actor=current_user)


@router.get("/{voucher_id}", response_model=VoucherResponse)
def get_voucher(
    voucher_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return voucher_management_service.get_voucher(db, voucher_id)


@router.post("/{voucher_id}/pause", response_model=VoucherResponse)
def pause_voucher(
    voucher_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "Billing"])),
    current_market: str = Depends(get_current_market)
):
    voucher = voucher_management_service.get_voucher(db, voucher_id)
    if not is_super_admin(current_user.role):
        if voucher.market_id.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can pause global-market (ALL) vouchers."
            )
        if voucher.market_id.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot pause voucher in market '{voucher.market_id}'."
            )
    return voucher_management_service.pause_voucher(db, voucher_id, actor=current_user)


@router.post("/{voucher_id}/activate", response_model=VoucherResponse)
def activate_voucher(
    voucher_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin", "Billing"])),
    current_market: str = Depends(get_current_market)
):
    voucher = voucher_management_service.get_voucher(db, voucher_id)
    if not is_super_admin(current_user.role):
        if voucher.market_id.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can activate global-market (ALL) vouchers."
            )
        if voucher.market_id.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot activate voucher in market '{voucher.market_id}'."
            )
    return voucher_management_service.activate_voucher(db, voucher_id, actor=current_user)


@router.post("/{voucher_id}/revoke", response_model=VoucherResponse)
def revoke_voucher(
    voucher_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin", "Super Admin"])),
    current_market: str = Depends(get_current_market)
):
    voucher = voucher_management_service.get_voucher(db, voucher_id)
    if not is_super_admin(current_user.role):
        if voucher.market_id.upper() == "ALL":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Super Admin operators can revoke global-market (ALL) vouchers."
            )
        if voucher.market_id.lower() != current_market.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Operator market '{current_market}' cannot revoke voucher in market '{voucher.market_id}'."
            )
    return voucher_management_service.revoke_voucher(db, voucher_id, actor=current_user)


@router.post("/validate", response_model=VoucherValidationResponse)
def validate_voucher(
    val_in: VoucherValidationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    current_market: str = Depends(get_current_market)
):
    is_valid, calc_discount, v_type, reason = voucher_management_service.validate_voucher_for_redemption(
        db=db,
        voucher_code=val_in.voucher_code,
        customer_id=val_in.customer_id,
        invoice_amount=val_in.invoice_amount,
        plan_id=val_in.plan_id,
        customer_market=current_market
    )
    return VoucherValidationResponse(
        is_valid=is_valid,
        voucher_code=val_in.voucher_code,
        voucher_type=v_type,
        calculated_discount=calc_discount,
        reason=reason
    )
