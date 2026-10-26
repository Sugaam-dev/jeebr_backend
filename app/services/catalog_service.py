"""
app/services/catalog_service.py

Persistent Canonical Service-Plan Catalog Service for SentinelOS.
Reconciles relational database records with canonical telecom tariff packages.
Prevents dual source-of-truth divergence and enforces plan lifecycle stability.
"""

from decimal import Decimal
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from app.models_service_plan import ServicePlan
from app.services.provisioning_catalog import CANONICAL_SERVICE_PLANS


def seed_service_plans_from_canonical(db: Session) -> int:
    """
    Deterministically and idempotently seeds canonical service plans into database.
    Does not overwrite custom modifications if plan already exists.
    """
    seeded_count = 0
    for plan in CANONICAL_SERVICE_PLANS:
        code = plan["plan_id"]
        existing = db.query(ServicePlan).filter(ServicePlan.plan_code == code).first()
        if not existing:
            sp = ServicePlan(
                plan_code=code,
                name=plan["name"],
                account_type=plan.get("account_type", "RESIDENTIAL"),
                billing_type=plan.get("billing_type", "POSTPAID"),
                price=Decimal(str(plan.get("price_inr", 0.0))),
                currency="INR",
                speed_mbps=int(plan.get("speed_mbps", 50)),
                data_quota_gb=Decimal(str(plan.get("data_quota_gb", 0.0))),
                daily_quota_gb=Decimal(str(plan["daily_quota_gb"])) if plan.get("daily_quota_gb") is not None else None,
                validity_days=int(plan.get("validity_days", 28)),
                segment=plan.get("segment", "Broadband"),
                service_vlan=int(plan.get("service_vlan", 101)),
                description=plan.get("description", ""),
                is_active=True
            )
            db.add(sp)
            seeded_count += 1
    if seeded_count > 0:
        db.commit()
    return seeded_count


def get_all_service_plans(
    db: Session,
    account_type: Optional[str] = None,
    billing_type: Optional[str] = None,
    active_only: bool = True
) -> List[ServicePlan]:
    """Retrieve catalog plans with optional filtering."""
    query = db.query(ServicePlan)
    if active_only:
        query = query.filter(ServicePlan.is_active == True)
    if account_type:
        query = query.filter(ServicePlan.account_type.ilike(account_type))
    if billing_type:
        query = query.filter(ServicePlan.billing_type.ilike(billing_type))
    return query.order_by(ServicePlan.price.asc()).all()


def get_service_plan_by_code(db: Session, plan_code: str) -> Optional[ServicePlan]:
    """Lookup a single plan by unique plan code."""
    return db.query(ServicePlan).filter(ServicePlan.plan_code == plan_code).first()


def deactivate_service_plan(db: Session, plan_code: str) -> Optional[ServicePlan]:
    """
    Safely deactivate a plan rather than destructively deleting it.
    Preserves historical subscription integrity and issued invoice references.
    """
    plan = get_service_plan_by_code(db, plan_code)
    if plan:
        plan.is_active = False
        db.commit()
        db.refresh(plan)
    return plan
