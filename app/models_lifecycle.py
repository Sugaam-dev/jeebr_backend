"""
app/models_lifecycle.py

SentinelOS Complex Telecom Lifecycle & Equipment Inventory Domain Models.
Supports:
1. Vacation Holds & Temporary Suspensions (safe custody line retention & proration adjustments).
2. Geographic Relocations (premises shift, fiber feasibility, and Section 12(11)(a) PoS recalibration).
3. CPE & Hardware Asset Lifecycle (Rental charges, refundable security deposits, and damage deductions).
4. Tariff Plan Upgrades & Downgrades (Immediate proration vs next-cycle scheduled transitions).
5. SLA Breach Outage Evaluation & Automatic Credit Note Issuance.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Numeric, CheckConstraint, Index, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID, CRMCustomer, CRMAddress
from app.models_finance import BillingAccount, BillingSubscription, CreditNote
from app.models_inventory import InventoryDevice
from app.models import Customer, Invoice


# ─── Status Enums ────────────────────────────────────────────────────────────

class VacationHoldStatus:
    ACTIVE_HOLD = "ACTIVE_HOLD"
    RESUMED = "RESUMED"
    CANCELLED = "CANCELLED"
    ALL = (ACTIVE_HOLD, RESUMED, CANCELLED)


class RelocationStatus:
    REQUESTED = "REQUESTED"
    FEASIBILITY_CONFIRMED = "FEASIBILITY_CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ALL = (REQUESTED, FEASIBILITY_CONFIRMED, COMPLETED, CANCELLED)


class CPEOwnershipType:
    ISP_RENTAL = "ISP_RENTAL"
    SECURITY_DEPOSIT = "SECURITY_DEPOSIT"
    CUSTOMER_OWNED = "CUSTOMER_OWNED"
    ALL = (ISP_RENTAL, SECURITY_DEPOSIT, CUSTOMER_OWNED)


class CPEAssignmentStatus:
    ASSIGNED = "ASSIGNED"
    RETURNED = "RETURNED"
    LOST = "LOST"
    DAMAGED = "DAMAGED"
    ALL = (ASSIGNED, RETURNED, LOST, DAMAGED)


class PlanChangeMode:
    IMMEDIATE = "IMMEDIATE"
    NEXT_CYCLE = "NEXT_CYCLE"
    ALL = (IMMEDIATE, NEXT_CYCLE)


class PlanChangeStatus:
    SCHEDULED = "SCHEDULED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    ALL = (SCHEDULED, EXECUTED, CANCELLED)


class SLAIncidentStatus:
    EVALUATED = "EVALUATED"
    CREDIT_ISSUED = "CREDIT_ISSUED"
    EXEMPTED = "EXEMPTED"
    ALL = (EVALUATED, CREDIT_ISSUED, EXEMPTED)


# ─── 1. Vacation Hold & Temporary Suspension ─────────────────────────────────

class VacationHold(Base):
    """
    Temporary suspension of subscriber services with safe custody line retention fees.
    Prorates bandwidth charges while preserving physical optical port reservation.
    """
    __tablename__ = "vacation_holds"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    subscription_id = Column(GUID(), ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    billing_account_id = Column(GUID(), ForeignKey("billing_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    hold_start_date = Column(DateTime, nullable=False)
    expected_resume_date = Column(DateTime, nullable=False)
    actual_resume_date = Column(DateTime, nullable=True)

    retention_fee_monthly = Column(Numeric(12, 2), default=Decimal('150.00'), nullable=False)
    original_monthly_price = Column(Numeric(12, 2), nullable=False)
    credit_adjustment_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    status = Column(String(32), default=VacationHoldStatus.ACTIVE_HOLD, nullable=False, index=True)
    reason = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    subscription = relationship("BillingSubscription")
    billing_account = relationship("BillingAccount")

    __table_args__ = (
        CheckConstraint(f"status IN {VacationHoldStatus.ALL}", name="ck_vacation_hold_status"),
        Index("ix_vacation_holds_sub_status", "subscription_id", "status"),
    )


# ─── 2. Geographic Premises Relocation ───────────────────────────────────────

class PremisesRelocation(Base):
    """
    Geographic premises shift tracking with Section 12(11)(a) GST Place of Supply recalibration.
    """
    __tablename__ = "premises_relocations"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    relocation_code = Column(String(32), unique=True, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="SET NULL"), nullable=True, index=True)
    subscription_id = Column(GUID(), ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    old_address_id = Column(GUID(), ForeignKey("crm_addresses.id", ondelete="SET NULL"), nullable=True)
    new_address_id = Column(GUID(), ForeignKey("crm_addresses.id", ondelete="RESTRICT"), nullable=False)

    old_place_of_supply = Column(String(64), nullable=False)
    new_place_of_supply = Column(String(64), nullable=False)
    feasibility_status = Column(String(32), default="FEASIBLE", nullable=False)

    shift_charge = Column(Numeric(12, 2), default=Decimal('1000.00'), nullable=False)
    shift_invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(32), default=RelocationStatus.REQUESTED, nullable=False, index=True)
    requested_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_date = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    customer = relationship("Customer")
    crm_customer = relationship("CRMCustomer")
    subscription = relationship("BillingSubscription")
    old_address = relationship("CRMAddress", foreign_keys=[old_address_id])
    new_address = relationship("CRMAddress", foreign_keys=[new_address_id])
    shift_invoice = relationship("Invoice")

    __table_args__ = (
        CheckConstraint(f"status IN {RelocationStatus.ALL}", name="ck_relocation_status"),
    )


# ─── 3. Customer CPE Asset Assignment & Security Deposit ─────────────────────

class CPEAssetAssignment(Base):
    """
    Authoritative binding of serialized inventory hardware (ONT, Router) to subscriber billing.
    Manages monthly equipment rental, security deposit liabilities, and return depreciation.
    """
    __tablename__ = "cpe_asset_assignments"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    device_id = Column(Integer, ForeignKey("inventory_devices.id", ondelete="RESTRICT"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="SET NULL"), nullable=True, index=True)
    billing_account_id = Column(GUID(), ForeignKey("billing_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id = Column(GUID(), ForeignKey("billing_subscriptions.id", ondelete="SET NULL"), nullable=True, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    ownership_type = Column(String(32), default=CPEOwnershipType.ISP_RENTAL, nullable=False)
    monthly_rental_price = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    security_deposit_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    deposit_paid = Column(Boolean, default=False, nullable=False)
    deposit_refunded = Column(Boolean, default=False, nullable=False)
    damage_deduction = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    status = Column(String(32), default=CPEAssignmentStatus.ASSIGNED, nullable=False, index=True)
    condition_on_return = Column(String(64), nullable=True)  # GOOD, FAIR, DAMAGED, UNREPAIRABLE

    assigned_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    returned_date = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    device = relationship("InventoryDevice")
    customer = relationship("Customer")
    crm_customer = relationship("CRMCustomer")
    billing_account = relationship("BillingAccount")
    subscription = relationship("BillingSubscription")

    __table_args__ = (
        CheckConstraint(f"ownership_type IN {CPEOwnershipType.ALL}", name="ck_cpe_ownership_type"),
        CheckConstraint(f"status IN {CPEAssignmentStatus.ALL}", name="ck_cpe_assignment_status"),
        Index("ix_cpe_assignments_device_status", "device_id", "status"),
    )


# ─── 4. Tariff Plan Upgrades & Downgrades ────────────────────────────────────

class ScheduledPlanChange(Base):
    """
    Scheduled or immediate plan transition record.
    Preserves audit trail of rate plan changes, proration credits, and effective dates.
    """
    __tablename__ = "scheduled_plan_changes"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    subscription_id = Column(GUID(), ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    billing_account_id = Column(GUID(), ForeignKey("billing_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    current_plan_id = Column(String(50), nullable=False)
    target_plan_id = Column(String(50), nullable=False)
    change_type = Column(String(32), nullable=False)  # UPGRADE, DOWNGRADE
    effective_mode = Column(String(32), default=PlanChangeMode.IMMEDIATE, nullable=False)

    scheduled_effective_date = Column(DateTime, nullable=False)
    executed_at = Column(DateTime, nullable=True)
    status = Column(String(32), default=PlanChangeStatus.SCHEDULED, nullable=False, index=True)

    adjustment_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    adjustment_invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    subscription = relationship("BillingSubscription")
    billing_account = relationship("BillingAccount")
    adjustment_invoice = relationship("Invoice")

    __table_args__ = (
        CheckConstraint(f"effective_mode IN {PlanChangeMode.ALL}", name="ck_plan_change_mode"),
        CheckConstraint(f"status IN {PlanChangeStatus.ALL}", name="ck_plan_change_status"),
    )


# ─── 5. SLA Breach Outage Incident & Credit Note ─────────────────────────────

class SLAIncidentRecord(Base):
    """
    Enterprise SLA availability tracking and automated credit note issuance.
    Compares committed uptime against actual availability and computes penalty credits.
    """
    __tablename__ = "sla_incident_records"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    incident_code = Column(String(32), unique=True, nullable=False, index=True)
    subscription_id = Column(GUID(), ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    billing_account_id = Column(GUID(), ForeignKey("billing_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    sla_tier = Column(String(32), nullable=False)  # STANDARD_99_5, PREMIUM_99_9, MISSION_CRITICAL_99_95
    committed_uptime_pct = Column(Numeric(5, 2), nullable=False)  # 99.50, 99.90, 99.95

    billing_period_start = Column(DateTime, nullable=False)
    billing_period_end = Column(DateTime, nullable=False)
    total_period_minutes = Column(Integer, nullable=False)
    total_downtime_minutes = Column(Integer, default=0, nullable=False)
    actual_uptime_pct = Column(Numeric(5, 2), nullable=False)

    sla_breached = Column(Boolean, default=False, nullable=False, index=True)
    penalty_credit_pct = Column(Numeric(5, 2), default=Decimal('0.00'), nullable=False)

    credit_note_id = Column(GUID(), ForeignKey("credit_notes.id", ondelete="SET NULL"), nullable=True)
    credit_note_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    status = Column(String(32), default=SLAIncidentStatus.EVALUATED, nullable=False, index=True)
    root_cause = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    subscription = relationship("BillingSubscription")
    billing_account = relationship("BillingAccount")
    customer = relationship("Customer")
    credit_note = relationship("CreditNote")

    __table_args__ = (
        CheckConstraint(f"status IN {SLAIncidentStatus.ALL}", name="ck_sla_incident_status"),
    )
