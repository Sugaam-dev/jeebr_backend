"""
app/models_tariff.py

SentinelOS Tariff Plan & Dynamic Pricing Structure Model.
Defines canonical recurring rates, setup fees, usage rates, billing timing,
tax percentages (18% GST), effective date ranges, and versioned pricing definitions.
Integrates with ServicePlans, BillingSubscriptions, and Invoices.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, Text, ForeignKey,
    CheckConstraint, Index
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models_crm import GUID


class TariffStatus:
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"
    ALL = (ACTIVE, INACTIVE, ARCHIVED)


class BillingTiming:
    IN_ADVANCE = "IN_ADVANCE"
    IN_ARREARS = "IN_ARREARS"
    ALL = (IN_ADVANCE, IN_ARREARS)


class TariffPlan(Base):
    """
    Authoritative pricing rules and charge structures associated with canonical telecom service plans.
    Enforces effective dating, version lock to prevent historical invoice mutation, and decimal precision.
    """
    __tablename__ = 'tariff_plans'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    tariff_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. TRF-PRE-HERO-50M-V1
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Association with Canonical Service Plan
    service_plan_code = Column(String(50), ForeignKey('service_plans.plan_code', ondelete='RESTRICT'), index=True, nullable=False)
    market_id = Column(String(32), default='ALL', index=True, nullable=False)  # ALL, mumbai, kolkata
    version = Column(Integer, default=1, nullable=False)

    # Billing & Charging Structure
    billing_type = Column(String(32), default='POSTPAID', nullable=False)      # PREPAID, POSTPAID
    billing_frequency = Column(String(32), default='MONTHLY', nullable=False)  # MONTHLY, QUARTERLY, ANNUAL, 28_DAYS, 56_DAYS, 84_DAYS, 365_DAYS
    billing_timing = Column(String(32), default='IN_ADVANCE', nullable=False)  # IN_ADVANCE, IN_ARREARS
    currency = Column(String(3), default='INR', nullable=False)

    # Financial Charge Breakdown (High-precision Decimal)
    base_recurring_charge = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    setup_fee = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    usage_rate_per_gb = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    tax_rate_percent = Column(Numeric(5, 2), default=Decimal('18.00'), nullable=False)  # Standard 18% GST
    discount_percentage = Column(Numeric(5, 2), default=Decimal('0.00'), nullable=False)

    # Effective Dating & Versioning
    effective_from = Column(DateTime, default=datetime.utcnow, nullable=False)
    effective_to = Column(DateTime, nullable=True)

    # Lifecycle State
    status = Column(String(32), default=TariffStatus.ACTIVE, index=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    service_plan = relationship('ServicePlan')

    __table_args__ = (
        CheckConstraint(f"status IN {TariffStatus.ALL}", name="chk_tariff_status"),
        CheckConstraint("billing_type IN ('PREPAID', 'POSTPAID')", name="chk_tariff_billing_type"),
        CheckConstraint(f"billing_timing IN {BillingTiming.ALL}", name="chk_tariff_billing_timing"),
        CheckConstraint("base_recurring_charge >= 0", name="chk_tariff_base_charge_positive"),
        CheckConstraint("setup_fee >= 0", name="chk_tariff_setup_fee_positive"),
        CheckConstraint("usage_rate_per_gb >= 0", name="chk_tariff_usage_rate_positive"),
        CheckConstraint("tax_rate_percent >= 0 AND tax_rate_percent <= 100", name="chk_tariff_tax_rate_bounds"),
        CheckConstraint("discount_percentage >= 0 AND discount_percentage <= 100", name="chk_tariff_discount_bounds"),
        Index("ix_tariff_plans_code_market", "tariff_code", "market_id"),
        Index("ix_tariff_plans_lookup", "service_plan_code", "market_id", "status"),
    )
