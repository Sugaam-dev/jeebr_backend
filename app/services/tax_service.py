"""
app/services/tax_service.py

Dynamic GST & Tax Calculation Engine for SentinelOS ISP Billing.
Enforces Indian GST rules, distinguishing Intra-State (CGST + SGST) from Inter-State (IGST) supplies.
Preserves fixed-precision Decimal arithmetic with ROUND_HALF_UP rounding.
Stores tax metadata on issued financial records for audit trail immutability.
"""

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple

# Statutory SAC classification for Wired Broadband Internet Telecommunications Service
DEFAULT_HSN_SAC_CODE = "998422"

# Indian State GST Codes (Standard 2-digit prefix)
STATE_CODE_MAP = {
    "maharashtra": "27",
    "mumbai": "27",
    "west bengal": "19",
    "kolkata": "19",
    "delhi": "07",
    "karnataka": "29",
    "bengaluru": "29",
    "bangalore": "29",
    "tamil nadu": "33",
    "chennai": "33",
    "telangana": "36",
    "hyderabad": "36",
    "gujarat": "24",
    "ahmedabad": "24",
    "uttar pradesh": "09",
    "noida": "09",
    "haryana": "06",
    "gurugram": "06",
}

# Standard Indian GST rate for telecom services is 18.00%
STANDARD_TELECOM_GST_RATE = Decimal("18.00")
HALF_GST_RATE = Decimal("9.00")


@dataclass
class SupplierTaxProfile:
    legal_name: str
    trade_name: str
    gstin: str
    pan: str
    state_code: str
    state_name: str
    address: str
    jurisdiction: str


@dataclass
class TaxComputation:
    subtotal: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    tax_treatment: str          # INTRA_STATE or INTER_STATE
    cgst_rate: Decimal
    cgst_amount: Decimal
    sgst_rate: Decimal
    sgst_amount: Decimal
    igst_rate: Decimal
    igst_amount: Decimal
    total_tax: Decimal
    total_amount: Decimal
    currency: str
    place_of_supply: str
    supplier_gstin: str
    supplier_legal_name: str
    customer_gstin: Optional[str] = None
    sac_code: str = DEFAULT_HSN_SAC_CODE


def get_supplier_tax_profile() -> SupplierTaxProfile:
    """Read verified supplier tax identity from server environment or canonical configuration."""
    return SupplierTaxProfile(
        legal_name=os.getenv("SUPPLIER_LEGAL_NAME", "SentinelOS Broadband Network (PMRG Solution LLP)").strip(),
        trade_name=os.getenv("SUPPLIER_TRADE_NAME", "SentinelOS Broadband").strip(),
        gstin=os.getenv("SUPPLIER_GSTIN", "27AAACP0123M1Z5").strip(),
        pan=os.getenv("SUPPLIER_PAN", "AAACP0123M").strip(),
        state_code=os.getenv("SUPPLIER_STATE_CODE", "27").strip(),
        state_name=os.getenv("SUPPLIER_STATE_NAME", "Maharashtra").strip(),
        address=os.getenv("SUPPLIER_ADDRESS", "Bandra Kurla Complex, Mumbai, Maharashtra 400051").strip(),
        jurisdiction=os.getenv("SUPPLIER_JURISDICTION", "Maharashtra, India").strip(),
    )


def resolve_state_code(identifier: Optional[str]) -> str:
    """Normalize market, city, or state name into a 2-digit Indian GST state code."""
    if not identifier:
        return "27"  # Default to primary Maharashtra hub
    cleaned = identifier.strip().lower()
    
    # Direct 2-digit code
    if len(cleaned) == 2 and cleaned.isdigit():
        return cleaned

    # 15-character GSTIN format: first 2 digits are state code
    if len(cleaned) == 15 and cleaned[:2].isdigit():
        return cleaned[:2]

    # Prefix match if in format '27-maharashtra'
    if "-" in cleaned and cleaned.split("-")[0].isdigit():
        return cleaned.split("-")[0].strip()

    return STATE_CODE_MAP.get(cleaned, "27")


def resolve_place_of_supply(market_id: str, address_state: Optional[str] = None, customer_gstin: Optional[str] = None) -> str:
    """Derives authoritative place-of-supply string (e.g. '27-Maharashtra')."""
    raw = customer_gstin or address_state or market_id
    code = resolve_state_code(raw)
    names = {
        "27": "Maharashtra",
        "19": "West Bengal",
        "07": "Delhi",
        "29": "Karnataka",
        "33": "Tamil Nadu",
        "36": "Telangana",
        "24": "Gujarat",
        "09": "Uttar Pradesh",
        "06": "Haryana",
    }
    name = names.get(code, "Maharashtra")
    return f"{code}-{name}"


def compute_gst(
    subtotal: Decimal,
    discount_amount: Decimal = Decimal("0.00"),
    market_id: str = "mumbai",
    customer_state: Optional[str] = None,
    customer_gstin: Optional[str] = None,
    currency: str = "INR"
) -> TaxComputation:
    """
    Computes precise GST according to Indian statutory rules:
    - Intra-State (Supplier State == Place of Supply): CGST 9% + SGST 9%
    - Inter-State (Supplier State != Place of Supply): IGST 18%
    Rounding: Decimal('0.01') with ROUND_HALF_UP applied to each statutory tax component.
    """
    supplier = get_supplier_tax_profile()
    pos = resolve_place_of_supply(market_id, customer_state, customer_gstin)
    pos_code = pos.split("-")[0].strip()

    taxable = max(Decimal("0.00"), subtotal - discount_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Determine tax treatment
    if pos_code == supplier.state_code:
        tax_treatment = "INTRA_STATE"
        cgst_rate = HALF_GST_RATE
        sgst_rate = HALF_GST_RATE
        igst_rate = Decimal("0.00")

        cgst_amount = (taxable * cgst_rate / Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        sgst_amount = (taxable * sgst_rate / Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        igst_amount = Decimal("0.00")
        total_tax = cgst_amount + sgst_amount
    else:
        tax_treatment = "INTER_STATE"
        cgst_rate = Decimal("0.00")
        sgst_rate = Decimal("0.00")
        igst_rate = STANDARD_TELECOM_GST_RATE

        cgst_amount = Decimal("0.00")
        sgst_amount = Decimal("0.00")
        igst_amount = (taxable * igst_rate / Decimal("100.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_tax = igst_amount

    total_amount = (taxable + total_tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return TaxComputation(
        subtotal=subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        discount_amount=discount_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        taxable_amount=taxable,
        tax_treatment=tax_treatment,
        cgst_rate=cgst_rate,
        cgst_amount=cgst_amount,
        sgst_rate=sgst_rate,
        sgst_amount=sgst_amount,
        igst_rate=igst_rate,
        igst_amount=igst_amount,
        total_tax=total_tax,
        total_amount=total_amount,
        currency=currency,
        place_of_supply=pos,
        supplier_gstin=supplier.gstin,
        supplier_legal_name=supplier.legal_name,
        customer_gstin=customer_gstin,
        sac_code=DEFAULT_HSN_SAC_CODE
    )
