"""
app/models_voucher.py

SentinelOS Voucher Campaign & Coupon Code Definition Model.
Enforces multi-use / single-use redemption policies, fixed vs percentage discounts with caps,
per-customer limits, valid date ranges, and atomic concurrency controls.
Integrates with VoucherRedemption, Billing, and General Ledger.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, Text, ForeignKey,
    CheckConstraint, Index, JSON
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models_crm import GUID


class VoucherStatus:
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EXPIRED = "EXPIRED"
    EXHAUSTED = "EXHAUSTED"
    REVOKED = "REVOKED"
    ALL = (ACTIVE, PAUSED, EXPIRED, EXHAUSTED, REVOKED)


class VoucherDefinitionType:
    BILL_DISCOUNT = "BILL_DISCOUNT"
    PERCENT_DISCOUNT = "PERCENT_DISCOUNT"
    SPEED_BOOST = "SPEED_BOOST"
    DATA_TOPUP = "DATA_TOPUP"
    OTT_PASS = "OTT_PASS"
    ALL = (BILL_DISCOUNT, PERCENT_DISCOUNT, SPEED_BOOST, DATA_TOPUP, OTT_PASS)


class Voucher(Base):
    """
    Authoritative definition of promotional, courtesy, and retention voucher campaigns.
    Controls discount mathematics, validity windows, maximum redemptions, and per-subscriber caps.
    """
    __tablename__ = 'vouchers'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    voucher_code = Column(String(64), unique=True, index=True, nullable=False)  # e.g. WELCOME100, RETENTION20
    campaign_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Benefit & Discount Definition
    voucher_type = Column(String(50), nullable=False)  # BILL_DISCOUNT, PERCENT_DISCOUNT, SPEED_BOOST, DATA_TOPUP, OTT_PASS
    discount_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)  # Fixed INR discount
    discount_percent = Column(Numeric(5, 2), default=Decimal('0.00'), nullable=False)   # Percentage discount (0-100)
    max_discount_cap = Column(Numeric(12, 2), nullable=True)                          # Max discount cap in INR
    min_invoice_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    benefit_payload = Column(JSON, default=dict)                                       # Dynamic metadata (speed, gb, ott)

    # Redemption Caps & Limits
    max_redemptions = Column(Integer, default=1, nullable=False)     # Total global redemptions allowed (1=single-use, N=multi-use)
    current_redemptions = Column(Integer, default=0, nullable=False) # Current successfully fulfilled redemptions
    per_customer_limit = Column(Integer, default=1, nullable=False)  # Maximum redemptions permitted per customer

    # Eligibility & Scoping
    valid_from = Column(DateTime, default=datetime.utcnow, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    market_id = Column(String(50), default='ALL', index=True, nullable=False)  # ALL, mumbai, kolkata
    applicable_plan_id = Column(String(50), nullable=True)                     # Optional plan restriction

    # Lifecycle State
    status = Column(String(32), default=VoucherStatus.ACTIVE, index=True, nullable=False)

    created_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    created_by = relationship('User')

    __table_args__ = (
        CheckConstraint(f"status IN {VoucherStatus.ALL}", name="chk_voucher_status"),
        CheckConstraint(f"voucher_type IN {VoucherDefinitionType.ALL}", name="chk_voucher_type"),
        CheckConstraint("max_redemptions >= 1", name="chk_voucher_max_redemptions"),
        CheckConstraint("current_redemptions >= 0", name="chk_voucher_current_redemptions_positive"),
        CheckConstraint("current_redemptions <= max_redemptions", name="chk_voucher_redemptions_within_bounds"),
        CheckConstraint("per_customer_limit >= 1", name="chk_voucher_per_customer_limit"),
        CheckConstraint("discount_amount >= 0", name="chk_voucher_discount_amount_positive"),
        CheckConstraint("discount_percent >= 0 AND discount_percent <= 100", name="chk_voucher_discount_percent_bounds"),
        Index("ix_vouchers_market_status", "market_id", "status"),
        Index("ix_vouchers_validity", "valid_from", "valid_until", "status"),
    )

    @property
    def is_valid(self) -> bool:
        now = datetime.utcnow()
        return (
            self.status == VoucherStatus.ACTIVE
            and self.valid_from <= now <= self.valid_until
            and self.current_redemptions < self.max_redemptions
        )
