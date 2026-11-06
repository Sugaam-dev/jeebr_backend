"""
app/services/cpq_service.py

SentinelOS Enterprise CPQ (Configure, Price, Quote) & Omnichannel Sales CRM Engine.
Manages end-to-end B2B telecom sales cycles:
1. Lead generation, scoring, and fiber feasibility assessment.
2. Bandwidth configuration with contention ratio and SLA tier multipliers.
3. Multi-year term discounting and finance approval governance.
4. One-click quotation acceptance and conversion to active CRMCustomer, BillingAccount,
   BillingSubscription, and initial activation Tax Invoice with balanced General Ledger entries.
"""

import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session

from app.models_cpq import (
    CRMLead, Quotation, QuotationItem, LeadSalesStage, LeadSource,
    FeasibilityStatus, QuotationStatus, SLATier, QuotationItemType
)
from app.models_crm import CRMCustomer, CRMAddress, CRMContact, AccountType, CustomerLifecycleStatus, HierarchyType
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingAccountStatus, BillingType,
    SubscriptionStatus, ChartOfAccounts, InvoiceLineItem
)
from app.models import Customer, Invoice
from app.services.sequence_service import generate_sequential_number, SequenceType
from app.services.tax_service import calculate_gst_breakdown, get_supplier_tax_profile
from app.services.billing_service import post_journal_entry


# SLA Tier Multipliers for Enterprise Leased Lines
SLA_MARKUPS = {
    SLATier.STANDARD_99_5: Decimal('1.00'),          # Standard 99.5%
    SLATier.PREMIUM_99_9: Decimal('1.15'),           # 99.9% Premium (+15%)
    SLATier.MISSION_CRITICAL_99_95: Decimal('1.30'), # 99.95% Mission Critical (+30%)
}

# Contention Ratio Multipliers (1:1 Dedicated Leased Line vs Shared)
CONTENTION_MULTIPLIERS = {
    "1:1": Decimal('1.00'),   # Dedicated Internet Access (DIA)
    "1:2": Decimal('0.75'),
    "1:4": Decimal('0.55'),
    "1:8": Decimal('0.35'),   # Shared Broadband
}


def create_lead(
    db: Session,
    company_name: str,
    contact_name: str,
    phone: str,
    email: Optional[str] = None,
    market_id: str = "mumbai",
    account_type: str = AccountType.ENTERPRISE,
    lead_source: str = LeadSource.WEBSITE,
    installation_address: Optional[str] = None,
    pincode: Optional[str] = None,
    estimated_mrr: Decimal = Decimal('0.00'),
    assigned_sales_rep_id: Optional[int] = None
) -> CRMLead:
    """Creates a new sales lead and computes initial AI/rule-based qualification score."""
    lead_count = db.query(CRMLead).count() + 1
    lead_code = f"LEAD-{market_id[:3].upper()}-{datetime.utcnow().year}-{lead_count:04d}"

    # Calculate lead score
    score = 50
    if account_type == AccountType.ENTERPRISE:
        score += 20
    if estimated_mrr > Decimal('10000.00'):
        score += 15
    if email and ("@gmail.com" not in email and "@yahoo.com" not in email):
        score += 10  # Corporate business domain
    if installation_address and pincode:
        score += 5
    score = min(100, max(0, score))

    new_lead = CRMLead(
        lead_code=lead_code,
        company_name=company_name,
        contact_name=contact_name,
        email=email,
        phone=phone,
        account_type=account_type,
        sales_stage=LeadSalesStage.NEW,
        lead_score=score,
        lead_source=lead_source,
        market_id=market_id,
        installation_address=installation_address,
        pincode=pincode,
        estimated_mrr=estimated_mrr,
        assigned_sales_rep_id=assigned_sales_rep_id
    )
    db.add(new_lead)
    db.commit()
    db.refresh(new_lead)
    return new_lead


