"""
app/tests/test_phase5_franchise_lco_and_bank_recon.py

Comprehensive Enterprise Test Suite for Phase 5:
1. Multi-tier Franchise & LCO Partner Hierarchy & Cycle Prevention.
2. Authoritative Prepaid Partner Wallet Float, Top-Ups & Wholesale Deductions with GL Balancing.
3. White-Label Commission Statements, Section 194H 5% TDS Withholding & Settlement.
4. Automated Bank Statement Ingestion (MT940/CSV) & Open Invoice Auto-Reconciliation.
5. Partner Wallet Credit Limit Allowance & Threshold Enforcement.
"""

import uuid
import pytest
from decimal import Decimal
from datetime import datetime, timedelta

from app.models import Customer, Invoice
from app.models_crm import CRMCustomer, CRMAddress, AccountType, CustomerLifecycleStatus, AddressType
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingAccountStatus,
    BillingType, SubscriptionStatus, InvoiceStatus, ChartOfAccounts
)
from app.models_partner import (
    FranchisePartner, PartnerWallet, WalletTransaction, PartnerCommissionStatement,
    BankStatementRecord, PartnerType, PartnerStatus, CommissionModel,
    CommissionBasis, WalletTransactionType, CommissionStatementStatus,
    BankReconciliationStatus
)
from app.services.ledger_service import verify_ledger_balancing, quantize_money
from app.services.sequence_service import generate_business_code, SequenceType
from app.services.partner_service import (
    create_franchise_partner, link_sub_partner, topup_partner_wallet,
    deduct_partner_wallet, generate_partner_commission_statement,
    settle_partner_commission_statement, ingest_bank_statement_records
)


# ─── Helper Functions ────────────────────────────────────────────────────────

