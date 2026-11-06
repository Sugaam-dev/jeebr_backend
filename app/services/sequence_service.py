"""
app/services/sequence_service.py

SentinelOS Concurrency-Safe Business Sequence Generator.
Replaces race-prone count() queries with atomic, transaction-locked sequence counters.
Generates contiguous, formatted codes for Invoices, Receipts, Credit Notes, and Payments.
"""

import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from app.models_finance import BillingSequence


class SequenceType:
    INVOICE = "INVOICE"
    CREDIT_NOTE = "CREDIT_NOTE"
    RECEIPT = "RECEIPT"
    PAYMENT = "PAYMENT"
    REFUND = "REFUND"
    CUSTOMER = "CUSTOMER"
    BILLING_ACCOUNT = "BILLING_ACCOUNT"
    SUBSCRIPTION = "SUBSCRIPTION"
    BILLING_RUN = "BILLING_RUN"
    RELOCATION = "RELOCATION"
    SLA_INCIDENT = "SLA_INCIDENT"
    PARTNER = "PARTNER"
    WALLET_TRANSACTION = "WALLET_TRANSACTION"
    COMMISSION_STATEMENT = "COMMISSION_STATEMENT"
    BANK_RECONCILIATION = "BANK_RECONCILIATION"
    ALL = (
        INVOICE, CREDIT_NOTE, RECEIPT, PAYMENT, REFUND, CUSTOMER,
        BILLING_ACCOUNT, SUBSCRIPTION, BILLING_RUN, RELOCATION,
        SLA_INCIDENT, PARTNER, WALLET_TRANSACTION, COMMISSION_STATEMENT, BANK_RECONCILIATION
    )


DEFAULT_PREFIXES = {
    SequenceType.INVOICE: "INV",
    SequenceType.CREDIT_NOTE: "CN",
    SequenceType.RECEIPT: "RCP",
    SequenceType.PAYMENT: "PAY",
    SequenceType.REFUND: "REF",
    SequenceType.CUSTOMER: "CUST",
    SequenceType.BILLING_ACCOUNT: "BA",
    SequenceType.SUBSCRIPTION: "SUB",
    SequenceType.BILLING_RUN: "RUN",
    SequenceType.RELOCATION: "REL",
    SequenceType.SLA_INCIDENT: "SLA",
    SequenceType.PARTNER: "LCO",
    SequenceType.WALLET_TRANSACTION: "WLT",
    SequenceType.COMMISSION_STATEMENT: "CMS",
    SequenceType.BANK_RECONCILIATION: "BRC",
}


def next_sequence_value(
    db: Session,
    sequence_type: str,
    market_id: str = "mumbai",
    year: Optional[int] = None,
    padding: int = 5
) -> int:
    """
    Atomically retrieves and increments the sequence counter within the active transaction.
    Uses pessimistic row locking (with_for_update) to guarantee concurrency safety.
    """
    target_market = (market_id or "mumbai").strip().lower()
    cur_year = year or datetime.utcnow().year

    # 1. Acquire row lock on existing sequence
    seq = (
        db.query(BillingSequence)
        .filter(
            BillingSequence.sequence_type == sequence_type,
            BillingSequence.market_id == target_market,
            BillingSequence.financial_year == cur_year,
        )
        .with_for_update()
        .first()
    )

    if not seq:
        prefix = DEFAULT_PREFIXES.get(sequence_type, sequence_type[:3].upper())
        seq = BillingSequence(
            id=uuid.uuid4(),
            sequence_type=sequence_type,
            market_id=target_market,
            financial_year=cur_year,
            prefix=prefix,
            current_value=1,
            padding_digits=padding,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(seq)
        db.flush()
        return 1

    seq.current_value += 1
    seq.updated_at = datetime.utcnow()
    db.flush()
    return seq.current_value


def generate_business_code(
    db: Session,
    sequence_type: str,
    market_id: str = "mumbai",
    year: Optional[int] = None,
    custom_prefix: Optional[str] = None,
    padding: int = 5
) -> str:
    """
    Generates a standardized, contiguous enterprise business code:
    Format: {PREFIX}-{MARKET}-{YEAR}-{NUMBER:05d}
    Example: INV-MUM-2026-00001
    """
    target_market = (market_id or "mumbai").strip().lower()
    mkt_short = target_market[:3].upper()
    cur_year = year or datetime.utcnow().year
    prefix = custom_prefix or DEFAULT_PREFIXES.get(sequence_type, sequence_type[:3].upper())

    val = next_sequence_value(
        db=db,
        sequence_type=sequence_type,
        market_id=mkt_short,
        year=cur_year,
        padding=padding
    )
    return f"{prefix}-{mkt_short}-{cur_year}-{val:0{padding}d}"


# Backwards-compatible alias
generate_sequential_number = generate_business_code
