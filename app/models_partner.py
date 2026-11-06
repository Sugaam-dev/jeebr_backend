"""
app/models_partner.py

SentinelOS Multi-Tenant Franchise, LCO Partner Portal & Revenue Share Domain Models.
Supports:
1. Multi-tier Franchise & LCO Partner Hierarchy (Master Distributor -> Sub-Franchisee -> LCO).
2. Authoritative Prepaid Partner Wallet (Float management, credit limits, top-ups, auto-deductions).
3. Wallet Transactions Audit Ledger (Top-ups, activations, renewals, commission payouts).
4. White-label Commission Statements & Statutory TDS Withholding (Section 194H 5% TDS).
5. Automated Bank Statement Ingestion (MT940 / CSV) & Auto-Reconciliation.
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
from app.models_crm import GUID
from app.models import Customer, Invoice


# ─── Enums & Constants ───────────────────────────────────────────────────────

class PartnerType:
    MASTER_DISTRIBUTOR = "MASTER_DISTRIBUTOR"
    SUB_FRANCHISEE = "SUB_FRANCHISEE"
    LCO_OPERATOR = "LCO_OPERATOR"
    ALL = (MASTER_DISTRIBUTOR, SUB_FRANCHISEE, LCO_OPERATOR)


class PartnerStatus:
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    ALL = (ACTIVE, SUSPENDED, TERMINATED)


class CommissionModel:
    FLAT_PERCENTAGE = "FLAT_PERCENTAGE"
    TIERED_VOLUME = "TIERED_VOLUME"
    FIXED_PER_SUB = "FIXED_PER_SUB"
    ALL = (FLAT_PERCENTAGE, TIERED_VOLUME, FIXED_PER_SUB)


class CommissionBasis:
    NET_OF_TAX = "NET_OF_TAX"       # Commission on Taxable Revenue (Standard)
    GROSS_BILLING = "GROSS_BILLING" # Commission on Total Invoice Amount
    ALL = (NET_OF_TAX, GROSS_BILLING)


class WalletTransactionType:
    TOPUP = "TOPUP"
    CUSTOMER_ACTIVATION = "CUSTOMER_ACTIVATION"
    PLAN_RENEWAL = "PLAN_RENEWAL"
    COMMISSION_PAYOUT = "COMMISSION_PAYOUT"
    ADJUSTMENT = "ADJUSTMENT"
    REFUND = "REFUND"
    ALL = (TOPUP, CUSTOMER_ACTIVATION, PLAN_RENEWAL, COMMISSION_PAYOUT, ADJUSTMENT, REFUND)


class CommissionStatementStatus:
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SETTLED = "SETTLED"
    ALL = (DRAFT, APPROVED, SETTLED)


class BankReconciliationStatus:
    UNMATCHED = "UNMATCHED"
    AUTO_MATCHED = "AUTO_MATCHED"
    MANUALLY_RECONCILED = "MANUALLY_RECONCILED"
    IGNORED = "IGNORED"
    ALL = (UNMATCHED, AUTO_MATCHED, MANUALLY_RECONCILED, IGNORED)


# ─── 1. Franchise & LCO Partner Master ───────────────────────────────────────

class FranchisePartner(Base):
    """
    Authoritative franchise, distributor, or Local Cable Operator (LCO) profile.
    Supports multi-tier hierarchy: Master Distributor -> Sub-Franchisee -> LCO Operator.
    """
    __tablename__ = "franchise_partners"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    partner_code = Column(String(32), unique=True, nullable=False, index=True)
    parent_partner_id = Column(GUID(), ForeignKey("franchise_partners.id", ondelete="SET NULL"), nullable=True, index=True)
    partner_type = Column(String(32), default=PartnerType.LCO_OPERATOR, nullable=False)

    legal_name = Column(String(150), nullable=False)
    trade_name = Column(String(150), nullable=False)
    contact_email = Column(String(100), nullable=False)
    contact_phone = Column(String(50), nullable=False)
    gstin = Column(String(15), nullable=True)
    pan = Column(String(10), nullable=True)

    market_id = Column(String(32), default="mumbai", nullable=False, index=True)
    territory = Column(String(100), nullable=True)
    state = Column(String(100), default="Maharashtra", nullable=False)

    # Revenue Share & Commission Settings
    commission_model = Column(String(32), default=CommissionModel.FLAT_PERCENTAGE, nullable=False)
    commission_rate = Column(Numeric(5, 2), default=Decimal('25.00'), nullable=False)  # e.g. 25.00%
    calculation_basis = Column(String(32), default=CommissionBasis.NET_OF_TAX, nullable=False)
    tds_rate = Column(Numeric(5, 2), default=Decimal('5.00'), nullable=False)  # Section 194H 5.00%

    status = Column(String(32), default=PartnerStatus.ACTIVE, nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    parent_partner = relationship("FranchisePartner", remote_side=[id], backref="sub_partners")
    wallet = relationship("PartnerWallet", back_populates="partner", uselist=False, cascade="all, delete-orphan")
    commission_statements = relationship("PartnerCommissionStatement", back_populates="partner")

    __table_args__ = (
        CheckConstraint(f"partner_type IN {PartnerType.ALL}", name="ck_partner_type"),
        CheckConstraint(f"status IN {PartnerStatus.ALL}", name="ck_partner_status"),
        CheckConstraint(f"commission_model IN {CommissionModel.ALL}", name="ck_commission_model"),
        CheckConstraint(f"calculation_basis IN {CommissionBasis.ALL}", name="ck_commission_basis"),
    )


# ─── 2. Prepaid Partner Wallet ───────────────────────────────────────────────

class PartnerWallet(Base):
    """
    Authoritative prepaid balance account for an LCO or franchise partner.
    All activations and plan renewals by the partner require wholesale wallet balance.
    """
    __tablename__ = "partner_wallets"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    partner_id = Column(GUID(), ForeignKey("franchise_partners.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    currency = Column(String(3), default="INR", nullable=False)

    balance = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    credit_limit = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    minimum_threshold = Column(Numeric(12, 2), default=Decimal('500.00'), nullable=False)
    status = Column(String(32), default="ACTIVE", nullable=False, index=True)
    last_topup_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    partner = relationship("FranchisePartner", back_populates="wallet")
    transactions = relationship("WalletTransaction", back_populates="wallet", cascade="all, delete-orphan")


# ─── 3. Wallet Transactions Audit Ledger ─────────────────────────────────────

class WalletTransaction(Base):
    """
    Immutable audit trail for all partner wallet credit and debit events.
    """
    __tablename__ = "wallet_transactions"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    transaction_code = Column(String(32), unique=True, nullable=False, index=True)
    wallet_id = Column(GUID(), ForeignKey("partner_wallets.id", ondelete="CASCADE"), nullable=False, index=True)
    partner_id = Column(GUID(), ForeignKey("franchise_partners.id", ondelete="CASCADE"), nullable=False, index=True)

    transaction_type = Column(String(32), nullable=False)  # TOPUP, CUSTOMER_ACTIVATION, PLAN_RENEWAL, etc.
    direction = Column(String(10), nullable=False)  # CREDIT, DEBIT
    amount = Column(Numeric(12, 2), nullable=False)

    balance_before = Column(Numeric(12, 2), nullable=False)
    balance_after = Column(Numeric(12, 2), nullable=False)

    reference_id = Column(String(100), nullable=True)  # Bank UTR / Order ID / Invoice Code
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    wallet = relationship("PartnerWallet", back_populates="transactions")
    partner = relationship("FranchisePartner")

    __table_args__ = (
        CheckConstraint(f"transaction_type IN {WalletTransactionType.ALL}", name="ck_wallet_tx_type"),
        CheckConstraint("direction IN ('CREDIT', 'DEBIT')", name="ck_wallet_tx_direction"),
        Index("ix_wallet_tx_partner_created", "partner_id", "created_at"),
    )


# ─── 4. Partner Commission Statement & Statutory TDS ─────────────────────────

class PartnerCommissionStatement(Base):
    """
    Monthly revenue share commission payout statement for an LCO partner.
    Calculates gross commission, Section 194H TDS withholding, and net payable.
    """
    __tablename__ = "partner_commission_statements"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    statement_code = Column(String(32), unique=True, nullable=False, index=True)
    partner_id = Column(GUID(), ForeignKey("franchise_partners.id", ondelete="RESTRICT"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)

    active_subscribers_count = Column(Integer, default=0, nullable=False)
    gross_billed_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    taxable_revenue = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    commission_rate = Column(Numeric(5, 2), default=Decimal('25.00'), nullable=False)
    gross_commission = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    tds_rate = Column(Numeric(5, 2), default=Decimal('5.00'), nullable=False)  # Section 194H 5%
    tds_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    net_commission_payable = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    status = Column(String(32), default=CommissionStatementStatus.DRAFT, nullable=False, index=True)
    settled_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    partner = relationship("FranchisePartner", back_populates="commission_statements")

    __table_args__ = (
        CheckConstraint(f"status IN {CommissionStatementStatus.ALL}", name="ck_cms_status"),
        Index("ix_commission_statements_partner_period", "partner_id", "period_start", "period_end"),
    )


# ─── 5. Bank Statement Ingestion & Automated Reconciliation ──────────────────

class BankStatementRecord(Base):
    """
    Ingested bank statement entry (MT940 / CSV / Bank Feed).
    Automated matching engine matches UTR / Ref numbers to open invoices and payments.
    """
    __tablename__ = "bank_statement_records"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    batch_reference = Column(String(64), index=True, nullable=False)
    bank_name = Column(String(64), default="HDFC Bank", nullable=False)

    transaction_date = Column(DateTime, nullable=False)
    value_date = Column(DateTime, nullable=False)
    utr_number = Column(String(64), unique=True, index=True, nullable=False)

    amount = Column(Numeric(12, 2), nullable=False)
    direction = Column(String(10), default="CR", nullable=False)  # CR = Credit, DR = Debit
    narration = Column(Text, nullable=False)

    reconciliation_status = Column(String(32), default=BankReconciliationStatus.UNMATCHED, nullable=False, index=True)
    matched_customer_id = Column(Integer, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    matched_invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)
    matched_payment_id = Column(GUID(), nullable=True)
    confidence_score = Column(Numeric(5, 2), default=Decimal('0.00'), nullable=False)

    reconciled_at = Column(DateTime, nullable=True)
    reconciled_by = Column(String(100), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    matched_customer = relationship("Customer")
    matched_invoice = relationship("Invoice")

    __table_args__ = (
        CheckConstraint(f"reconciliation_status IN {BankReconciliationStatus.ALL}", name="ck_bank_recon_status"),
        Index("ix_bank_statement_status_date", "reconciliation_status", "transaction_date"),
    )
