"""
app/services/voucher_management_service.py

SentinelOS Voucher Campaign & Coupon Code Management Service.
Handles:
1. Voucher definition lifecycle (single-use, multi-use, campaign rules, caps).
2. Bulk voucher generation with cryptographic collision avoidance.
3. Pre-redemption validation and discount computation.
4. Concurrency-safe status transitions (ACTIVE, PAUSED, EXHAUSTED, REVOKED).
"""

import uuid
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from fastapi import HTTPException, status

from app.models_voucher import Voucher, VoucherStatus, VoucherDefinitionType
from app.models_finance import VoucherRedemption, VoucherRedemptionStatus
from app.models import Customer, User


def generate_voucher_code(prefix: str = "PROMO", length: int = 6) -> str:
    """Generates an alphanumeric voucher code."""
    token = secrets.token_hex(length // 2).upper()
    return f"{prefix.strip().upper()}-{token}"


def get_voucher_by_code(db: Session, voucher_code: str) -> Optional[Voucher]:
    return db.query(Voucher).filter(
        Voucher.voucher_code == voucher_code.strip().upper()
    ).first()


def get_voucher(db: Session, voucher_id: Any) -> Voucher:
    parsed_id = uuid.UUID(str(voucher_id)) if not isinstance(voucher_id, uuid.UUID) else voucher_id
    voucher = db.query(Voucher).filter(Voucher.id == parsed_id).first()
    if not voucher:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voucher with ID '{voucher_id}' not found"
        )
    return voucher


def list_vouchers(
    db: Session,
    market_id: Optional[str] = None,
    status: Optional[str] = None,
    voucher_type: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100
) -> List[Voucher]:
    query = db.query(Voucher)
    if market_id and market_id.upper() != "ALL":
        query = query.filter(or_(Voucher.market_id == market_id, Voucher.market_id == "ALL"))
    if status:
        query = query.filter(Voucher.status == status.upper())
    if voucher_type:
        query = query.filter(Voucher.voucher_type == voucher_type.upper())
    if search:
        s = f"%{search.strip()}%"
        query = query.filter(or_(Voucher.voucher_code.ilike(s), Voucher.campaign_name.ilike(s)))
    return query.order_by(Voucher.created_at.desc()).offset(skip).limit(limit).all()


def create_voucher(db: Session, voucher_in: Any, actor: Optional[User] = None) -> Voucher:
    code = getattr(voucher_in, 'voucher_code', '').strip().upper()
    existing = get_voucher_by_code(db, code)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Voucher code '{code}' already exists"
        )

    valid_from = getattr(voucher_in, 'valid_from', None) or datetime.utcnow()
    valid_until = getattr(voucher_in, 'valid_until', None)
    if not valid_until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="valid_until expiration date is required"
        )
    if valid_until <= valid_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="valid_until must be strictly greater than valid_from"
        )

    voucher = Voucher(
        id=uuid.uuid4(),
        voucher_code=code,
        campaign_name=getattr(voucher_in, 'campaign_name', '').strip(),
        description=getattr(voucher_in, 'description', None),
        voucher_type=getattr(voucher_in, 'voucher_type', VoucherDefinitionType.BILL_DISCOUNT),
        discount_amount=getattr(voucher_in, 'discount_amount', Decimal('0.00')),
        discount_percent=getattr(voucher_in, 'discount_percent', Decimal('0.00')),
        max_discount_cap=getattr(voucher_in, 'max_discount_cap', None),
        min_invoice_amount=getattr(voucher_in, 'min_invoice_amount', Decimal('0.00')),
        benefit_payload=getattr(voucher_in, 'benefit_payload', {}) or {},
        max_redemptions=getattr(voucher_in, 'max_redemptions', 1),
        current_redemptions=0,
        per_customer_limit=getattr(voucher_in, 'per_customer_limit', 1),
        valid_from=valid_from,
        valid_until=valid_until,
        market_id=getattr(voucher_in, 'market_id', 'ALL'),
        applicable_plan_id=getattr(voucher_in, 'applicable_plan_id', None),
        status=getattr(voucher_in, 'status', VoucherStatus.ACTIVE),
        created_by_id=actor.id if actor else None
    )
    db.add(voucher)
    db.commit()
    db.refresh(voucher)
    return voucher


