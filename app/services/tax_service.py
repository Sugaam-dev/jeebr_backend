"""
app/services/tax_service.py

Dynamic GST & Tax Calculation Engine for SentinelOS ISP Billing.
Enforces Indian GST rules, distinguishing Intra-State (CGST + SGST) from Inter-State (IGST) supplies.
Preserves fixed-precision Decimal arithmetic with ROUND_HALF_UP rounding.
Stores tax metadata on issued financial records for audit trail immutability.

Statutory Alignment:
- Under Section 12(11)(a) of the IGST Act, 2017:
  Place of supply for telecommunication services via fixed line, leased circuits,
  or internet broadband is strictly the location where the line/circuit is installed.
"""

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session

# Statutory SAC classification for Wired Broadband Internet Telecommunications Service
DEFAULT_HSN_SAC_CODE = "998422"

# Comprehensive Indian Telecom SAC and HSN Codes
TELECOM_SAC_CODES = {
    "INTERNET": "998422",          # Internet telecommunication services (broadband, ILL)
    "ROUTER_RENTAL": "997331",     # Leasing or rental of modems, ONTs, routers
    "INSTALLATION": "998729",      # Installation, setup, maintenance, and fiber repair
    "VOIP_TELEPHONY": "998413",    # Fixed telephony and voice services
    "CPE_HARDWARE": "8517",        # Physical sale of ONT / Wi-Fi router (HSN)
}

# Complete Indian State & Union Territory GST Codes (Standard 2-digit prefix)
STATE_CODE_MAP = {
    "jammu and kashmir": "01",
    "himachal pradesh": "02",
    "punjab": "03",
    "chandigarh": "04",
    "uttarakhand": "05",
    "haryana": "06",
    "gurugram": "06",
    "delhi": "07",
    "new delhi": "07",
    "rajasthan": "08",
    "jaipur": "08",
    "uttar pradesh": "09",
    "noida": "09",
    "lucknow": "09",
    "bihar": "10",
    "patna": "10",
    "sikkim": "11",
    "arunachal pradesh": "12",
    "nagaland": "13",
    "manipur": "14",
    "mizoram": "15",
    "tripura": "16",
    "meghalaya": "17",
    "assam": "18",
    "guwahati": "18",
    "west bengal": "19",
    "kolkata": "19",
    "jharkhand": "20",
    "ranchi": "20",
    "odisha": "21",
    "bhubaneswar": "21",
    "chhattisgarh": "22",
    "madhya pradesh": "23",
    "bhopal": "23",
    "indore": "23",
    "gujarat": "24",
    "ahmedabad": "24",
    "surat": "24",
    "daman and diu": "26",
    "dadra and nagar haveli": "26",
    "maharashtra": "27",
    "mumbai": "27",
    "pune": "27",
    "nagpur": "27",
    "andhra pradesh": "28",
    "karnataka": "29",
    "bengaluru": "29",
    "bangalore": "29",
    "goa": "30",
    "lakshadweep": "31",
    "kerala": "32",
    "kochi": "32",
    "thiruvananthapuram": "32",
    "tamil nadu": "33",
    "chennai": "33",
    "puducherry": "34",
    "pondicherry": "34",
    "andaman and nicobar islands": "35",
    "telangana": "36",
    "hyderabad": "36",
    "andhra pradesh (new)": "37",
    "ladakh": "38",
}

STATE_CODE_TO_NAME = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh (New)",
    "38": "Ladakh",
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

    @property
    def tax_amount(self) -> Decimal:
        return self.total_tax


