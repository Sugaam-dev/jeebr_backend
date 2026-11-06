"""
app/tests/test_phase2_enterprise_crm_and_billing.py

Comprehensive Enterprise Test Suite for Phase 2:
1. Corporate Account Hierarchy (Parent-Child, Cycle Detection & Multi-Site Rollups).
2. Aggregated Parent Consolidated Invoicing with Balanced General Ledger Entries.
3. Omnichannel Sales CRM & CPQ (Configure, Price, Quote) Pipeline & Conversion.
4. Statutory GST e-Invoicing (INV-01, 64-char SHA-256 IRN, signed QR, Dynamic B2C UPI QR & PDF generation).
5. UPI AutoPay Recurring Mandate Lifecycle (Registration, RBI Pre-Debit Notification, Execution, GL Settlement & Revocation).
"""

import uuid
import pytest
from decimal import Decimal
from datetime import datetime, timedelta

from app.models_crm import CRMCustomer, CRMAddress, CRMContact, AccountType, CustomerLifecycleStatus, HierarchyType
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingAccountStatus, BillingType,
    SubscriptionStatus, InvoicingPreference, ChartOfAccounts
)
from app.services.ledger_service import verify_ledger_balancing
from app.models_cpq import (
    CRMLead, Quotation, QuotationItem, LeadSalesStage, FeasibilityStatus,
    QuotationStatus, SLATier, QuotationItemType
)
from app.models_autopay import PaymentMandate, PreDebitNotification, MandateType, MandateStatus
from app.models import Customer, Invoice
from app.services.hierarchy_service import (
    link_customer_hierarchy, get_corporate_hierarchy_tree, generate_parent_consolidated_invoice
)
from app.services.cpq_service import (
    create_lead, assess_fiber_feasibility, create_quotation, approve_quotation, convert_quotation_to_customer
)
from app.services.einvoice_service import (
    compute_canonical_irn_hash, generate_inv01_payload, is_einvoice_applicable,
    process_einvoice_registration, cancel_einvoice, generate_dynamic_b2c_upi_qr_payload
)
from app.services.autopay_service import (
    register_mandate, issue_pre_debit_notification, execute_mandate_auto_debit, revoke_mandate
)
from app.services.invoice_document import generate_invoice_pdf


# ─── 1. Corporate Account Hierarchy Tests ────────────────────────────────────

def test_corporate_hierarchy_linking_and_cycle_prevention(db_session):
    """Verifies parent-child multi-site linking and cycle prevention."""
    # Parent Holding Company
    parent = CRMCustomer(
        customer_number="CUST-CORP-PARENT-01",
        legal_name="Apex Holdings India Ltd",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        hierarchy_type=HierarchyType.HOLDING_COMPANY
    )
    # Child 1: Bandra Branch
    child1 = CRMCustomer(
        customer_number="CUST-CORP-CHILD-01",
        legal_name="Apex Bandra Branch",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        hierarchy_type=HierarchyType.BRANCH_LOCATION
    )
    # Child 2: Andheri Branch
    child2 = CRMCustomer(
        customer_number="CUST-CORP-CHILD-02",
        legal_name="Apex Andheri R&D Center",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        hierarchy_type=HierarchyType.BRANCH_LOCATION
    )
    db_session.add_all([parent, child1, child2])
    db_session.commit()

    # Link child1 to parent
    link_customer_hierarchy(db_session, child1.id, parent.id, hierarchy_type=HierarchyType.BRANCH_LOCATION)
    # Link child2 to parent
    link_customer_hierarchy(db_session, child2.id, parent.id, hierarchy_type=HierarchyType.BRANCH_LOCATION)

    db_session.refresh(parent)
    db_session.refresh(child1)
    db_session.refresh(child2)

    assert child1.parent_customer_id == parent.id
    assert child2.parent_customer_id == parent.id
    assert len(parent.subsidiaries) == 2

    # Verify cycle prevention: attempting to make parent a child of child1 must raise ValueError
    with pytest.raises(ValueError, match="Circular hierarchy detected"):
        link_customer_hierarchy(db_session, parent.id, child1.id)

    # Self-linking prevention
    with pytest.raises(ValueError, match="cannot be set as its own parent"):
        link_customer_hierarchy(db_session, parent.id, parent.id)


