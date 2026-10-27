"""
app/services/tariff_service.py

SentinelOS Tariff Plan & Dynamic Pricing Structure Management Service.
Handles:
1. Tariff plan lifecycle (CRUD, versioning, status transitions).
2. Effective date range overlap validation per service plan and market.
3. Seeding of authoritative tariff rules linked to canonical service plans.
4. Financial calculation breakdown (recurring charges, setup fees, usage, GST 18%, discounts).
5. Market boundary enforcement.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func
from fastapi import HTTPException, status

from app.models_tariff import TariffPlan, TariffStatus, BillingTiming
from app.models_service_plan import ServicePlan
from app.models import User
from app.services.catalog_service import seed_service_plans_from_canonical, get_service_plan_by_code


def validate_effective_date_overlap(
    db: Session,
    service_plan_code: str,
    market_id: str,
    effective_from: datetime,
    effective_to: Optional[datetime] = None,
    exclude_tariff_id: Optional[uuid.UUID] = None
) -> None:
    """
    Enforces that active tariffs for the same service_plan_code and market_id
    do not have overlapping effective date intervals.
    """
    query = db.query(TariffPlan).filter(
        TariffPlan.service_plan_code == service_plan_code,
        TariffPlan.market_id == market_id,
        TariffPlan.status == TariffStatus.ACTIVE
    )
    if exclude_tariff_id:
        query = query.filter(TariffPlan.id != exclude_tariff_id)

    existing_active = query.all()
    for existing in existing_active:
        ex_from = existing.effective_from
        ex_to = existing.effective_to

        # Check overlap
        overlap = False
        if effective_to is None:
            # New tariff has no end date
            if ex_to is None:
                overlap = True
            elif ex_to >= effective_from:
                overlap = True
        else:
            # New tariff has an end date
            if ex_to is None:
                if ex_from <= effective_to:
                    overlap = True
            else:
                if ex_from <= effective_to and effective_from <= ex_to:
                    overlap = True

        if overlap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Effective date conflict: Tariff '{existing.tariff_code}' is currently active "
                    f"for plan '{service_plan_code}' in market '{market_id}' from {ex_from.date()} "
                    f"to {ex_to.date() if ex_to else 'indefinite'}."
                )
            )


def list_tariffs(
    db: Session,
    service_plan_code: Optional[str] = None,
    market_id: Optional[str] = None,
    billing_type: Optional[str] = None,
    billing_frequency: Optional[str] = None,
    status: Optional[str] = None
) -> List[TariffPlan]:
    query = db.query(TariffPlan)
    if service_plan_code:
        query = query.filter(TariffPlan.service_plan_code == service_plan_code)
    if market_id and market_id.upper() != "ALL":
        query = query.filter(or_(TariffPlan.market_id == market_id, TariffPlan.market_id == "ALL"))
    if billing_type:
        query = query.filter(TariffPlan.billing_type == billing_type.upper())
    if billing_frequency:
        query = query.filter(TariffPlan.billing_frequency == billing_frequency.upper())
    if status:
        query = query.filter(TariffPlan.status == status.upper())
    return query.order_by(TariffPlan.created_at.desc()).all()


def get_tariff(db: Session, tariff_id: Any) -> TariffPlan:
    parsed_id = uuid.UUID(str(tariff_id)) if not isinstance(tariff_id, uuid.UUID) else tariff_id
    tariff = db.query(TariffPlan).filter(TariffPlan.id == parsed_id).first()
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tariff plan with ID '{tariff_id}' not found"
        )
    return tariff


def get_tariff_by_code(db: Session, tariff_code: str) -> Optional[TariffPlan]:
    return db.query(TariffPlan).filter(TariffPlan.tariff_code == tariff_code.strip()).first()


def create_tariff(db: Session, tariff_in: Any, actor: Optional[User] = None) -> TariffPlan:
    # 1. Check uniqueness of tariff_code
    code = getattr(tariff_in, 'tariff_code', '').strip()
    existing = get_tariff_by_code(db, code)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tariff code '{code}' already exists"
        )

    # 2. Check canonical ServicePlan existence
    plan_code = getattr(tariff_in, 'service_plan_code', '')
    service_plan = get_service_plan_by_code(db, plan_code)
    if not service_plan:
        # Try seeding canonical service plans
        seed_service_plans_from_canonical(db)
        service_plan = get_service_plan_by_code(db, plan_code)
        if not service_plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Referenced service plan '{plan_code}' does not exist"
            )

    eff_from = getattr(tariff_in, 'effective_from', None) or datetime.utcnow()
    eff_to = getattr(tariff_in, 'effective_to', None)
    target_status = getattr(tariff_in, 'status', TariffStatus.ACTIVE)
    market_id = getattr(tariff_in, 'market_id', 'ALL')

    # 3. Date overlap validation
    if target_status == TariffStatus.ACTIVE:
        validate_effective_date_overlap(
            db,
            service_plan_code=plan_code,
            market_id=market_id,
            effective_from=eff_from,
            effective_to=eff_to
        )

    tariff = TariffPlan(
        id=uuid.uuid4(),
        tariff_code=code,
        name=getattr(tariff_in, 'name', '').strip(),
        description=getattr(tariff_in, 'description', None),
        service_plan_code=plan_code,
        market_id=market_id,
        version=getattr(tariff_in, 'version', 1) or 1,
        billing_type=getattr(tariff_in, 'billing_type', 'POSTPAID'),
        billing_frequency=getattr(tariff_in, 'billing_frequency', 'MONTHLY'),
        billing_timing=getattr(tariff_in, 'billing_timing', 'IN_ADVANCE'),
        currency=getattr(tariff_in, 'currency', 'INR') or "INR",
        base_recurring_charge=getattr(tariff_in, 'base_recurring_charge', Decimal('0.00')),
        setup_fee=getattr(tariff_in, 'setup_fee', Decimal('0.00')),
        usage_rate_per_gb=getattr(tariff_in, 'usage_rate_per_gb', Decimal('0.00')),
        tax_rate_percent=getattr(tariff_in, 'tax_rate_percent', Decimal('18.00')),
        discount_percentage=getattr(tariff_in, 'discount_percentage', Decimal('0.00')),
        effective_from=eff_from,
        effective_to=eff_to,
        status=target_status
    )
    db.add(tariff)
    db.commit()
    db.refresh(tariff)
    return tariff


def update_tariff(db: Session, tariff_id: Any, tariff_in: Any, actor: Optional[User] = None) -> TariffPlan:
    tariff = get_tariff(db, tariff_id)
    update_data = tariff_in.model_dump(exclude_unset=True) if hasattr(tariff_in, 'model_dump') else tariff_in.dict(exclude_unset=True)

    # If updating effective dates or status to ACTIVE, validate overlap
    eff_from = update_data.get('effective_from', tariff.effective_from)
    eff_to = update_data.get('effective_to', tariff.effective_to)
    target_status = update_data.get('status', tariff.status)

    if target_status == TariffStatus.ACTIVE and (
        'effective_from' in update_data or 'effective_to' in update_data or 'status' in update_data
    ):
        validate_effective_date_overlap(
            db,
            service_plan_code=tariff.service_plan_code,
            market_id=update_data.get('market_id', tariff.market_id),
            effective_from=eff_from,
            effective_to=eff_to,
            exclude_tariff_id=tariff.id
        )

    for field, val in update_data.items():
        if val is not None:
            setattr(tariff, field, val)
    tariff.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tariff)
    return tariff


def deactivate_tariff(db: Session, tariff_id: Any, actor: Optional[User] = None) -> TariffPlan:
    tariff = get_tariff(db, tariff_id)
    tariff.status = TariffStatus.INACTIVE
    tariff.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tariff)
    return tariff


def calculate_tariff_breakdown(
    db: Session,
    tariff_code: str,
    usage_gb: float = 0.0,
    apply_setup_fee: bool = False,
    voucher_code: Optional[str] = None
) -> Dict[str, Any]:
    tariff = get_tariff_by_code(db, tariff_code)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tariff '{tariff_code}' not found"
        )

    base_charge = Decimal(str(tariff.base_recurring_charge))
    setup = Decimal(str(tariff.setup_fee)) if apply_setup_fee else Decimal("0.00")
    usage_rate = Decimal(str(tariff.usage_rate_per_gb))
    usage_charge = Decimal(str(max(0.0, float(usage_gb)))) * usage_rate

    gross_subtotal = base_charge + setup + usage_charge
    plan_discount = round(gross_subtotal * (Decimal(str(tariff.discount_percentage)) / Decimal("100.00")), 2)

    voucher_discount = Decimal("0.00")
    if voucher_code:
        from app.models_voucher import Voucher, VoucherStatus
        v = db.query(Voucher).filter(
            Voucher.voucher_code == voucher_code.strip().upper(),
            Voucher.status == VoucherStatus.ACTIVE
        ).first()
        if v:
            if v.voucher_type == "PERCENT_DISCOUNT" or v.voucher_type == "PERCENTAGE":
                calc_v_disc = (gross_subtotal - plan_discount) * (Decimal(str(v.discount_percent or 0)) / Decimal("100.00"))
                if v.max_discount_cap:
                    calc_v_disc = min(calc_v_disc, Decimal(str(v.max_discount_cap)))
                voucher_discount = round(calc_v_disc, 2)
            else:
                voucher_discount = round(Decimal(str(v.discount_amount or 0)), 2)

    taxable_amount = max(Decimal("0.00"), gross_subtotal - plan_discount - voucher_discount)
    tax_rate = Decimal(str(tariff.tax_rate_percent))
    tax_amount = round(taxable_amount * (tax_rate / Decimal("100.00")), 2)
    total_payable = taxable_amount + tax_amount

    return {
        "tariff_code": tariff.tariff_code,
        "service_plan_code": tariff.service_plan_code,
        "currency": tariff.currency,
        "base_recurring_charge": base_charge,
        "setup_fee": setup,
        "usage_gb": usage_gb,
        "usage_rate_per_gb": usage_rate,
        "usage_charge": usage_charge,
        "gross_subtotal": gross_subtotal,
        "plan_discount_amount": plan_discount,
        "voucher_discount_amount": voucher_discount,
        "taxable_amount": taxable_amount,
        "tax_rate_percent": tax_rate,
        "tax_amount": tax_amount,
        "total_amount_payable": total_payable
    }


def seed_canonical_tariffs(db: Session) -> int:
    """
    Seeds initial authoritative v1 tariffs for canonical service plans.
    """
    seed_service_plans_from_canonical(db)
    plans = db.query(ServicePlan).all()
    seeded = 0

    for plan in plans:
        tcode = f"TRF-{plan.plan_code}-V1"
        existing = get_tariff_by_code(db, tcode)
        if not existing:
            freq = "MONTHLY"
            if plan.validity_days == 28:
                freq = "28_DAYS"
            elif plan.validity_days == 56:
                freq = "56_DAYS"
            elif plan.validity_days == 84:
                freq = "84_DAYS"
            elif plan.validity_days == 365:
                freq = "365_DAYS"

            tariff = TariffPlan(
                id=uuid.uuid4(),
                tariff_code=tcode,
                name=f"{plan.name} (Standard Tariff)",
                description=f"Authoritative standard tariff rate for canonical plan {plan.plan_code}",
                service_plan_code=plan.plan_code,
                market_id="ALL",
                version=1,
                billing_type=plan.billing_type,
                billing_frequency=freq,
                billing_timing=BillingTiming.IN_ADVANCE if plan.billing_type == "PREPAID" else BillingTiming.IN_ARREARS,
                currency="INR",
                base_recurring_charge=plan.price,
                setup_fee=Decimal("500.00") if plan.billing_type == "POSTPAID" else Decimal("0.00"),
                usage_rate_per_gb=Decimal("5.00"),  # Standard overage rate
                tax_rate_percent=Decimal("18.00"),  # Standard 18% GST
                discount_percentage=Decimal("0.00"),
                effective_from=datetime(2026, 1, 1),
                status=TariffStatus.ACTIVE
            )
            db.add(tariff)
            seeded += 1

    if seeded > 0:
        db.commit()
    return seeded