def assess_fiber_feasibility(
    db: Session,
    lead_id: uuid.UUID,
    fiber_distance_meters: int,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None
) -> CRMLead:
    """
    Evaluates fiber infrastructure feasibility from nearest OLT/POP to subscriber premises.
    Updates lead stage and score based on physical delivery feasibility.
    """
    lead = db.query(CRMLead).filter(CRMLead.id == lead_id).first()
    if not lead:
        raise ValueError(f"Lead {lead_id} not found.")

    lead.fiber_distance_meters = fiber_distance_meters
    if latitude:
        lead.latitude = latitude
    if longitude:
        lead.longitude = longitude

    if fiber_distance_meters <= 350:
        lead.feasibility_status = FeasibilityStatus.FEASIBLE
        lead.lead_score = min(100, lead.lead_score + 15)
        lead.sales_stage = LeadSalesStage.FEASIBILITY_CHECK
    elif fiber_distance_meters <= 1200:
        lead.feasibility_status = FeasibilityStatus.HIGH_COST_BUILD
        lead.sales_stage = LeadSalesStage.FEASIBILITY_CHECK
    else:
        lead.feasibility_status = FeasibilityStatus.UNSERVICEABLE
        lead.lead_score = max(0, lead.lead_score - 30)

    db.commit()
    db.refresh(lead)
    return lead


def create_quotation(
    db: Session,
    lead_id: uuid.UUID,
    items_data: List[Dict[str, Any]],
    contract_term_months: int = 12,
    discount_pct: Decimal = Decimal('0.00'),
    setup_charges: Decimal = Decimal('0.00'),
    billing_frequency: str = "MONTHLY",
    valid_days: int = 30
) -> Quotation:
    """
    Configures and prices an enterprise proposal.
    Calculates bandwidth rates with contention ratio & SLA multipliers,
    checks discount governance thresholds, and computes required tax.
    """
    lead = db.query(CRMLead).filter(CRMLead.id == lead_id).first()
    if not lead:
        raise ValueError(f"Lead {lead_id} not found.")

    mkt = lead.market_id or "mumbai"
    q_count = db.query(Quotation).count() + 1
    q_code = f"QT-{mkt[:3].upper()}-{datetime.utcnow().year}-{q_count:04d}"

    base_mrr = Decimal('0.00')
    line_items: List[QuotationItem] = []

    for item in items_data:
        bandwidth = item.get("bandwidth_mbps", 100)
        contention = item.get("contention_ratio", "1:1")
        sla = item.get("sla_tier", SLATier.STANDARD_99_5)
        raw_price = Decimal(str(item.get("unit_price", "5000.00")))
        qty = Decimal(str(item.get("quantity", "1.00")))

        # Apply Contention and SLA Multipliers
        c_mult = CONTENTION_MULTIPLIERS.get(contention, Decimal('1.00'))
        s_mult = SLA_MARKUPS.get(sla, Decimal('1.00'))
        computed_unit_price = (raw_price * c_mult * s_mult).quantize(Decimal('0.01'))
        subtotal = (computed_unit_price * qty).quantize(Decimal('0.01'))

        base_mrr += subtotal

        q_item = QuotationItem(
            item_type=item.get("item_type", QuotationItemType.BANDWIDTH),
            plan_id=item.get("plan_id", f"PLAN-DIA-{bandwidth}M"),
            description=item.get("description", f"{bandwidth} Mbps Leased Line ({contention}, SLA {sla})"),
            bandwidth_mbps=bandwidth,
            contention_ratio=contention,
            sla_tier=sla,
            quantity=qty,
            unit_price=computed_unit_price,
            subtotal=subtotal
        )
        line_items.append(q_item)

    # Contract Term Multi-Year Discount
    effective_discount_pct = Decimal(str(discount_pct))
    if contract_term_months >= 36 and effective_discount_pct == Decimal('0.00'):
        effective_discount_pct = Decimal('10.00')
    elif contract_term_months >= 24 and effective_discount_pct == Decimal('0.00'):
        effective_discount_pct = Decimal('5.00')

    discount_amount = (base_mrr * (effective_discount_pct / Decimal('100.00'))).quantize(Decimal('0.01'))
    discounted_mrr = base_mrr - discount_amount
    otc = Decimal(str(setup_charges))

    # Initial taxable value = 1 month MRR + Setup Charges (OTC)
    taxable_first_month = discounted_mrr + otc
    tax_amount = (taxable_first_month * Decimal('0.18')).quantize(Decimal('0.01'))
    total_first_payment = taxable_first_month + tax_amount

    # Approval Threshold: Discounts > 15% or terms < 12 months require executive sign-off
    requires_approval = (effective_discount_pct > Decimal('15.00') or contract_term_months < 12)
    initial_status = QuotationStatus.PENDING_APPROVAL if requires_approval else QuotationStatus.DRAFT
    approval_status = "PENDING" if requires_approval else "AUTO_APPROVED"

    quotation = Quotation(
        quotation_code=q_code,
        lead_id=lead.id,
        market_id=mkt,
        status=initial_status,
        contract_term_months=contract_term_months,
        billing_frequency=billing_frequency,
        base_mrr=base_mrr,
        discount_pct=effective_discount_pct,
        discounted_mrr=discounted_mrr,
        setup_charges=otc,
        tax_amount=tax_amount,
        total_first_payment=total_first_payment,
        requires_approval=requires_approval,
        approval_status=approval_status,
        valid_until=datetime.utcnow() + timedelta(days=valid_days),
        terms_and_conditions="Payment strictly within 15 days of invoice. 99.9% uptime guaranteed under standard MTTR 4 hours."
    )
    db.add(quotation)
    db.flush()

    for qi in line_items:
        qi.quotation_id = quotation.id
        db.add(qi)

    lead.sales_stage = LeadSalesStage.PROPOSAL_SENT
    lead.estimated_mrr = discounted_mrr

    db.commit()
    db.refresh(quotation)
    return quotation