def test_corporate_hierarchy_tree_rollup(db_session):
    """Verifies recursive hierarchy traversal with aggregated MRR and branch counts."""
    parent = CRMCustomer(
        customer_number="CUST-TREE-PARENT",
        legal_name="Global Tech Conglomerate",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        hierarchy_type=HierarchyType.HOLDING_COMPANY
    )
    db_session.add(parent)
    db_session.flush()

    child = CRMCustomer(
        customer_number="CUST-TREE-BRANCH-1",
        legal_name="Global Tech Mumbai SEZ",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        hierarchy_type=HierarchyType.BRANCH_LOCATION,
        parent_customer_id=parent.id
    )
    db_session.add(child)
    db_session.commit()

    # Create Billing Accounts
    parent_ba = BillingAccount(
        account_number="BA-TREE-P01",
        crm_customer_id=parent.id,
        market_id="mumbai",
        balance=Decimal('0.00'),
        status=BillingAccountStatus.ACTIVE
    )
    child_ba = BillingAccount(
        account_number="BA-TREE-C01",
        crm_customer_id=child.id,
        parent_billing_account_id=parent_ba.id,
        market_id="mumbai",
        balance=Decimal('2500.00'),
        invoicing_preference=InvoicingPreference.CONSOLIDATED,
        status=BillingAccountStatus.ACTIVE
    )
    db_session.add_all([parent_ba, child_ba])
    db_session.commit()

    # Add active subscription to child
    sub = BillingSubscription(
        subscription_code="SUB-TREE-C01",
        billing_account_id=child_ba.id,
        customer_id=1,
        crm_customer_id=child.id,
        market_id="mumbai",
        plan_id="PLAN-DIA-1G",
        plan_name="1 Gbps Dedicated Leased Line",
        unit_price=Decimal('15000.00'),
        status=SubscriptionStatus.ACTIVE,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() + timedelta(days=30)
    )
    db_session.add(sub)
    db_session.commit()

    tree = get_corporate_hierarchy_tree(db_session, parent.id)

    assert tree["customer_number"] == "CUST-TREE-PARENT"
    assert tree["rollup_metrics"]["total_nodes_count"] == 2
    assert tree["rollup_metrics"]["active_subscriptions_count"] == 1
    assert tree["rollup_metrics"]["total_mrr"] == 15000.0
    assert tree["rollup_metrics"]["total_balance"] == 2500.0
    assert len(tree["children"]) == 1
    assert tree["children"][0]["customer_number"] == "CUST-TREE-BRANCH-1"