def get_supplier_tax_profile(market_id: Optional[str] = None) -> SupplierTaxProfile:
    """Read verified supplier tax identity from server environment or canonical configuration."""
    target_mkt = (market_id or "mumbai").lower()
    is_mumbai = (target_mkt == "mumbai" or "mumbai" in target_mkt)
    state_code = "27" if is_mumbai else "19"
    state_name = "Maharashtra" if is_mumbai else "West Bengal"
    gstin = os.getenv("SUPPLIER_GSTIN", "27AAACP0123A1Z1" if is_mumbai else "19AAACP0123A1Z8").strip()
    pan = os.getenv("SUPPLIER_PAN", "AAACP0123A").strip()

    return SupplierTaxProfile(
        legal_name=os.getenv("SUPPLIER_LEGAL_NAME", "SentinelOS Broadband Network (PMRG Solution LLP)").strip(),
        trade_name=os.getenv("SUPPLIER_TRADE_NAME", "SentinelOS Broadband").strip(),
        gstin=gstin,
        pan=pan,
        state_code=state_code,
        state_name=state_name,
        address=os.getenv("SUPPLIER_ADDRESS", f"Bandra Kurla Complex, {state_name}").strip(),
        jurisdiction=os.getenv("SUPPLIER_JURISDICTION", f"{state_name}, India").strip(),
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


def resolve_place_of_supply(
    market_id: str,
    installation_state: Optional[str] = None,
    billing_state: Optional[str] = None,
    customer_gstin: Optional[str] = None
) -> str:
    """
    Derives authoritative Place of Supply adhering to Section 12(11)(a) of the IGST Act, 2017:
    1. Physical installation premises state takes absolute priority for fixed telecommunication lines.
    2. Fallback to customer billing address state if installation state is unavailable.
    3. Fallback to customer GSTIN state prefix.
    4. Fallback to operational market_id.
    """
    raw = installation_state or billing_state or customer_gstin or market_id
    code = resolve_state_code(raw)
    name = STATE_CODE_TO_NAME.get(code, "Maharashtra")
    return f"{code}-{name}"


def resolve_installation_state(db: Session, customer_id: int) -> Optional[str]:
    """
    Inspects CRMAddress for the customer to find the physical INSTALLATION address state.
    """
    from app.models_crm import CRMAddress, AddressType, CRMCustomer
    from app.models import Customer

    # First attempt: Check via CRMCustomer installation address
    crm_cust = db.query(CRMCustomer).filter(
        (CRMCustomer.legacy_customer_id == customer_id) | (CRMCustomer.customer_number == str(customer_id))
    ).first()

    if crm_cust:
        addr = db.query(CRMAddress).filter(
            CRMAddress.crm_customer_id == crm_cust.id,
            CRMAddress.address_type == AddressType.INSTALLATION,
            CRMAddress.is_archived == False
        ).first()
        if addr and addr.state:
            return addr.state.strip()

    # Second attempt: check any non-archived CRMAddress
    if crm_cust:
        any_addr = db.query(CRMAddress).filter(
            CRMAddress.crm_customer_id == crm_cust.id,
            CRMAddress.is_archived == False
        ).first()
        if any_addr and any_addr.state:
            return any_addr.state.strip()

    # Third attempt: Legacy Customer model attributes
    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if cust:
        state = getattr(cust, 'state', None)
        if state:
            return state.strip()

    return None


def compute_gst(
    subtotal: Decimal,
    discount_amount: Decimal = Decimal("0.00"),
    market_id: str = "mumbai",
    customer_state: Optional[str] = None,
    customer_gstin: Optional[str] = None,
    currency: str = "INR",
    sac_code: str = DEFAULT_HSN_SAC_CODE
) -> TaxComputation:
    """
    Computes precise GST according to Indian statutory rules:
    - Intra-State (Supplier State == Place of Supply): CGST 9% + SGST 9%
    - Inter-State (Supplier State != Place of Supply): IGST 18%
    Rounding: Decimal('0.01') with ROUND_HALF_UP applied to each statutory tax component.
    """
    supplier = get_supplier_tax_profile()
    pos = resolve_place_of_supply(
        market_id=market_id,
        installation_state=customer_state,
        customer_gstin=customer_gstin
    )
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
        sac_code=sac_code
    )


def calculate_gst_breakdown(
    taxable_amount: Decimal,
    market_id: str = "mumbai",
    customer_state: Optional[str] = None,
    customer_gstin: Optional[str] = None,
    sac_code: str = DEFAULT_HSN_SAC_CODE
) -> TaxComputation:
    """Convenience alias for compute_gst."""
    return compute_gst(
        subtotal=taxable_amount,
        discount_amount=Decimal("0.00"),
        market_id=market_id,
        customer_state=customer_state,
        customer_gstin=customer_gstin,
        sac_code=sac_code
    )