def _setup_open_invoice(
    db,
    tag: str,
    market_id: str = "mumbai_p5",
    invoice_amount: Decimal = Decimal('1180.00')
) -> tuple:
    """Creates a customer and an open unpaid invoice for bank reconciliation tests."""
    uid = uuid.uuid4().hex[:6].upper()
    cust_code = f"CUST-P5-{tag}-{uid}"
    cust = Customer(
        name=f"Subscriber P5 {tag}",
        email=f"sub_p5_{tag.lower()}_{uid.lower()}@testisp.in",
        phone=f"98200{int(uid[:4], 16) % 90000 + 10000}",
        customer_code=cust_code,
        market_id=market_id,
        locality="Andheri East",
        segment="Retail",
        plan_name="UltraFiber 150M"
    )
    db.add(cust)
    db.flush()

    crm_cust = CRMCustomer(
        customer_number=f"CRM-P5-{tag}-{uid}",
        legal_name=f"Subscriber P5 {tag}",
        account_type=AccountType.RESIDENTIAL,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id=market_id,
        legacy_customer_id=cust.id
    )
    db.add(crm_cust)
    db.flush()

    acct_code = generate_business_code(db=db, sequence_type=SequenceType.BILLING_ACCOUNT, market_id=market_id)
    acct = BillingAccount(
        account_number=acct_code,
        customer_id=cust.id,
        crm_customer_id=crm_cust.id,
        market_id=market_id,
        billing_type=BillingType.POSTPAID,
        currency="INR",
        status=BillingAccountStatus.ACTIVE,
        balance=Decimal('0.00'),
        credit_limit=Decimal('0.00')
    )
    db.add(acct)
    db.flush()

    inv_code = generate_business_code(db=db, sequence_type=SequenceType.INVOICE, market_id=market_id)
    taxable = quantize_money(invoice_amount / Decimal('1.18'))
    tax_amt = invoice_amount - taxable

    inv = Invoice(
        invoice_code=inv_code,
        customer_id=cust.id,
        billing_account_id=acct.id,
        market_id=market_id,
        plan_name="UltraFiber 150M",
        billed_amount=float(invoice_amount),
        expected_amount=float(invoice_amount),
        subtotal=taxable,
        tax_amount=tax_amt,
        discount_amount=Decimal('0.00'),
        total_amount=invoice_amount,
        paid_amount=Decimal('0.00'),
        outstanding_amount=invoice_amount,
        currency="INR",
        status=InvoiceStatus.ISSUED,
        due_date=datetime.utcnow() + timedelta(days=15),
        created_at=datetime.utcnow()
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return cust, acct, inv


# ─── Test 1: Partner Creation & Cycle Prevention ─────────────────────────────

def test_franchise_partner_creation_and_cycle_prevention(db_session):
    """
    Verifies multi-tier franchise partner onboarding:
    1. Master Distributor -> Sub-Franchisee -> LCO Operator.
    2. Automatic wallet provisioning for each partner.
    3. Cycle detection preventing circular parent-child relationships.
    """
    mkt = "mumbai_p5a"

    # 1. Onboard Master Distributor
    master = create_franchise_partner(
        db=db_session,
        legal_name="Apex Telecom Infrastructure Ltd",
        trade_name="Apex Fiber Mumbai",
        contact_email="billing@apexfiber.in",
        contact_phone="9820011111",
        market_id=mkt,
        partner_type=PartnerType.MASTER_DISTRIBUTOR,
        commission_rate=Decimal('30.00'),
        gstin="27AAACA1234A1Z5"
    )
    assert master.id is not None
    assert master.partner_code.startswith("LCO-")
    assert master.partner_type == PartnerType.MASTER_DISTRIBUTOR
    assert master.wallet is not None
    assert master.wallet.balance == Decimal('0.00')

    # 2. Onboard Sub-Franchisee under Master Distributor
    sub_franchisee = create_franchise_partner(
        db=db_session,
        legal_name="Western Suburbs Networks LLP",
        trade_name="Western Fiber",
        contact_email="accounts@westernfiber.in",
        contact_phone="9820022222",
        market_id=mkt,
        partner_type=PartnerType.SUB_FRANCHISEE,
        parent_partner_id=master.id,
        commission_rate=Decimal('25.00'),
        gstin="27AAACW5678B1Z2"
    )
    assert sub_franchisee.parent_partner_id == master.id
    assert sub_franchisee.wallet is not None

    # 3. Onboard Local Cable Operator (LCO) under Sub-Franchisee
    lco = create_franchise_partner(
        db=db_session,
        legal_name="Andheri Cable Network",
        trade_name="Andheri Net",
        contact_email="support@andhericable.in",
        contact_phone="9820033333",
        market_id=mkt,
        partner_type=PartnerType.LCO_OPERATOR,
        parent_partner_id=sub_franchisee.id,
        commission_rate=Decimal('20.00')
    )
    assert lco.parent_partner_id == sub_franchisee.id
    assert lco.wallet is not None

    # 4. Cycle Detection: Attempting to link Master Distributor under LCO Operator must fail
    with pytest.raises(ValueError, match="Circular hierarchy detected"):
        link_sub_partner(db=db_session, sub_partner_id=master.id, parent_partner_id=lco.id)

    # 5. Self-link Prevention: Partner cannot be its own parent
    with pytest.raises(ValueError, match="Partner cannot be linked to itself"):
        link_sub_partner(db=db_session, sub_partner_id=lco.id, parent_partner_id=lco.id)


# ─── Test 2: Prepaid Wallet Top-up & Deduction with GL Balancing ──────────────

def test_partner_wallet_topup_and_insufficient_balance_deduction(db_session):
    """
    Verifies authoritative prepaid wallet accounting:
    1. Wallet top-up credits float and posts balanced GL entries (1100 Dr / 2300 Cr).
    2. Wholesale deduction reduces float upon customer activation and posts (2300 Dr / 4100 Cr).
    3. Deduction exceeding available balance (balance + credit limit) is blocked with clear error.
    """
    mkt = "mumbai_p5b"

    lco = create_franchise_partner(
        db=db_session,
        legal_name="Bandra Broadband Services",
        trade_name="Bandra Net",
        contact_email="ops@bandranet.in",
        contact_phone="9820044444",
        market_id=mkt,
        partner_type=PartnerType.LCO_OPERATOR,
        credit_limit=Decimal('0.00')
    )
    assert lco.wallet.balance == Decimal('0.00')

    # 1. Top-up Wallet by INR 10,000 via Bank Transfer
    topup_tx = topup_partner_wallet(
        db=db_session,
        partner_id=lco.id,
        amount=Decimal('10000.00'),
        reference_id="UTR-HDFC-998877",
        notes="Prepaid float replenishment via NEFT"
    )
    assert topup_tx.direction == "CREDIT"
    assert topup_tx.amount == Decimal('10000.00')
    assert topup_tx.balance_before == Decimal('0.00')
    assert topup_tx.balance_after == Decimal('10000.00')
    assert topup_tx.transaction_code.startswith("WLT-")

    # Verify GL balancing after top-up
    ledger_chk1 = verify_ledger_balancing(db=db_session, market_id=mkt)
    assert ledger_chk1["is_fully_balanced"] is True

    # 2. Deduct wholesale operator share for subscriber activation (INR 2,500)
    deduct_tx = deduct_partner_wallet(
        db=db_session,
        partner_id=lco.id,
        wholesale_amount=Decimal('2500.00'),
        transaction_type=WalletTransactionType.CUSTOMER_ACTIVATION,
        reference_id="SUB-ACT-2026-001",
        notes="Wholesale wholesale activation fee for customer CUST-001"
    )
    assert deduct_tx.direction == "DEBIT"
    assert deduct_tx.amount == Decimal('2500.00')
    assert deduct_tx.balance_before == Decimal('10000.00')
    assert deduct_tx.balance_after == Decimal('7500.00')

    # Verify GL balancing after wholesale deduction
    ledger_chk2 = verify_ledger_balancing(db=db_session, market_id=mkt)
    assert ledger_chk2["is_fully_balanced"] is True

    # 3. Attempt deduction of INR 15,000 (Available: INR 7,500, Credit limit: INR 0) -> Must fail
    with pytest.raises(ValueError, match="Insufficient partner wallet balance"):
        deduct_partner_wallet(
            db=db_session,
            partner_id=lco.id,
            wholesale_amount=Decimal('15000.00'),
            transaction_type=WalletTransactionType.CUSTOMER_ACTIVATION,
            reference_id="SUB-ACT-2026-002"
        )

    # Balance remains intact at INR 7,500
    db_session.refresh(lco.wallet)
    assert lco.wallet.balance == Decimal('7500.00')


# ─── Test 3: Commission Statement & Section 194H TDS Settlement ──────────────

def test_white_label_commission_statement_and_section_194h_tds_settlement(db_session):
    """
    Verifies statutory telecom commission settlement under Section 194H:
    1. Monthly collections = INR 118,000 (Taxable = INR 100,000, GST = INR 18,000).
    2. Commission Rate = 25% on Net of Tax -> Gross Commission = INR 25,000.00.
    3. Section 194H TDS Withholding = 5% of INR 25,000 = INR 1,250.00.
    4. Net Commission Payable = INR 23,750.00.
    5. Settlement credits net payout to wallet and posts balanced GL entries:
       Debit 5100 Partner Commission Expense (INR 25,000.00) ==
       Credit 2400 TDS Payable (INR 1,250.00) + Credit 2300 Partner Wallet Liability (INR 23,750.00).
    """
    mkt = "mumbai_p5c"

    partner = create_franchise_partner(
        db=db_session,
        legal_name="Juhu Cable & Broadband Solutions",
        trade_name="Juhu Net",
        contact_email="finance@juhunet.in",
        contact_phone="9820055555",
        market_id=mkt,
        partner_type=PartnerType.LCO_OPERATOR,
        commission_rate=Decimal('25.00'),
        tds_rate=Decimal('5.00')
    )

    p_start = datetime(2026, 1, 1)
    p_end = datetime(2026, 1, 31, 23, 59, 59)

    # 1. Generate Commission Statement
    stmt = generate_partner_commission_statement(
        db=db_session,
        partner_id=partner.id,
        period_start=p_start,
        period_end=p_end,
        gross_collections=Decimal('118000.00'),
        taxable_revenue=Decimal('100000.00'),
        subscribers_count=120
    )

    assert stmt.statement_code.startswith("CMS-")
    assert stmt.gross_billed_amount == Decimal('118000.00')
    assert stmt.taxable_revenue == Decimal('100000.00')
    assert stmt.gross_commission == Decimal('25000.00')
    assert stmt.tds_amount == Decimal('1250.00')
    assert stmt.net_commission_payable == Decimal('23750.00')
    assert stmt.status == CommissionStatementStatus.DRAFT

    # 2. Settle Commission Statement to Partner Wallet
    settle_res = settle_partner_commission_statement(
        db=db_session,
        statement_id=stmt.id,
        payout_method="WALLET_CREDIT"
    )

    assert settle_res["status"] == "SETTLED"
    assert settle_res["gross_commission"] == 25000.00
    assert settle_res["tds_withheld"] == 1250.00
    assert settle_res["net_payout"] == 23750.00
    assert settle_res["wallet_balance_after"] == 23750.00

    # Verify statement status updated
    db_session.refresh(stmt)
    assert stmt.status == CommissionStatementStatus.SETTLED
    assert stmt.settled_at is not None

    # Verify wallet balance
    db_session.refresh(partner.wallet)
    assert partner.wallet.balance == Decimal('23750.00')

    # 3. Verify General Ledger Invariant: Gross Expense == TDS Payable + Net Wallet Liability
    ledger_chk = verify_ledger_balancing(db=db_session, market_id=mkt)
    assert ledger_chk["is_fully_balanced"] is True

    # 4. Attempting to re-settle already settled statement raises error
    with pytest.raises(ValueError, match="already settled"):
        settle_partner_commission_statement(db=db_session, statement_id=stmt.id)


# ─── Test 4: Bank Statement Ingestion & Auto-Reconciliation ──────────────────

def test_bank_statement_csv_ingestion_and_auto_reconciliation(db_session):
    """
    Verifies automated bank feed matching engine:
    1. Ingests bank feed containing invoice codes, customer codes, and unidentified transactions.
    2. Auto-matches Record 1 via Invoice Code in narration and clears open balance.
    3. Auto-matches Record 2 via Customer Code + exact outstanding balance.
    4. Leaves Record 3 as UNMATCHED for finance desk review.
    5. Confirms double-entry General Ledger balancing (1100 Dr / 1200 Cr).
    """
    mkt = "mumbai_p5d"

    # Setup 2 customers with open unpaid invoices
    cust1, acct1, inv1 = _setup_open_invoice(db=db_session, tag="AUTO1", market_id=mkt, invoice_amount=Decimal('1180.00'))
    cust2, acct2, inv2 = _setup_open_invoice(db=db_session, tag="AUTO2", market_id=mkt, invoice_amount=Decimal('590.00'))

    assert inv1.status == InvoiceStatus.ISSUED
    assert inv1.outstanding_amount == Decimal('1180.00')
    assert inv2.status == InvoiceStatus.ISSUED
    assert inv2.outstanding_amount == Decimal('590.00')

    # Prepare bank feed entries
    batch_ref = f"BATCH-BANK-{uuid.uuid4().hex[:8].upper()}"
    records = [
        # Record 1: Matches by exact invoice code in narration
        {
            "utr_number": f"UTR-HDFC-{uuid.uuid4().hex[:10].upper()}",
            "amount": Decimal('1180.00'),
            "narration": f"NEFT-CLR-9901 / PYMT FOR {inv1.invoice_code} / CITIBANK",
            "transaction_date": datetime.utcnow()
        },
        # Record 2: Matches by customer code + exact amount
        {
            "utr_number": f"UTR-ICIC-{uuid.uuid4().hex[:10].upper()}",
            "amount": Decimal('590.00'),
            "narration": f"UPI/CR/1234567890/{cust2.customer_code}/BROADBAND",
            "transaction_date": datetime.utcnow()
        },
        # Record 3: Unidentified payment without reference
        {
            "utr_number": f"UTR-AXIS-{uuid.uuid4().hex[:10].upper()}",
            "amount": Decimal('2500.00'),
            "narration": "IMPS-TRANSFER-UNKNOWN REMITTER-REF-889900",
            "transaction_date": datetime.utcnow()
        }
    ]

    # Ingest and execute automated reconciliation
    recon_summary = ingest_bank_statement_records(
        db=db_session,
        batch_reference=batch_ref,
        records_data=records
    )

    assert recon_summary["batch_reference"] == batch_ref
    assert recon_summary["total_records"] == 3
    assert recon_summary["auto_matched_count"] == 2
    assert recon_summary["unmatched_count"] == 1
    assert recon_summary["match_rate_pct"] == 66.67

    # Verify Invoice 1 was marked PAID
    db_session.refresh(inv1)
    assert inv1.status == InvoiceStatus.PAID
    assert inv1.outstanding_amount == Decimal('0.00')
    assert inv1.paid_amount == Decimal('1180.00')
    assert inv1.paid_date is not None

    # Verify Invoice 2 was marked PAID
    db_session.refresh(inv2)
    assert inv2.status == InvoiceStatus.PAID
    assert inv2.outstanding_amount == Decimal('0.00')
    assert inv2.paid_amount == Decimal('590.00')

    # Verify bank statement records in database
    db_records = db_session.query(BankStatementRecord).filter(
        BankStatementRecord.batch_reference == batch_ref
    ).all()
    assert len(db_records) == 3

    matched_recs = [r for r in db_records if r.reconciliation_status == BankReconciliationStatus.AUTO_MATCHED]
    unmatched_recs = [r for r in db_records if r.reconciliation_status == BankReconciliationStatus.UNMATCHED]

    assert len(matched_recs) == 2
    assert len(unmatched_recs) == 1
    assert matched_recs[0].confidence_score == Decimal('100.00')
    assert matched_recs[0].reconciled_by == "BANK_RECON_AI"

    # Verify General Ledger is fully balanced for the market
    ledger_chk = verify_ledger_balancing(db=db_session, market_id=mkt)
    assert ledger_chk["is_fully_balanced"] is True


# ─── Test 5: Partner Wallet Credit Limit Allowance ───────────────────────────

def test_partner_wallet_credit_limit_allowance(db_session):
    """
    Verifies partner credit limit flexibility:
    1. Partner with INR 5,000 credit limit can deduct charges into negative balance up to the limit.
    2. Over-limit deductions exceeding credit limit are rejected.
    """
    mkt = "mumbai_p5e"

    lco = create_franchise_partner(
        db=db_session,
        legal_name="Chembur Internet Services",
        trade_name="Chembur Net",
        contact_email="billing@chemburnet.in",
        contact_phone="9820066666",
        market_id=mkt,
        partner_type=PartnerType.LCO_OPERATOR,
        credit_limit=Decimal('5000.00')
    )
    assert lco.wallet.balance == Decimal('0.00')
    assert lco.wallet.credit_limit == Decimal('5000.00')

    # 1. Wholesale deduction of INR 3,000 against zero balance (allowed via credit limit)
    tx1 = deduct_partner_wallet(
        db=db_session,
        partner_id=lco.id,
        wholesale_amount=Decimal('3000.00'),
        transaction_type=WalletTransactionType.PLAN_RENEWAL,
        reference_id="RENEWAL-2026-001"
    )
    assert tx1.balance_before == Decimal('0.00')
    assert tx1.balance_after == Decimal('-3000.00')
    assert tx1.direction == "DEBIT"

    db_session.refresh(lco.wallet)
    assert lco.wallet.balance == Decimal('-3000.00')

    # Available remaining: INR 5,000 - INR 3,000 = INR 2,000
    # 2. Further deduction of INR 2,500 exceeds available credit limit -> Must fail
    with pytest.raises(ValueError, match="Insufficient partner wallet balance"):
        deduct_partner_wallet(
            db=db_session,
            partner_id=lco.id,
            wholesale_amount=Decimal('2500.00'),
            transaction_type=WalletTransactionType.PLAN_RENEWAL,
            reference_id="RENEWAL-2026-002"
        )

    # 3. Deduction of INR 2,000 exactly matches remaining credit limit -> Must succeed
    tx2 = deduct_partner_wallet(
        db=db_session,
        partner_id=lco.id,
        wholesale_amount=Decimal('2000.00'),
        transaction_type=WalletTransactionType.PLAN_RENEWAL,
        reference_id="RENEWAL-2026-003"
    )
    assert tx2.balance_after == Decimal('-5000.00')

    db_session.refresh(lco.wallet)
    assert lco.wallet.balance == Decimal('-5000.00')

    # Ledger balanced
    ledger_chk = verify_ledger_balancing(db=db_session, market_id=mkt)
    assert ledger_chk["is_fully_balanced"] is True