def test_parent_consolidated_multi_site_invoicing(db_session):
    """Verifies aggregated master invoicing across parent and branches with balanced GL entries."""
    parent = CRMCustomer(
        customer_number="CUST-CONSOL-HQ",
        legal_name="Tata Enterprises Ltd",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        hierarchy_type=HierarchyType.HOLDING_COMPANY,
        tax_id="27AAACT0001A1Z5"
    )
    db_session.add(parent)
    db_session.commit()

    # Address
    addr = CRMAddress(
        crm_customer_id=parent.id,
        address_type="BILLING",
        address_line1="Bandra Kurla Complex",
        city="Mumbai",
        postal_code="400051",
        state="Maharashtra"
    )
    db_session.add(addr)

    # Legacy Customer
    leg_cust = Customer(
        name="Tata Enterprises Ltd",
        email="billing@tata.com",
        phone="9820011223",
        customer_code="TATA-LEGACY-01",
        market_id="mumbai",
        locality="Bandra Kurla Complex",
        segment="Enterprise",
        customer_type="Postpaid",
        plan_name="Enterprise Dedicated DIA"
    )
    db_session.add(leg_cust)
    db_session.commit()

    parent_ba = BillingAccount(
        account_number="BA-TATA-HQ",
        customer_id=leg_cust.id,
        crm_customer_id=parent.id,
        market_id="mumbai",
        balance=Decimal('0.00'),
        status=BillingAccountStatus.ACTIVE
    )
    db_session.add(parent_ba)
    db_session.commit()

    # HQ Subscription: 500 Mbps Link
    hq_sub = BillingSubscription(
        subscription_code="SUB-TATA-HQ-500M",
        billing_account_id=parent_ba.id,
        customer_id=leg_cust.id,
        crm_customer_id=parent.id,
        market_id="mumbai",
        plan_id="PLAN-DIA-500M",
        plan_name="500 Mbps DIA Primary Link",
        unit_price=Decimal('20000.00'),
        status=SubscriptionStatus.ACTIVE,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() + timedelta(days=30)
    )

    # Branch 1: Nariman Point
    b1_crm = CRMCustomer(
        customer_number="CUST-TATA-NP",
        legal_name="Tata Nariman Point Branch",
        account_type=AccountType.ENTERPRISE,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id="mumbai",
        parent_customer_id=parent.id,
        hierarchy_type=HierarchyType.BRANCH_LOCATION
    )
    db_session.add_all([hq_sub, b1_crm])
    db_session.commit()

    b1_ba = BillingAccount(
        account_number="BA-TATA-NP",
        customer_id=leg_cust.id,
        crm_customer_id=b1_crm.id,
        parent_billing_account_id=parent_ba.id,
        market_id="mumbai",
        invoicing_preference=InvoicingPreference.CONSOLIDATED,
        status=BillingAccountStatus.ACTIVE
    )
    db_session.add(b1_ba)
    db_session.commit()

    # Branch 1 Subscription: 200 Mbps Link
    b1_sub = BillingSubscription(
        subscription_code="SUB-TATA-NP-200M",
        billing_account_id=b1_ba.id,
        customer_id=leg_cust.id,
        crm_customer_id=b1_crm.id,
        market_id="mumbai",
        plan_id="PLAN-DIA-200M",
        plan_name="200 Mbps DIA Branch Link",
        unit_price=Decimal('10000.00'),
        status=SubscriptionStatus.ACTIVE,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() + timedelta(days=30)
    )
    db_session.add(b1_sub)
    db_session.commit()

    # Generate parent consolidated invoice
    t_start = datetime(2026, 4, 1)
    t_end = datetime(2026, 4, 30)
    inv = generate_parent_consolidated_invoice(
        db=db_session,
        parent_billing_account_id=parent_ba.id,
        billing_period_start=t_start,
        billing_period_end=t_end
    )

    # Assertions
    # Taxable subtotal = 20,000 (HQ) + 10,000 (NP) = 30,000.00
    assert Decimal(str(inv.subtotal)) == Decimal('30000.00')
    # GST 18% = 5,400.00 (CGST 2,700.00, SGST 2,700.00)
    assert Decimal(str(inv.tax_amount)) == Decimal('5400.00')
    assert Decimal(str(inv.total_amount)) == Decimal('35400.00')
    assert len(inv.line_items) == 2

    # Verify site tagging on line items
    item_descs = [item.description for item in inv.line_items]
    assert any("[Headquarters]" in d for d in item_descs)
    assert any("[Tata Nariman Point Branch]" in d for d in item_descs)

    # Verify General Ledger is completely balanced (Debit == Credit)
    ledger_audit = verify_ledger_balancing(db_session)
    assert ledger_audit["is_fully_balanced"] is True


# ─── 2. Omnichannel Sales CRM & CPQ Tests ────────────────────────────────────

def test_sales_lead_creation_and_fiber_feasibility(db_session):
    """Verifies sales lead scoring and fiber distance feasibility check."""
    lead = create_lead(
        db=db_session,
        company_name="Infosys Technologies Ltd",
        contact_name="Rohit Sharma",
        phone="9819900112",
        email="rohit@infosys.com",
        market_id="mumbai",
        account_type=AccountType.ENTERPRISE,
        estimated_mrr=Decimal('25000.00'),
        installation_address="BKC Complex, Mumbai",
        pincode="400051"
    )

    # Base score: 50 + 20(Enterprise) + 15(MRR > 10k) + 10(Corporate email) + 5(Address) = 100
    assert lead.lead_score == 100
    assert lead.sales_stage == LeadSalesStage.NEW

    # Perform feasibility check (Distance = 250 meters)
    updated_lead = assess_fiber_feasibility(db_session, lead.id, fiber_distance_meters=250)
    assert updated_lead.feasibility_status == FeasibilityStatus.FEASIBLE
    assert updated_lead.sales_stage == LeadSalesStage.FEASIBILITY_CHECK


