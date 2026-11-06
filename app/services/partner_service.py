"""
app/services/partner_service.py

SentinelOS Multi-Tenant Franchise, LCO Partner Portal & Revenue Share Engine.
Implements:
1. Multi-tier Franchise & LCO Partner Hierarchy (Master Distributor -> Sub-Franchisee -> LCO).
2. Authoritative Prepaid Partner Wallet (Float management, credit limits, top-ups, auto-deductions).
3. Wallet Transactions Audit Ledger (Top-ups, activations, renewals, commission payouts).
4. White-label Commission Statements & Statutory TDS Withholding (Section 194H 5% TDS).
5. Automated Bank Statement Ingestion (MT940 / CSV) & Auto-Reconciliation.
"""

import uuid
import re
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_

from app.models import Customer, Invoice
from app.models_finance import (
    BillingAccount, BillingSubscription, InvoiceStatus, ChartOfAccounts
)
from app.models_partner import (
    FranchisePartner, PartnerWallet, WalletTransaction, PartnerCommissionStatement,
    BankStatementRecord, PartnerType, PartnerStatus, CommissionModel,
    CommissionBasis, WalletTransactionType, CommissionStatementStatus,
    BankReconciliationStatus
)
from app.services.billing_service import _format_money
from app.services.ledger_service import quantize_money, post_journal_entry
from app.services.sequence_service import generate_business_code, SequenceType

logger = logging.getLogger(__name__)


# ─── 1. Franchise & LCO Partner Master & Hierarchy ───────────────────────────

