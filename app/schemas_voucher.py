"""
app/schemas_voucher.py

Pydantic schemas for SentinelOS Voucher Campaign & Coupon Management.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal
import uuid
from pydantic import BaseModel, Field


class VoucherBase(BaseModel):
    voucher_code: str = Field(..., max_length=64, description="Unique coupon code e.g. WELCOME100, RETENTION20")
    campaign_name: str = Field(..., max_length=100)
    description: Optional[str] = None
    voucher_type: str = Field(..., pattern="^(BILL_DISCOUNT|PERCENT_DISCOUNT|SPEED_BOOST|DATA_TOPUP|OTT_PASS)$")
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    discount_percent: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"), le=Decimal("100.00"))
    max_discount_cap: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    min_invoice_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    benefit_payload: Optional[Dict[str, Any]] = None
    max_redemptions: int = Field(1, ge=1, description="Total global redemptions allowed")
    per_customer_limit: int = Field(1, ge=1, description="Max redemptions per customer")
    valid_from: Optional[datetime] = None
    valid_until: datetime
    market_id: str = Field("ALL", max_length=50)
    applicable_plan_id: Optional[str] = None
    status: str = Field("ACTIVE", pattern="^(ACTIVE|PAUSED|EXPIRED|EXHAUSTED|REVOKED)$")


class VoucherCreate(VoucherBase):
    pass


class VoucherBulkCreate(BaseModel):
    prefix: str = Field(..., max_length=20, description="Code prefix e.g. PROMO, FESTIVE")
    count: int = Field(..., ge=1, le=500, description="Number of unique vouchers to generate")
    campaign_name: str = Field(..., max_length=100)
    description: Optional[str] = None
    voucher_type: str = Field(..., pattern="^(BILL_DISCOUNT|PERCENT_DISCOUNT|SPEED_BOOST|DATA_TOPUP|OTT_PASS)$")
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    discount_percent: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"), le=Decimal("100.00"))
    max_discount_cap: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    min_invoice_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    benefit_payload: Optional[Dict[str, Any]] = None
    max_redemptions: int = Field(1, ge=1)
    per_customer_limit: int = Field(1, ge=1)
    valid_from: Optional[datetime] = None
    valid_until: datetime
    market_id: str = Field("ALL", max_length=50)
    applicable_plan_id: Optional[str] = None


class VoucherUpdate(BaseModel):
    campaign_name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    valid_until: Optional[datetime] = None
    max_redemptions: Optional[int] = Field(None, ge=1)
    per_customer_limit: Optional[int] = Field(None, ge=1)
    status: Optional[str] = Field(None, pattern="^(ACTIVE|PAUSED|EXPIRED|EXHAUSTED|REVOKED)$")


class VoucherResponse(VoucherBase):
    id: uuid.UUID
    current_redemptions: int
    is_valid: bool
    created_by_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VoucherValidationRequest(BaseModel):
    voucher_code: str
    customer_id: int
    invoice_amount: Optional[Decimal] = Decimal("0.00")
    plan_id: Optional[str] = None


class VoucherValidationResponse(BaseModel):
    is_valid: bool
    voucher_code: str
    voucher_type: Optional[str] = None
    calculated_discount: Decimal = Decimal("0.00")
    reason: Optional[str] = None