def test_cpq_quotation_pricing_approval_and_conversion(db_session):
    """Verifies quote configuration with SLA/contention multipliers, discount threshold, and conversion."""
    lead = create_lead(
        db=db_session,
        company_name="Reliance Retail Ltd",
        contact_name="Mukesh Ambani",
        phone="9820099887",
        email="mukesh@reliance.com",
        market_id="mumbai",
        account_type=AccountType.ENTERPRISE
    )

    # Configure a quotation:
    # Item 1: 500 Mbps Dedicated (1:1), Mission Critical 99.95% SLA (+30% markup)
    # Base unit price = 10,000. Multipliers: 1.0 (1:1) * 1.30 (Mission Critical) = 13,000.00
    items = [
        {
            "item_type": QuotationItemType.BANDWIDTH,
            "plan_id": "PLAN-DIA-500M",
            "description": "500 Mbps High-Availability Dedicated Link",
            "bandwidth_mbps": 500,
            "contention_ratio": "1:1",
            "sla_tier": SLATier.MISSION_CRITICAL_99_95,
            "unit_price": "10000.00",
            "quantity": "1.00"
        }
    ]

    # Offer 20% discount (exceeds 15% threshold -> requires finance approval)
    quote = create_quotation(
        db=db_session,
        lead_id=lead.id,
        items_data=items,
        contract_term_months=12,
        discount_pct=Decimal('20.00'),
        setup_charges=Decimal('5000.00')
    )

    assert Decimal(str(quote.base_mrr)) == Decimal('13000.00')
    # 20% off 13,000 = 2,600 discount -> discounted_mrr = 10,400.00
    assert Decimal(str(quote.discounted_mrr)) == Decimal('10400.00')
    assert quote.requires_approval is True
    assert quote.approval_status == "PENDING"
    assert quote.status == QuotationStatus.PENDING_APPROVAL

    # VP Finance approves quote
    quote = approve_quotation(db_session, quote.id, approved_by_id=1, notes="Strategic enterprise account approved.")
    assert quote.approval_status == "APPROVED"
    assert quote.status == QuotationStatus.APPROVED

    # Convert quote to customer & active order
    res = convert_quotation_to_customer(db_session, quote.id, user_id=1)
    assert res["status"] == "CONVERTED"
    assert res["initial_invoice_code"].startswith("INV-")
    # First payment = discounted_mrr (10,400) + OTC (5,000) = 15,400 + 18% GST (2,772) = 18,172.00
    assert Decimal(str(res["total_first_payment"])) == Decimal('18172.00')

    # Verify lead transitioned to WON
    db_session.refresh(lead)
    assert lead.sales_stage == LeadSalesStage.WON

    # Verify General Ledger is completely balanced
    ledger_audit = verify_ledger_balancing(db_session)
    assert ledger_audit["is_fully_balanced"] is True


# ─── 3. Statutory GST e-Invoicing & Dynamic B2C UPI QR Tests ─────────────────

