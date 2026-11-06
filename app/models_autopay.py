"""
app/models_autopay.py

SentinelOS Recurring Payment & Mandate Automation Domain Models.
Implements Reserve Bank of India (RBI) e-Mandate Framework & NPCI UPI AutoPay / e-NACH specifications.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Numeric, CheckConstraint, Index
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID


class MandateType:
    UPI_AUTOPAY = "UPI_AUTOPAY"
    NACH_ENACH = "NACH_ENACH"
    CARD_TOKEN = "CARD_TOKEN"
    ALL = (UPI_AUTOPAY, NACH_ENACH, CARD_TOKEN)


class MandateStatus:
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    ALL = (PENDING, ACTIVE, PAUSED, REVOKED, EXPIRED)


class NotificationChannel:
    SMS = "SMS"
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    ALL = (SMS, EMAIL, WHATSAPP)


class PaymentMandate(Base):
    __tablename__ = "payment_mandates"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    mandate_code = Column(String(32), unique=True, nullable=False, index=True)
    billing_account_id = Column(GUID(), ForeignKey("billing_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    mandate_type = Column(String(32), default=MandateType.UPI_AUTOPAY, nullable=False)
    umrn = Column(String(100), unique=True, nullable=False, index=True)  # Unique Mandate Reference Number (NPCI)
    max_amount = Column(Numeric(12, 2), default=Decimal('15000.00'), nullable=False)
    frequency = Column(String(32), default="MONTHLY", nullable=False)
    status = Column(String(32), default=MandateStatus.ACTIVE, nullable=False, index=True)

    # Identifiers
    vpa = Column(String(100), nullable=True)  # user@okhdfcbank
    bank_name = Column(String(100), nullable=True)
    account_number_masked = Column(String(50), nullable=True)

    start_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    end_date = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    billing_account = relationship("BillingAccount")
    customer = relationship("Customer")
    notifications = relationship("PreDebitNotification", back_populates="mandate", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(f"mandate_type IN {MandateType.ALL}", name="ck_mandate_type"),
        CheckConstraint(f"status IN {MandateStatus.ALL}", name="ck_mandate_status"),
        Index("ix_payment_mandates_market_status", "market_id", "status"),
    )


class PreDebitNotification(Base):
    __tablename__ = "pre_debit_notifications"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    mandate_id = Column(GUID(), ForeignKey("payment_mandates.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    notification_reference = Column(String(64), unique=True, nullable=False, index=True)

    scheduled_debit_date = Column(DateTime, nullable=False)
    debit_amount = Column(Numeric(12, 2), nullable=False)
    channel = Column(String(32), default=NotificationChannel.SMS, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String(32), default="SENT", nullable=False)
    opt_out_url = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    mandate = relationship("PaymentMandate", back_populates="notifications")
    invoice = relationship("Invoice")

    __table_args__ = (
        CheckConstraint(f"channel IN {NotificationChannel.ALL}", name="ck_pdn_channel"),
    )