def create_franchise_partner(
    db: Session,
    legal_name: str,
    trade_name: str,
    contact_email: str,
    contact_phone: str,
    market_id: str = "mumbai",
    partner_type: str = PartnerType.LCO_OPERATOR,
    parent_partner_id: Optional[Any] = None,
    commission_rate: Decimal = Decimal('25.00'),
    tds_rate: Decimal = Decimal('5.00'),
    gstin: Optional[str] = None,
    territory: Optional[str] = None,
    credit_limit: Decimal = Decimal('0.00')
) -> FranchisePartner:
    """
    Onboards an authoritative LCO or franchise partner and initializes their prepaid wallet.
    Generates contiguous business code e.g. LCO-MUM-2026-00001.
    """
    parent_uuid = None
    if parent_partner_id:
        parent_uuid = uuid.UUID(str(parent_partner_id)) if not isinstance(parent_partner_id, uuid.UUID) else parent_partner_id
        parent = db.query(FranchisePartner).filter(FranchisePartner.id == parent_uuid).first()
        if not parent:
            raise ValueError(f"Parent partner '{parent_partner_id}' not found.")

    partner_code = generate_business_code(
        db=db,
        sequence_type=SequenceType.PARTNER,
        market_id=market_id
    )

    partner = FranchisePartner(
        id=uuid.uuid4(),
        partner_code=partner_code,
        parent_partner_id=parent_uuid,
        partner_type=partner_type,
        legal_name=legal_name,
        trade_name=trade_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        gstin=gstin,
        pan=gstin[2:12] if gstin and len(gstin) >= 12 else None,
        market_id=market_id,
        territory=territory or "Default Territory",
        state="Maharashtra",
        commission_model=CommissionModel.FLAT_PERCENTAGE,
        commission_rate=quantize_money(commission_rate),
        calculation_basis=CommissionBasis.NET_OF_TAX,
        tds_rate=quantize_money(tds_rate),
        status=PartnerStatus.ACTIVE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(partner)
    db.flush()

    # Automatically provision partner prepaid wallet
    wallet = PartnerWallet(
        id=uuid.uuid4(),
        partner_id=partner.id,
        currency="INR",
        balance=Decimal('0.00'),
        credit_limit=quantize_money(credit_limit),
        minimum_threshold=Decimal('500.00'),
        status="ACTIVE",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(wallet)
    db.commit()
    db.refresh(partner)
    return partner


def link_sub_partner(
    db: Session,
    sub_partner_id: Any,
    parent_partner_id: Any
) -> FranchisePartner:
    """
    Links a sub-franchisee or LCO operator under a Master Distributor with cycle detection.
    """
    sid = uuid.UUID(str(sub_partner_id)) if not isinstance(sub_partner_id, uuid.UUID) else sub_partner_id
    pid = uuid.UUID(str(parent_partner_id)) if not isinstance(parent_partner_id, uuid.UUID) else parent_partner_id

    if sid == pid:
        raise ValueError("Partner cannot be linked to itself.")

    sub = db.query(FranchisePartner).filter(FranchisePartner.id == sid).first()
    parent = db.query(FranchisePartner).filter(FranchisePartner.id == pid).first()
    if not sub or not parent:
        raise ValueError("Sub-partner or parent partner not found.")

    # Cycle detection traversal
    curr = parent
    visited = {sid}
    while curr:
        if curr.id in visited:
            raise ValueError(f"Circular hierarchy detected: {curr.partner_code} is an ancestor of itself.")
        visited.add(curr.id)
        curr = curr.parent_partner if curr.parent_partner_id else None

    sub.parent_partner_id = parent.id
    sub.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(sub)
    return sub


# ─── 2. Prepaid Partner Wallet Engine ────────────────────────────────────────

def topup_partner_wallet(
    db: Session,
    partner_id: Any,
    amount: Decimal,
    reference_id: str,
    notes: Optional[str] = None
) -> WalletTransaction:
    """
    Credits an LCO partner's prepaid wallet float and posts balanced General Ledger entries:
    Debit:  1100 Cash & Bank Clearing
    Credit: 2300 Partner Wallet Float Liabilities
    """
    pid = uuid.UUID(str(partner_id)) if not isinstance(partner_id, uuid.UUID) else partner_id
    wallet = db.query(PartnerWallet).filter(PartnerWallet.partner_id == pid).with_for_update().first()
    if not wallet:
        raise ValueError(f"Wallet for partner '{partner_id}' not found.")

    dec_amt = quantize_money(amount)
    if dec_amt <= Decimal('0.00'):
        raise ValueError("Wallet top-up amount must be strictly positive.")

    partner = wallet.partner
    tx_code = generate_business_code(db=db, sequence_type=SequenceType.WALLET_TRANSACTION, market_id=partner.market_id)

    bal_before = quantize_money(wallet.balance)
    bal_after = bal_before + dec_amt

    wallet.balance = bal_after
    wallet.last_topup_at = datetime.utcnow()
    wallet.updated_at = datetime.utcnow()

    tx = WalletTransaction(
        id=uuid.uuid4(),
        transaction_code=tx_code,
        wallet_id=wallet.id,
        partner_id=partner.id,
        transaction_type=WalletTransactionType.TOPUP,
        direction="CREDIT",
        amount=dec_amt,
        balance_before=bal_before,
        balance_after=bal_after,
        reference_id=reference_id,
        notes=notes or f"Prepaid wallet top-up via {reference_id}",
        created_at=datetime.utcnow()
    )
    db.add(tx)

    # Post balanced double-entry General Ledger entry
    post_journal_entry(
        db=db,
        market_id=partner.market_id,
        reference_type="WALLET_TOPUP",
        reference_id=tx_code,
        description=f"Partner wallet top-up {tx_code} for {partner.partner_code}",
        lines=[
            {
                "account_code": ChartOfAccounts.CASH_CLEARING,
                "debit": dec_amt,
                "credit": Decimal('0.00'),
                "currency": wallet.currency
            },
            {
                "account_code": ChartOfAccounts.PARTNER_WALLET_LIABILITY,
                "debit": Decimal('0.00'),
                "credit": dec_amt,
                "currency": wallet.currency
            }
        ],
        allow_idempotent_replay=True
    )

    db.commit()
    db.refresh(tx)
    return tx


def deduct_partner_wallet(
    db: Session,
    partner_id: Any,
    wholesale_amount: Decimal,
    transaction_type: str = WalletTransactionType.CUSTOMER_ACTIVATION,
    reference_id: Optional[str] = None,
    notes: Optional[str] = None
) -> WalletTransaction:
    """
    Deducts wholesale operator fee from LCO's prepaid wallet upon subscriber activation or plan renewal.
    Blocks operation if available balance (balance + credit_limit) is insufficient.
    Debit:  2300 Partner Wallet Float Liabilities
    Credit: 4100 Broadband Service Revenue
    """
    pid = uuid.UUID(str(partner_id)) if not isinstance(partner_id, uuid.UUID) else partner_id
    wallet = db.query(PartnerWallet).filter(PartnerWallet.partner_id == pid).with_for_update().first()
    if not wallet:
        raise ValueError(f"Wallet for partner '{partner_id}' not found.")

    dec_amt = quantize_money(wholesale_amount)
    if dec_amt <= Decimal('0.00'):
        raise ValueError("Wholesale deduction amount must be strictly positive.")

    available = quantize_money(wallet.balance) + quantize_money(wallet.credit_limit)
    if available < dec_amt:
        raise ValueError(
            f"Insufficient partner wallet balance for {wallet.partner.partner_code}: "
            f"Required INR {dec_amt:.2f}, available INR {available:.2f}. "
            f"Please top up partner wallet before proceeding."
        )

    partner = wallet.partner
    tx_code = generate_business_code(db=db, sequence_type=SequenceType.WALLET_TRANSACTION, market_id=partner.market_id)

    bal_before = quantize_money(wallet.balance)
    bal_after = bal_before - dec_amt

    wallet.balance = bal_after
    wallet.updated_at = datetime.utcnow()

    tx = WalletTransaction(
        id=uuid.uuid4(),
        transaction_code=tx_code,
        wallet_id=wallet.id,
        partner_id=partner.id,
        transaction_type=transaction_type,
        direction="DEBIT",
        amount=dec_amt,
        balance_before=bal_before,
        balance_after=bal_after,
        reference_id=reference_id,
        notes=notes or f"Wholesale operator share deduction for {reference_id or 'subscriber'}",
        created_at=datetime.utcnow()
    )
    db.add(tx)

    # Post balanced double-entry General Ledger entry
    post_journal_entry(
        db=db,
        market_id=partner.market_id,
        reference_type="WALLET_DEDUCTION",
        reference_id=tx_code,
        description=f"Wholesale partner deduction {tx_code} from {partner.partner_code}",
        lines=[
            {
                "account_code": ChartOfAccounts.PARTNER_WALLET_LIABILITY,
                "debit": dec_amt,
                "credit": Decimal('0.00'),
                "currency": wallet.currency
            },
            {
                "account_code": ChartOfAccounts.BROADBAND_REVENUE,
                "debit": Decimal('0.00'),
                "credit": dec_amt,
                "currency": wallet.currency
            }
        ],
        allow_idempotent_replay=True
    )

    db.commit()
    db.refresh(tx)
    return tx


# ─── 3. White-Label Commission Statements & Section 194H TDS ────────────────

def generate_partner_commission_statement(
    db: Session,
    partner_id: Any,
    period_start: datetime,
    period_end: datetime,
    gross_collections: Decimal,
    taxable_revenue: Optional[Decimal] = None,
    subscribers_count: int = 1
) -> PartnerCommissionStatement:
    """
    Computes monthly partner revenue share commission and statutory Section 194H TDS withholding:
    - Net of Tax: Commission on Taxable Revenue (standard 18% GST excluded from commission base).
    - Gross Commission: Commission Base * Commission Rate (e.g. 25%).
    - TDS Withholding: 5% withheld under Section 194H of Indian Income Tax Act.
    - Net Payable = Gross Commission - TDS Withholding.
    """
    pid = uuid.UUID(str(partner_id)) if not isinstance(partner_id, uuid.UUID) else partner_id
    partner = db.query(FranchisePartner).filter(FranchisePartner.id == pid).first()
    if not partner:
        raise ValueError(f"FranchisePartner '{partner_id}' not found.")

    dec_gross = quantize_money(gross_collections)
    dec_taxable = quantize_money(taxable_revenue) if taxable_revenue is not None else quantize_money(dec_gross / Decimal('1.18'))

    # Determine commission base
    commission_base = dec_taxable if partner.calculation_basis == CommissionBasis.NET_OF_TAX else dec_gross

    # Calculate Gross Commission
    rate = quantize_money(partner.commission_rate)
    gross_commission = quantize_money(commission_base * (rate / Decimal('100.00')))

    # Calculate Section 194H TDS Withholding (5%)
    tds_r = quantize_money(partner.tds_rate)
    tds_amount = quantize_money(gross_commission * (tds_r / Decimal('100.00')))

    net_payable = gross_commission - tds_amount

    statement_code = generate_business_code(
        db=db,
        sequence_type=SequenceType.COMMISSION_STATEMENT,
        market_id=partner.market_id
    )

    statement = PartnerCommissionStatement(
        id=uuid.uuid4(),
        statement_code=statement_code,
        partner_id=partner.id,
        market_id=partner.market_id,
        period_start=period_start,
        period_end=period_end,
        active_subscribers_count=subscribers_count,
        gross_billed_amount=dec_gross,
        taxable_revenue=dec_taxable,
        commission_rate=rate,
        gross_commission=gross_commission,
        tds_rate=tds_r,
        tds_amount=tds_amount,
        net_commission_payable=net_payable,
        status=CommissionStatementStatus.DRAFT,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(statement)
    db.commit()
    db.refresh(statement)
    return statement


def settle_partner_commission_statement(
    db: Session,
    statement_id: Any,
    payout_method: str = "WALLET_CREDIT"
) -> Dict[str, Any]:
    """
    Settles approved partner commission:
    Credits net commission to partner's prepaid wallet float and posts balanced GL entries:
    Debit:  5100 Partner Commission Expense (Gross Commission)
    Credit: 2400 TDS Payable Section 194H (TDS Withheld)
    Credit: 2300 Partner Wallet Float Liabilities (Net Commission)
    Invariant: Debit == Credit (Gross == TDS + Net)
    """
    sid = uuid.UUID(str(statement_id)) if not isinstance(statement_id, uuid.UUID) else statement_id
    stmt = db.query(PartnerCommissionStatement).filter(PartnerCommissionStatement.id == sid).first()
    if not stmt:
        raise ValueError(f"Commission statement '{statement_id}' not found.")

    if stmt.status == CommissionStatementStatus.SETTLED:
        raise ValueError(f"Statement '{stmt.statement_code}' is already settled.")

    partner = stmt.partner
    wallet = partner.wallet
    if not wallet:
        raise ValueError("Partner wallet not initialized.")

    # 1. Credit net commission to partner wallet
    bal_before = quantize_money(wallet.balance)
    bal_after = bal_before + stmt.net_commission_payable
    wallet.balance = bal_after
    wallet.updated_at = datetime.utcnow()

    tx_code = generate_business_code(db=db, sequence_type=SequenceType.WALLET_TRANSACTION, market_id=partner.market_id)
    tx = WalletTransaction(
        id=uuid.uuid4(),
        transaction_code=tx_code,
        wallet_id=wallet.id,
        partner_id=partner.id,
        transaction_type=WalletTransactionType.COMMISSION_PAYOUT,
        direction="CREDIT",
        amount=stmt.net_commission_payable,
        balance_before=bal_before,
        balance_after=bal_after,
        reference_id=stmt.statement_code,
        notes=f"Net commission payout for statement {stmt.statement_code}",
        created_at=datetime.utcnow()
    )
    db.add(tx)

    # 2. Post balanced double-entry General Ledger entry
    # Debit:  5100 Partner Commission Expense (Gross)
    # Credit: 2400 TDS Payable Section 194H (Withholding)
    # Credit: 2300 Partner Wallet Float (Net)
    post_journal_entry(
        db=db,
        market_id=stmt.market_id,
        reference_type="COMMISSION_SETTLEMENT",
        reference_id=stmt.statement_code,
        description=f"Commission payout & Sec 194H TDS for {partner.partner_code} ({stmt.statement_code})",
        lines=[
            {
                "account_code": ChartOfAccounts.PARTNER_COMMISSION_EXPENSE,
                "debit": stmt.gross_commission,
                "credit": Decimal('0.00'),
                "currency": wallet.currency
            },
            {
                "account_code": ChartOfAccounts.TDS_PAYABLE_194H,
                "debit": Decimal('0.00'),
                "credit": stmt.tds_amount,
                "currency": wallet.currency
            },
            {
                "account_code": ChartOfAccounts.PARTNER_WALLET_LIABILITY,
                "debit": Decimal('0.00'),
                "credit": stmt.net_commission_payable,
                "currency": wallet.currency
            }
        ],
        allow_idempotent_replay=True
    )

    stmt.status = CommissionStatementStatus.SETTLED
    stmt.settled_at = datetime.utcnow()
    stmt.updated_at = datetime.utcnow()

    db.commit()
    return {
        "statement_code": stmt.statement_code,
        "partner_code": partner.partner_code,
        "gross_commission": float(stmt.gross_commission),
        "tds_withheld": float(stmt.tds_amount),
        "net_payout": float(stmt.net_commission_payable),
        "wallet_balance_after": float(bal_after),
        "status": "SETTLED"
    }


# ─── 4. Automated Bank Statement Ingestion & Reconciliation ──────────────────

def ingest_bank_statement_records(
    db: Session,
    batch_reference: str,
    records_data: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Ingests bank statement entries (MT940 / CSV bank feed) and executes automated matching:
    - Matches narration containing invoice codes (e.g. INV-MUM-2026-00001).
    - Matches customer codes (e.g. CUST-MUM-...).
    - Matches outstanding invoice balance.
    - Allocates collections, marks invoices PAID, and posts balanced General Ledger entries.
    """
    total_records = len(records_data)
    auto_matched = 0
    unmatched = 0

    for item in records_data:
        utr = item.get("utr_number") or f"UTR-{uuid.uuid4().hex[:12].upper()}"
        amount = quantize_money(Decimal(str(item.get("amount", "0.00"))))
        narration = str(item.get("narration", ""))
        tx_date = item.get("transaction_date") or datetime.utcnow()

        stmt_rec = BankStatementRecord(
            id=uuid.uuid4(),
            batch_reference=batch_reference,
            bank_name=item.get("bank_name", "HDFC Bank"),
            transaction_date=tx_date,
            value_date=item.get("value_date") or tx_date,
            utr_number=utr,
            amount=amount,
            direction=item.get("direction", "CR"),
            narration=narration,
            reconciliation_status=BankReconciliationStatus.UNMATCHED,
            created_at=datetime.utcnow()
        )
        db.add(stmt_rec)
        db.flush()

        # Automated Matching Engine
        matched_invoice = None

        # Rule 1: Regex scan for Invoice Code in narration e.g. INV-[A-Z]+-\d+-\d+
        inv_match = re.search(r"INV-[A-Z0-9]+-\d+-\d+", narration, re.IGNORECASE)
        if inv_match:
            inv_code = inv_match.group(0).upper()
            matched_invoice = db.query(Invoice).filter(Invoice.invoice_code == inv_code).first()

        # Rule 2: If no invoice code, match by exact customer code in narration + matching outstanding amount
        if not matched_invoice:
            cust_match = re.search(r"CUST-[A-Z0-9-]+", narration, re.IGNORECASE)
            if cust_match:
                cust_code = cust_match.group(0).upper()
                cust = db.query(Customer).filter(Customer.customer_code == cust_code).first()
                if cust:
                    # Look for open invoice with matching amount
                    matched_invoice = db.query(Invoice).filter(
                        Invoice.customer_id == cust.id,
                        Invoice.outstanding_amount == amount,
                        Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.LATE, InvoiceStatus.OVERDUE])
                    ).first()

        # If matched, auto-reconcile
        if matched_invoice and matched_invoice.outstanding_amount > Decimal('0.00'):
            alloc_amt = min(amount, _format_money(matched_invoice.outstanding_amount))
            matched_invoice.paid_amount = _format_money(matched_invoice.paid_amount) + alloc_amt
            matched_invoice.outstanding_amount = _format_money(matched_invoice.outstanding_amount) - alloc_amt
            if matched_invoice.outstanding_amount <= Decimal('0.00'):
                matched_invoice.status = InvoiceStatus.PAID
                matched_invoice.paid_date = datetime.utcnow()
            else:
                matched_invoice.status = InvoiceStatus.PARTIALLY_PAID

            stmt_rec.reconciliation_status = BankReconciliationStatus.AUTO_MATCHED
            stmt_rec.matched_customer_id = matched_invoice.customer_id
            stmt_rec.matched_invoice_id = matched_invoice.id
            stmt_rec.confidence_score = Decimal('100.00')
            stmt_rec.reconciled_at = datetime.utcnow()
            stmt_rec.reconciled_by = "BANK_RECON_AI"

            # Post balanced General Ledger entry:
            # Debit:  1100 Cash / Bank Clearing
            # Credit: 1200 Accounts Receivable
            post_journal_entry(
                db=db,
                market_id=matched_invoice.market_id,
                reference_type="BANK_RECON_PAYMENT",
                reference_id=utr,
                description=f"Auto-reconciled bank collection {utr} for invoice {matched_invoice.invoice_code}",
                lines=[
                    {
                        "account_code": ChartOfAccounts.CASH_CLEARING,
                        "debit": alloc_amt,
                        "credit": Decimal('0.00'),
                        "currency": matched_invoice.currency
                    },
                    {
                        "account_code": ChartOfAccounts.ACCOUNTS_RECEIVABLE,
                        "debit": Decimal('0.00'),
                        "credit": alloc_amt,
                        "currency": matched_invoice.currency
                    }
                ],
                allow_idempotent_replay=True
            )
            auto_matched += 1
        else:
            unmatched += 1

    db.commit()
    return {
        "batch_reference": batch_reference,
        "total_records": total_records,
        "auto_matched_count": auto_matched,
        "unmatched_count": unmatched,
        "match_rate_pct": float(quantize_money(Decimal(str(auto_matched)) / Decimal(str(max(1, total_records))) * Decimal('100.00')))
    }