def test_einvoice_b2b_irn_generation_and_cancellation(db_session):
    """Verifies statutory INV-01 payload, 64-char SHA-256 IRN hash, and 24h cancellation."""
    inv_code = f"INV-EINV-{uuid.uuid4().hex[:6].upper()}"
    # Test canonical SHA-256 hash algorithm matches NIC specification
    # IRN = SHA256(SupplierGSTIN + FinYear + DocType + DocNo)
    expected_hash = compute_canonical_irn_hash("27AAACP0123A1Z1", "2026-27", "INV", inv_code)
    assert len(expected_hash) == 64
    assert all(c in "0123456789abcdef" for c in expected_hash)

    # Create B2B Customer with GSTIN
    cust = Customer(
        name="Wipro Ltd",
        email="finance@wipro.com",
        phone="9820011999",
        customer_code=f"CUST-WIPRO-{uuid.uuid4().hex[:4].upper()}",
        market_id="mumbai",
        locality="Electronic City",
        segment="Enterprise",
        customer_type="Postpaid",
        plan_name="Enterprise Leased Line"
    )
    db_session.add(cust)
    db_session.commit()

    inv = Invoice(
        market_id="mumbai",
        invoice_code=inv_code,
        customer_id=cust.id,
        plan_name="Enterprise Dedicated Internet Access",
        billed_amount=59000.0,
        expected_amount=59000.0,
        subtotal=Decimal('50000.00'),
        tax_amount=Decimal('9000.00'),
        discount_amount=Decimal('0.00'),
        total_amount=Decimal('59000.00'),
        paid_amount=Decimal('0.00'),
        outstanding_amount=Decimal('59000.00'),
        due_date=datetime.utcnow() + timedelta(days=15),
        status="Issued",
        tax_treatment="INTRA_STATE",
        cgst_amount=Decimal('4500.00'),
        sgst_amount=Decimal('4500.00'),
        igst_amount=Decimal('0.00'),
        place_of_supply="27-Maharashtra",
        customer_gstin="27AAACW1234A1Z9"  # Valid B2B GSTIN
    )
    db_session.add(inv)
    db_session.commit()

    assert is_einvoice_applicable(inv, cust) is True

    # Generate statutory INV-01 JSON payload
    payload = generate_inv01_payload(inv, db_session)
    assert payload["Version"] == "1.1"
    assert payload["TranDtls"]["SupTyp"] == "B2B"
    assert payload["SellerDtls"]["Gstin"] == "27AAACP0123A1Z1"
    assert payload["BuyerDtls"]["Gstin"] == "27AAACW1234A1Z9"
    assert payload["ValDtls"]["TotInvVal"] == 59000.0

    # Process e-Invoice Registration
    reg_res = process_einvoice_registration(db_session, inv.id)
    assert reg_res["einvoice_status"] == "GENERATED"
    assert len(reg_res["irn"]) == 64
    assert reg_res["irp_ack_no"].startswith("11")
    assert reg_res["signed_qr"] is not None

    db_session.refresh(inv)
    assert inv.irn_hash == reg_res["irn"]
    assert inv.einvoice_status == "GENERATED"

    # Cancel e-Invoice within 24 hours
    cancel_res = cancel_einvoice(db_session, inv.id, cancel_reason="1", cancel_remarks="Duplicate entry")
    assert cancel_res["einvoice_status"] == "CANCELLED"
    db_session.refresh(inv)
    assert inv.einvoice_status == "CANCELLED"


def test_b2c_dynamic_upi_qr_generation_and_pdf(db_session):
    """Verifies dynamic B2C UPI QR code string generation and ReportLab PDF embedding."""
    inv_code = f"INV-B2C-{uuid.uuid4().hex[:6].upper()}"
    cust = Customer(
        name="Sunita Deshmukh",
        email="sunita@example.com",
        phone="9820033445",
        customer_code=f"CUST-RETAIL-{uuid.uuid4().hex[:4].upper()}",
        market_id="mumbai",
        locality="Andheri West",
        segment="Retail Broadband",
        customer_type="Prepaid",
        plan_name="Fiber Home 300 Mbps"
    )
    db_session.add(cust)
    db_session.commit()

    inv = Invoice(
        market_id="mumbai",
        invoice_code=inv_code,
        customer_id=cust.id,
        plan_name="Fiber Home 300 Mbps Unlimited",
        billed_amount=1178.82,
        expected_amount=1178.82,
        subtotal=Decimal('999.00'),
        tax_amount=Decimal('179.82'),
        discount_amount=Decimal('0.00'),
        total_amount=Decimal('1178.82'),
        paid_amount=Decimal('0.00'),
        outstanding_amount=Decimal('1178.82'),
        due_date=datetime.utcnow() + timedelta(days=15),
        status="Issued",
        tax_treatment="INTRA_STATE",
        cgst_amount=Decimal('89.91'),
        sgst_amount=Decimal('89.91'),
        customer_gstin=None  # Retail Consumer (B2C)
    )
    db_session.add(inv)
    db_session.commit()

    assert is_einvoice_applicable(inv, cust) is False

    # Process B2C registration
    reg_res = process_einvoice_registration(db_session, inv.id)
    assert reg_res["einvoice_status"] == "NOT_APPLICABLE"
    assert "upi://pay?pa=sentinelos.pmrg@icici" in reg_res["upi_qr_payload"]
    assert "am=1178.82" in reg_res["upi_qr_payload"]
    assert f"tr={inv_code}" in reg_res["upi_qr_payload"]

    # Verify ReportLab PDF Generation succeeds with the embedded dynamic UPI QR drawing
    pdf_bytes = generate_invoice_pdf(inv)
    assert len(pdf_bytes) > 2000
    assert pdf_bytes.startswith(b"%PDF")


# ─── 4. UPI AutoPay Recurring Mandate & Execution Tests ──────────────────────

