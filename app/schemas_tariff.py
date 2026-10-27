"""
app/schemas_tariff.py

Pydantic schemas for SentinelOS Tariff Plan Management.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal
import uuid
from pydantic import BaseModel, Field


class TariffPlanBase(BaseModel):
    tariff_code: str = Field(..., max_length=50, description="Unique tariff code e.g. TRF-PRE-HERO-50M-V1")
    name: str = Field(..., max_length=100)
    description: Optional[str] = None
    service_plan_code: str = Field(..., max_length=50, description="Canonical service plan code")
    market_id: str = Field("ALL", max_length=32)
    version: int = Field(1, ge=1)
    billing_type: str = Field("POSTPAID", pattern="^(PREPAID|POSTPAID)$")
    billing_frequency: str = Field("MONTHLY", max_length=32)
    billing_timing: str = Field("IN_ADVANCE", pattern="^(IN_ADVANCE|IN_ARREARS)$")
    currency: str = Field("INR", max_length=3)
    base_recurring_charge: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    setup_fee: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    usage_rate_per_gb: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    tax_rate_percent: Decimal = Field(default=Decimal("18.00"), ge=Decimal("0.00"), le=Decimal("100.00"))
    discount_percentage: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"), le=Decimal("100.00"))
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    status: str = Field("ACTIVE", pattern="^(ACTIVE|INACTIVE|ARCHIVED)$")


class TariffPlanCreate(TariffPlanBase):
    pass


class TariffPlanUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    market_id: Optional[str] = Field(None, max_length=32)
    version: Optional[int] = Field(None, ge=1)
    billing_type: Optional[str] = Field(None, pattern="^(PREPAID|POSTPAID)$")
    billing_frequency: Optional[str] = Field(None, max_length=32)
    billing_timing: Optional[str] = Field(None, pattern="^(IN_ADVANCE|IN_ARREARS)$")
    base_recurring_charge: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    setup_fee: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    usage_rate_per_gb: Optional[Decimal] = Field(None, ge=Decimal("0.00"))
    tax_rate_percent: Optional[Decimal] = Field(None, ge=Decimal("0.00"), le=Decimal("100.00"))
    discount_percentage: Optional[Decimal] = Field(None, ge=Decimal("0.00"), le=Decimal("100.00"))
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    status: Optional[str] = Field(None, pattern="^(ACTIVE|INACTIVE|ARCHIVED)$")


class TariffPlanResponse(TariffPlanBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TariffCalculationRequest(BaseModel):
    tariff_code: str
    usage_gb: Optional[float] = 0.0
    apply_setup_fee: Optional[bool] = False
    voucher_code: Optional[str] = None


class TariffCalculationResponse(BaseModel):
    tariff_code: str
    service_plan_code: str
    currency: str
    base_recurring_charge: Decimal
    setup_fee: Decimal
    usage_gb: float
    usage_rate_per_gb: Decimal
    usage_charge: Decimal
    gross_subtotal: Decimal
    plan_discount_amount: Decimal
    voucher_discount_amount: Decimal
    taxable_amount: Decimal
    tax_rate_percent: Decimal
    tax_amount: Decimal
    total_amount_payable: Decimal
