"""
app/models_cpq.py

SentinelOS Omnichannel Sales CRM & CPQ (Configure, Price, Quote) Domain Models.
Enables enterprise telecom sales pipelines, lead scoring, fiber feasibility checks,
custom ILL (Internet Leased Line) / DIA (Dedicated Internet Access) quotation configuration,
discount approval governance, and one-click quote-to-customer conversion.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Numeric, CheckConstraint, Index, Float
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID


class LeadSalesStage:
    NEW = "NEW"
    QUALIFIED = "QUALIFIED"
    FEASIBILITY_CHECK = "FEASIBILITY_CHECK"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    NEGOTIATION = "NEGOTIATION"
    WON = "WON"
    LOST = "LOST"
    ALL = (NEW, QUALIFIED, FEASIBILITY_CHECK, PROPOSAL_SENT, NEGOTIATION, WON, LOST)


class LeadSource:
    WEBSITE = "WEBSITE"
    INBOUND_CALL = "INBOUND_CALL"
    SALES_VISIT = "SALES_VISIT"
    CHANNEL_PARTNER = "CHANNEL_PARTNER"
    CAMPAIGN = "CAMPAIGN"
    REFERRAL = "REFERRAL"
    ALL = (WEBSITE, INBOUND_CALL, SALES_VISIT, CHANNEL_PARTNER, CAMPAIGN, REFERRAL)


class FeasibilityStatus:
    PENDING = "PENDING"
    FEASIBLE = "FEASIBLE"
    HIGH_COST_BUILD = "HIGH_COST_BUILD"
    UNSERVICEABLE = "UNSERVICEABLE"
    ALL = (PENDING, FEASIBLE, HIGH_COST_BUILD, UNSERVICEABLE)


class QuotationStatus:
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONVERTED = "CONVERTED"
    ALL = (DRAFT, PENDING_APPROVAL, APPROVED, ACCEPTED, REJECTED, EXPIRED, CONVERTED)


class SLATier:
    STANDARD_99_5 = "STANDARD_99_5"        # 99.5% Uptime SLA (Standard SME)
    PREMIUM_99_9 = "PREMIUM_99_9"          # 99.9% Uptime SLA (+15% price markup)
    MISSION_CRITICAL_99_95 = "MISSION_CRITICAL_99_95"  # 99.95% High-Availability (+30% price markup)
    ALL = (STANDARD_99_5, PREMIUM_99_9, MISSION_CRITICAL_99_95)


class QuotationItemType:
    BANDWIDTH = "BANDWIDTH"
    STATIC_IP = "STATIC_IP"
    MANAGED_CPE = "MANAGED_CPE"
    SLA_ADDON = "SLA_ADDON"
    INSTALLATION_OTC = "INSTALLATION_OTC"
    ALL = (BANDWIDTH, STATIC_IP, MANAGED_CPE, SLA_ADDON, INSTALLATION_OTC)


# ─── CRM Lead Model ──────────────────────────────────────────────────────────

class CRMLead(Base):
    __tablename__ = "crm_leads"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    lead_code = Column(String(32), unique=True, nullable=False, index=True)
    company_name = Column(String(255), nullable=False, index=True)
    contact_name = Column(String(150), nullable=False)
    email = Column(String(255), nullable=True, index=True)
    phone = Column(String(32), nullable=False, index=True)
    account_type = Column(String(32), nullable=False, default="ENTERPRISE")
    sales_stage = Column(String(32), nullable=False, default=LeadSalesStage.NEW, index=True)
    lead_score = Column(Integer, default=50, nullable=False)
    lead_source = Column(String(50), default=LeadSource.WEBSITE, nullable=False)
    assigned_sales_rep_id = Column(Integer, nullable=True)
    market_id = Column(String(32), nullable=False, default="mumbai", index=True)

    # Location & Feasibility
    installation_address = Column(Text, nullable=True)
    pincode = Column(String(10), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    feasibility_status = Column(String(32), default=FeasibilityStatus.PENDING, nullable=False)
    fiber_distance_meters = Column(Integer, nullable=True)

    estimated_mrr = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    lost_reason = Column(Text, nullable=True)
    converted_crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    quotations = relationship("Quotation", back_populates="lead", cascade="all, delete-orphan")
    converted_customer = relationship("CRMCustomer")

    __table_args__ = (
        CheckConstraint(f"sales_stage IN {LeadSalesStage.ALL}", name="ck_lead_sales_stage"),
        CheckConstraint(f"feasibility_status IN {FeasibilityStatus.ALL}", name="ck_lead_feasibility_status"),
        Index("ix_crm_leads_market_stage", "market_id", "sales_stage"),
    )


# ─── Quotation Model ─────────────────────────────────────────────────────────

class Quotation(Base):
    __tablename__ = "quotations"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    quotation_code = Column(String(32), unique=True, nullable=False, index=True)
    lead_id = Column(GUID(), ForeignKey("crm_leads.id", ondelete="CASCADE"), nullable=True, index=True)
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="SET NULL"), nullable=True, index=True)
    market_id = Column(String(32), nullable=False, default="mumbai", index=True)
    version = Column(Integer, default=1, nullable=False)

    status = Column(String(32), default=QuotationStatus.DRAFT, nullable=False, index=True)
    contract_term_months = Column(Integer, default=12, nullable=False)
    billing_frequency = Column(String(32), default="MONTHLY", nullable=False)

    # Commercial Breakdown
    base_mrr = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    discount_pct = Column(Numeric(5, 2), default=Decimal('0.00'), nullable=False)
    discounted_mrr = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    setup_charges = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)  # OTC
    tax_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    total_first_payment = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    # Governance & Approval Workflow
    requires_approval = Column(Boolean, default=False, nullable=False)
    approval_status = Column(String(32), default="AUTO_APPROVED", nullable=False)
    approved_by_id = Column(Integer, nullable=True)
    approval_notes = Column(Text, nullable=True)

    valid_until = Column(DateTime, nullable=False)
    terms_and_conditions = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    lead = relationship("CRMLead", back_populates="quotations")
    customer = relationship("CRMCustomer")
    items = relationship("QuotationItem", back_populates="quotation", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(f"status IN {QuotationStatus.ALL}", name="ck_quotation_status"),
        Index("ix_quotations_market_status", "market_id", "status"),
    )


# ─── Quotation Line Item Model ───────────────────────────────────────────────

class QuotationItem(Base):
    __tablename__ = "quotation_items"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    quotation_id = Column(GUID(), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True)
    item_type = Column(String(32), default=QuotationItemType.BANDWIDTH, nullable=False)
    plan_id = Column(String(50), nullable=True)
    description = Column(String(255), nullable=False)

    # Technical Specifications
    bandwidth_mbps = Column(Integer, default=100, nullable=False)
    contention_ratio = Column(String(20), default="1:1", nullable=False)  # 1:1 (Dedicated ILL), 1:4, 1:8
    sla_tier = Column(String(32), default=SLATier.STANDARD_99_5, nullable=False)

    quantity = Column(Numeric(10, 2), default=Decimal('1.00'), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    subtotal = Column(Numeric(12, 2), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    quotation = relationship("Quotation", back_populates="items")

    __table_args__ = (
        CheckConstraint(f"item_type IN {QuotationItemType.ALL}", name="ck_quotation_item_type"),
        CheckConstraint(f"sla_tier IN {SLATier.ALL}", name="ck_quotation_item_sla"),
    )