def test_autopay_mandate_lifecycle_and_execution(db_session):
    """Verifies mandate registration, RBI statutory pre-debit alert, auto-debit execution, and GL posting."""
    inv_code = f"INV-AUTO-{uuid.uuid4().hex[:6].upper()}"
    cust = Customer(
        name="Aarav Mehta",
        email="aarav@example.com",
        phone="9820077889",
        customer_code=f"CUST-AUTOPAY-{uuid.uuid4().hex[:4].upper()}",
        market_id="mumbai",
        locality="Worli",
        segment="Retail Broadband",
        customer_type="Postpaid",
        plan_name="Fiber Turbo 500 Mbps"
    )
    db_session.add(cust)
    db_session.commit()

    ba = BillingAccount(
        account_number=f"BA-AUTOPAY-{uuid.uuid4().hex[:4].upper()}",
        customer_id=cust.id,
        market_id="mumbai",
        balance=Decimal('0.00'),
        status=BillingAccountStatus.ACTIVE
    )
    db_session.add(ba)
    db_session.commit()

    # 1. Register Mandate
    mandate = register_mandate(
        db=db_session,
        billing_account_id=ba.id,
        mandate_type=MandateType.UPI_AUTOPAY,
        max_amount=Decimal('5000.00'),
        vpa="aarav@oksbi"
    )

    assert mandate.status == MandateStatus.ACTIVE
    assert mandate.umrn.startswith("NPCIMUM")
    db_session.refresh(ba)
    assert ba.auto_pay_enabled is True

    # 2. Issue Invoice
    inv = Invoice(
        market_id="mumbai",
        invoice_code=inv_code,
        customer_id=cust.id,
        billing_account_id=ba.id,
        plan_name="Fiber Turbo 500 Mbps",
        billed_amount=1770.0,
        expected_amount=1770.0,
        subtotal=Decimal('1500.00'),
        tax_amount=Decimal('270.00'),
        total_amount=Decimal('1770.00'),
        paid_amount=Decimal('0.00'),
        outstanding_amount=Decimal('1770.00'),
        due_date=datetime.utcnow() + timedelta(days=2),
        status="Issued"
    )
    db_session.add(inv)
    db_session.commit()

    # 3. Issue Statutory Pre-Debit Notification (RBI circular compliance)
    pdn = issue_pre_debit_notification(db_session, mandate.id, inv.id, advance_days=2)
    assert pdn.status == "SENT"
    assert pdn.debit_amount == Decimal('1770.00')
    assert "opt-out" in pdn.opt_out_url

    # 4. Execute Automated Debit
    exec_res = execute_mandate_auto_debit(db_session, mandate.id, inv.id)
    assert exec_res["status"] == "SUCCESS"
    assert exec_res["debited_amount"] == 1770.0
    assert exec_res["invoice_status"] == "Paid"
    assert exec_res["remaining_outstanding"] == 0.0

    db_session.refresh(inv)
    assert inv.status == "Paid"
    assert inv.paid_amount == Decimal('1770.00')
    assert inv.outstanding_amount == Decimal('0.00')

    # Verify General Ledger is completely balanced (Dr Cash Clearing 1100, Cr Accounts Receivable 1200)
    ledger_audit = verify_ledger_balancing(db_session)
    assert ledger_audit["is_fully_balanced"] is True

    # 5. Mandate Limit Protection: attempting debit > max_amount raises ValueError
    high_inv = Invoice(
        market_id="mumbai",
        invoice_code=f"INV-HIGH-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cust.id,
        billing_account_id=ba.id,
        plan_name="Ultra Heavy Enterprise Link",
        billed_amount=10000.0,
        expected_amount=10000.0,
        total_amount=Decimal('10000.00'),
        outstanding_amount=Decimal('10000.00'),  # Exceeds max_amount of 5000.00
        due_date=datetime.utcnow() + timedelta(days=2),
        status="Issued"
    )
    db_session.add(high_inv)
    db_session.commit()

    with pytest.raises(ValueError, match="exceeds mandate"):
        execute_mandate_auto_debit(db_session, mandate.id, high_inv.id)

    # 6. Revoke Mandate
    revoked = revoke_mandate(db_session, mandate.id, reason="Customer switching to manual payment")
    assert revoked.status == MandateStatus.REVOKED
    db_session.refresh(ba)
    assert ba.auto_pay_enabled is False
