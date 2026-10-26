"""
app/models_service_plan.py

SentinelOS Persistent Canonical Service-Plan Catalog Model.
Stores telecom tariff definitions, pricing, speeds, quotas, and lifecycles.
Integrates with BillingSubscriptions and ServiceActivationOrders.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Column, String, Integer, Numeric, Boolean, DateTime, Text, Index
from app.database import Base
from app.models_crm import GUID


class ServicePlan(Base):
    """
    Authoritative database entity for canonical telecom tariff packages.
    Prevents destructive removal of plans referenced by historical subscriptions.
    """
    __tablename__ = 'service_plans'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    plan_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. PLAN-PRE-HERO-1.5G
    name = Column(String(100), nullable=False)
    account_type = Column(String(32), default='RESIDENTIAL', nullable=False)  # RESIDENTIAL, BUSINESS
    billing_type = Column(String(32), default='POSTPAID', nullable=False)     # PREPAID, POSTPAID
    price = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), default='INR', nullable=False)
    speed_mbps = Column(Integer, default=50, nullable=False)
    data_quota_gb = Column(Numeric(10, 2), default=Decimal('0.00'), nullable=False)
    daily_quota_gb = Column(Numeric(10, 2), nullable=True)
    validity_days = Column(Integer, default=28, nullable=False)
    segment = Column(String(50), nullable=False)
    service_vlan = Column(Integer, default=101, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, index=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_service_plans_type_active", "billing_type", "is_active"),
    )