def approve_quotation(
    db: Session,
    quotation_id: uuid.UUID,
    approved_by_id: int,
    notes: Optional[str] = None
) -> Quotation:
    """Finance/Commercial VP approves quotation that exceeded automatic discount thresholds."""
    q = db.query(Quotation).filter(Quotation.id == quotation_id).first()
    if not q:
        raise ValueError(f"Quotation {quotation_id} not found.")

    q.approval_status = "APPROVED"
    q.status = QuotationStatus.APPROVED
    q.approved_by_id = approved_by_id
    q.approval_notes = notes

    db.commit()
    db.refresh(q)
    return q


def convert_quotation_to_customer(
    db: Session,
    quotation_id: uuid.UUID,
    user_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Converts an approved/accepted Quotation into:
    1. Active CRMCustomer & Legacy Customer Bridge.
    2. Active BillingAccount.
    3. Active BillingSubscription(s).
    4. First statutory Tax Invoice (Setup Charges + Month 1 MRR + 18% GST).
    5. Balanced General Ledger double entries.
    6. Lead transitioned to WON.
    """
    q = db.query(Quotation).filter(Quotation.id == quotation_id).first()
    if not q:
        raise ValueError(f"Quotation {quotation_id} not found.")

    if q.status not in (QuotationStatus.APPROVED, QuotationStatus.ACCEPTED, QuotationStatus.DRAFT):
        raise ValueError(f"Cannot convert quotation with status {q.status}.")

    lead = q.lead
    if not lead:
        raise ValueError(f"Quotation {q.quotation_code} is not linked to an active sales lead.")

    mkt = q.market_id or "mumbai"

    # 1. Create CRMCustomer
    cust_code = generate_sequential_number(db, SequenceType.CUSTOMER, mkt)
    crm_cust = CRMCustomer(
        customer_number=cust_code,
        legal_name=lead.company_name,
        account_type=lead.account_type,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id=mkt,
        hierarchy_type=HierarchyType.STANDALONE,
        billing_currency="INR"
    )
    db.add(crm_cust)
    db.flush()

    # Add Contact
    contact = CRMContact(
        crm_customer_id=crm_cust.id,
        first_name=lead.contact_name,
        email=lead.email,
        phone=lead.phone,
        role="PRIMARY"
    )
    db.add(contact)

    # Add Address
    addr = CRMAddress(
        crm_customer_id=crm_cust.id,
        address_type="INSTALLATION",
        address_line1=lead.installation_address or f"Corporate Office, {mkt.title()}",
        postal_code=lead.pincode or "400001",
        city=mkt.title(),
        state="Maharashtra" if mkt.lower() == "mumbai" else "West Bengal"
    )
    db.add(addr)

    # 2. Create Legacy Operational Customer Bridge
    legacy_cust = Customer(
        name=lead.company_name,
        phone=lead.phone,
        email=lead.email or f"{cust_code.lower()}@customer.sentinelos.com",
        customer_code=cust_code,
        locality=mkt.title(),
        segment="Enterprise",
        customer_type="Postpaid",
        status="Active",
        market_id=mkt,
        plan_name=q.items[0].description if q.items else "Enterprise Dedicated Leased Line",
        plan_price=float(q.discounted_mrr),
        service_address=lead.installation_address or f"Corporate Office, {mkt.title()}"
    )
    db.add(legacy_cust)
    db.flush()
    crm_cust.legacy_customer_id = legacy_cust.id

    # 3. Create BillingAccount
    ba_code = generate_sequential_number(db, SequenceType.BILLING_ACCOUNT, mkt)
    billing_acct = BillingAccount(
        account_number=ba_code,
        customer_id=legacy_cust.id,
        crm_customer_id=crm_cust.id,
        market_id=mkt,
        billing_type=BillingType.POSTPAID,
        currency="INR",
        balance=Decimal('0.00'),
        credit_limit=Decimal('50000.00'),
        status=BillingAccountStatus.ACTIVE,
        payment_term_days=15
    )
    db.add(billing_acct)
    db.flush()

    # 4. Create BillingSubscription for primary item
    now = datetime.utcnow()
    period_end = now + timedelta(days=30)
    primary_item = q.items[0] if q.items else None

    sub_code = generate_sequential_number(db, SequenceType.SUBSCRIPTION, mkt)
    subscription = BillingSubscription(
        subscription_code=sub_code,
        billing_account_id=billing_acct.id,
        customer_id=legacy_cust.id,
        crm_customer_id=crm_cust.id,
        market_id=mkt,
        plan_id=primary_item.plan_id if primary_item else "PLAN-ENTERPRISE-DIA",
        plan_name=primary_item.description if primary_item else "Enterprise Leased Line",
        billing_type=BillingType.POSTPAID,
        billing_frequency=q.billing_frequency,
        unit_price=q.discounted_mrr,
        currency="INR",
        status=SubscriptionStatus.ACTIVE,
        start_date=now,
        current_period_start=now,
        current_period_end=period_end,
        next_billing_date=period_end
    )
    db.add(subscription)
    db.flush()

    # 5. Generate Initial Activation Tax Invoice (Month 1 MRR + Setup OTC + 18% GST)
    tax_calc = calculate_gst_breakdown(
        taxable_amount=q.discounted_mrr + q.setup_charges,
        market_id=mkt,
        customer_state=addr.state,
        sac_code="998422"
    )
    supplier = get_supplier_tax_profile(mkt)
    inv_code = generate_sequential_number(db, SequenceType.INVOICE, mkt)
    total_invoice = (q.discounted_mrr + q.setup_charges) + tax_calc.tax_amount

    inv = Invoice(
        market_id=mkt,
        invoice_code=inv_code,
        customer_id=legacy_cust.id,
        billing_account_id=billing_acct.id,
        subscription_id=subscription.id,
        plan_name=subscription.plan_name,
        transaction_type="Activation & Installation Invoice",
        payment_method="NET_BANKING",
        billed_amount=float(total_invoice),
        expected_amount=float(total_invoice),
        due_date=now + timedelta(days=15),
        status="Issued",
        currency="INR",
        subtotal=q.discounted_mrr + q.setup_charges,
        tax_amount=tax_calc.tax_amount,
        discount_amount=Decimal('0.00'),
        total_amount=total_invoice,
        paid_amount=Decimal('0.00'),
        outstanding_amount=total_invoice,
        billing_period_start=now,
        billing_period_end=period_end,
        tax_treatment=tax_calc.tax_treatment,
        cgst_amount=tax_calc.cgst_amount,
        sgst_amount=tax_calc.sgst_amount,
        igst_amount=tax_calc.igst_amount,
        place_of_supply=tax_calc.place_of_supply,
        supplier_gstin=supplier.gstin,
        notes=f"Converted from accepted quotation {q.quotation_code}.",
        idempotency_key=f"CONVERT-INV-{q.id}"
    )
    db.add(inv)
    db.flush()

    # Invoice Line Items
    if q.setup_charges > Decimal('0.00'):
        otc_tax = (q.setup_charges * Decimal('0.18')).quantize(Decimal('0.01'))
        db.add(InvoiceLineItem(
            invoice_id=inv.id,
            description="One-Time Setup, Fiber Pull & Optical CPE Installation Charges",
            quantity=Decimal('1.00'),
            unit_price=q.setup_charges,
            amount=q.setup_charges,
            tax_rate=Decimal('18.00'),
            tax_amount=otc_tax,
            total_amount=q.setup_charges + otc_tax,
            currency="INR"
        ))

    mrr_tax = (q.discounted_mrr * Decimal('0.18')).quantize(Decimal('0.01'))
    db.add(InvoiceLineItem(
        invoice_id=inv.id,
        subscription_id=subscription.id,
        description=f"Advance Month 1 Recurring Broadband Charge - {subscription.plan_name}",
        quantity=Decimal('1.00'),
        unit_price=q.discounted_mrr,
        amount=q.discounted_mrr,
        tax_rate=Decimal('18.00'),
        tax_amount=mrr_tax,
        total_amount=q.discounted_mrr + mrr_tax,
        currency="INR"
    ))

    # General Ledger Balanced Double Entries
    journal_lines = [
        {
            "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
            "debit": total_invoice,
            "credit": Decimal('0.00'),
            "description": f"Accounts Receivable - Initial Invoice {inv_code}"
        },
        {
            "account_code": ChartOfAccounts.BROADBAND_REVENUE,
            "debit": Decimal('0.00'),
            "credit": q.discounted_mrr + q.setup_charges,
            "description": f"Broadband Revenue & OTC - Initial Invoice {inv_code}"
        },
        {
            "account_code": ChartOfAccounts.TAX_PAYABLE_GST,
            "debit": Decimal('0.00'),
            "credit": tax_calc.tax_amount,
            "description": f"GST Output Tax Payable ({tax_calc.tax_treatment}) - Invoice {inv_code}"
        }
    ]
    post_journal_entry(
        db=db,
        market_id=mkt,
        reference_type="INVOICE",
        reference_id=str(inv.id),
        description=f"First Activation Invoice {inv_code} for {crm_cust.legal_name}",
        lines=journal_lines
    )

    # 6. Update Lead and Quotation Statuses
    q.status = QuotationStatus.CONVERTED
    q.crm_customer_id = crm_cust.id
    lead.sales_stage = LeadSalesStage.WON
    lead.converted_crm_customer_id = crm_cust.id

    db.commit()
    db.refresh(crm_cust)
    db.refresh(inv)

    return {
        "status": "CONVERTED",
        "lead_id": str(lead.id),
        "quotation_id": str(q.id),
        "crm_customer_id": str(crm_cust.id),
        "customer_number": crm_cust.customer_number,
        "billing_account_number": billing_acct.account_number,
        "subscription_code": subscription.subscription_code,
        "initial_invoice_code": inv.invoice_code,
        "total_first_payment": float(total_invoice),
    }