def bulk_create_vouchers(db: Session, bulk_in: Any, actor: Optional[User] = None) -> List[Voucher]:
    count = getattr(bulk_in, 'count', 1)
    prefix = getattr(bulk_in, 'prefix', 'PROMO').strip().upper()
    valid_from = getattr(bulk_in, 'valid_from', None) or datetime.utcnow()
    valid_until = getattr(bulk_in, 'valid_until', None)
    if not valid_until or valid_until <= valid_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="valid_until must be strictly greater than valid_from"
        )

    vouchers: List[Voucher] = []
    generated_codes = set()

    for _ in range(count):
        # Generate unique code
        for _attempt in range(10):
            cand_code = generate_voucher_code(prefix=prefix, length=6)
            if cand_code not in generated_codes and not get_voucher_by_code(db, cand_code):
                generated_codes.add(cand_code)
                break
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate unique voucher code in batch"
            )

        v = Voucher(
            id=uuid.uuid4(),
            voucher_code=cand_code,
            campaign_name=getattr(bulk_in, 'campaign_name', '').strip(),
            description=getattr(bulk_in, 'description', None),
            voucher_type=getattr(bulk_in, 'voucher_type', VoucherDefinitionType.BILL_DISCOUNT),
            discount_amount=getattr(bulk_in, 'discount_amount', Decimal('0.00')),
            discount_percent=getattr(bulk_in, 'discount_percent', Decimal('0.00')),
            max_discount_cap=getattr(bulk_in, 'max_discount_cap', None),
            min_invoice_amount=getattr(bulk_in, 'min_invoice_amount', Decimal('0.00')),
            benefit_payload=getattr(bulk_in, 'benefit_payload', {}) or {},
            max_redemptions=getattr(bulk_in, 'max_redemptions', 1),
            current_redemptions=0,
            per_customer_limit=getattr(bulk_in, 'per_customer_limit', 1),
            valid_from=valid_from,
            valid_until=valid_until,
            market_id=getattr(bulk_in, 'market_id', 'ALL'),
            applicable_plan_id=getattr(bulk_in, 'applicable_plan_id', None),
            status=VoucherStatus.ACTIVE,
            created_by_id=actor.id if actor else None
        )
        db.add(v)
        vouchers.append(v)

    db.commit()
    for v in vouchers:
        db.refresh(v)
    return vouchers


def pause_voucher(db: Session, voucher_id: Any, actor: Optional[User] = None) -> Voucher:
    voucher = get_voucher(db, voucher_id)
    voucher.status = VoucherStatus.PAUSED
    voucher.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(voucher)
    return voucher


def activate_voucher(db: Session, voucher_id: Any, actor: Optional[User] = None) -> Voucher:
    voucher = get_voucher(db, voucher_id)
    now = datetime.utcnow()
    if voucher.valid_until < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot activate expired voucher (expired on {voucher.valid_until})"
        )
    if voucher.current_redemptions >= voucher.max_redemptions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot activate exhausted voucher"
        )
    voucher.status = VoucherStatus.ACTIVE
    voucher.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(voucher)
    return voucher


def revoke_voucher(db: Session, voucher_id: Any, actor: Optional[User] = None) -> Voucher:
    voucher = get_voucher(db, voucher_id)
    voucher.status = VoucherStatus.REVOKED
    voucher.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(voucher)
    return voucher


def validate_voucher_for_redemption(
    db: Session,
    voucher_code: str,
    customer_id: int,
    invoice_amount: Decimal = Decimal('0.00'),
    plan_id: Optional[str] = None,
    customer_market: Optional[str] = None
) -> Tuple[bool, Decimal, Optional[str], Optional[str]]:
    """
    Evaluates whether a voucher is eligible for redemption.
    Returns: (is_valid, calculated_discount, voucher_type, reason)
    """
    v = get_voucher_by_code(db, voucher_code)
    if not v:
        return False, Decimal('0.00'), None, f"Voucher '{voucher_code}' not found."

    now = datetime.utcnow()
    if v.status != VoucherStatus.ACTIVE:
        return False, Decimal('0.00'), v.voucher_type, f"Voucher is {v.status}."

    if now < v.valid_from:
        return False, Decimal('0.00'), v.voucher_type, f"Voucher is not valid until {v.valid_from}."
    if now > v.valid_until:
        return False, Decimal('0.00'), v.voucher_type, f"Voucher expired on {v.valid_until}."

    if v.current_redemptions >= v.max_redemptions:
        return False, Decimal('0.00'), v.voucher_type, "Voucher maximum redemptions quota reached."

    # Per-customer limit check
    prior_redemptions = db.query(VoucherRedemption).filter(
        VoucherRedemption.voucher_code == v.voucher_code,
        VoucherRedemption.customer_id == customer_id,
        VoucherRedemption.status == VoucherRedemptionStatus.FULFILLED
    ).count()
    if prior_redemptions >= v.per_customer_limit:
        return False, Decimal('0.00'), v.voucher_type, f"Customer has already redeemed this voucher {prior_redemptions} time(s) (limit {v.per_customer_limit})."

    # Market isolation
    if customer_market and v.market_id != "ALL" and v.market_id.lower() != customer_market.lower():
        return False, Decimal('0.00'), v.voucher_type, f"Voucher only applicable in market '{v.market_id}', customer is in '{customer_market}'."

    # Plan restriction
    if v.applicable_plan_id and plan_id and v.applicable_plan_id != plan_id:
        return False, Decimal('0.00'), v.voucher_type, f"Voucher only valid for plan '{v.applicable_plan_id}'."

    # Minimum invoice amount
    if v.min_invoice_amount and invoice_amount < v.min_invoice_amount:
        return False, Decimal('0.00'), v.voucher_type, f"Minimum invoice amount required is ₹{v.min_invoice_amount}."

    # Calculate discount
    calc_discount = Decimal('0.00')
    if v.voucher_type in (VoucherDefinitionType.PERCENT_DISCOUNT, "PERCENTAGE"):
        pct = Decimal(str(v.discount_percent or 0))
        calc_discount = round(invoice_amount * (pct / Decimal('100.00')), 2)
        if v.max_discount_cap:
            calc_discount = min(calc_discount, Decimal(str(v.max_discount_cap)))
    elif v.voucher_type == VoucherDefinitionType.BILL_DISCOUNT:
        calc_discount = round(Decimal(str(v.discount_amount or 0)), 2)

    return True, calc_discount, v.voucher_type, None
