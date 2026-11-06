"""
app/routers/partners.py

SentinelOS Multi-Tenant Franchise, LCO Partner Portal & Bank Recon Router.
Exposes endpoints for:
1. Franchise & LCO Partner Directory & Hierarchy
2. Authoritative Prepaid Partner Wallet (Top-ups, Wholesale Deductions, Transactions)
3. White-Label Monthly Commission Statements & Section 194H 5% TDS Settlement
4. Bank Statement (MT940/CSV) Ingestion & Automated Reconciliation
"""

import uuid
from decimal import Decimal
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Query, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.models_partner import (
    FranchisePartner, PartnerWallet, WalletTransaction,
    PartnerCommissionStatement, BankStatementRecord, PartnerType, PartnerStatus
)
from app.auth import get_current_user
from app.markets import get_current_market
from app.services.partner_service import (
    create_franchise_partner, link_sub_partner, topup_partner_wallet,
    deduct_partner_wallet, generate_partner_commission_statement,
    settle_partner_commission_statement, ingest_bank_statement_records
)
from app.services.ledger_service import quantize_money

router = APIRouter(prefix="/partners", tags=["Franchise & LCO Partner Portal"])


# ─── Pydantic Schemas ────────────────────────────────────────────────────────

class PartnerCreateRequest(BaseModel):
    legal_name: str
    trade_name: str
    contact_email: str
    contact_phone: str
    partner_type: str = PartnerType.LCO_OPERATOR
    parent_partner_id: Optional[str] = None
    commission_rate: float = 25.0
    tds_rate: float = 5.0
    gstin: Optional[str] = None
    territory: Optional[str] = None
    credit_limit: float = 0.0


class WalletTopupRequest(BaseModel):
    amount: float
    reference_id: str
    notes: Optional[str] = None


class WalletDeductRequest(BaseModel):
    wholesale_amount: float
    transaction_type: str = "CUSTOMER_ACTIVATION"
    reference_id: Optional[str] = None
    notes: Optional[str] = None


class CommissionGenerateRequest(BaseModel):
    period_start: datetime
    period_end: datetime
    gross_collections: float
    taxable_revenue: Optional[float] = None
    subscribers_count: int = 1


class BankReconIngestRequest(BaseModel):
    batch_reference: str
    records: List[Dict[str, Any]]


# ─── 1. Partner Master Endpoints ─────────────────────────────────────────────

@router.get("")
def list_partners(
    market: str = Depends(get_current_market),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lists franchise and LCO partners for the active market with wallet balances."""
    query = db.query(FranchisePartner)
    if market and market != "all":
        query = query.filter(FranchisePartner.market_id == market)
    partners = query.order_by(FranchisePartner.created_at.desc()).all()

    res = []
    for p in partners:
        w = p.wallet
        res.append({
            "id": str(p.id),
            "partner_code": p.partner_code,
            "parent_partner_id": str(p.parent_partner_id) if p.parent_partner_id else None,
            "partner_type": p.partner_type,
            "legal_name": p.legal_name,
            "trade_name": p.trade_name,
            "contact_email": p.contact_email,
            "contact_phone": p.contact_phone,
            "gstin": p.gstin,
            "pan": p.pan,
            "market_id": p.market_id,
            "territory": p.territory,
            "commission_rate": float(p.commission_rate),
            "tds_rate": float(p.tds_rate),
            "status": p.status,
            "wallet": {
                "balance": float(w.balance) if w else 0.0,
                "credit_limit": float(w.credit_limit) if w else 0.0,
                "available_funds": float(w.balance + w.credit_limit) if w else 0.0,
                "status": w.status if w else "INACTIVE",
                "last_topup_at": w.last_topup_at.isoformat() if w and w.last_topup_at else None
            } if w else None,
            "created_at": p.created_at.isoformat()
        })
    return res


@router.post("")
def create_partner(
    payload: PartnerCreateRequest,
    market: str = Depends(get_current_market),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Onboards a new franchise or LCO partner with an initialized wallet."""
    try:
        partner = create_franchise_partner(
            db=db,
            legal_name=payload.legal_name,
            trade_name=payload.trade_name,
            contact_email=payload.contact_email,
            contact_phone=payload.contact_phone,
            market_id=market or "mumbai",
            partner_type=payload.partner_type,
            parent_partner_id=payload.parent_partner_id,
            commission_rate=Decimal(str(payload.commission_rate)),
            tds_rate=Decimal(str(payload.tds_rate)),
            gstin=payload.gstin,
            territory=payload.territory,
            credit_limit=Decimal(str(payload.credit_limit))
        )
        return {
            "id": str(partner.id),
            "partner_code": partner.partner_code,
            "legal_name": partner.legal_name,
            "status": partner.status,
            "wallet_balance": float(partner.wallet.balance) if partner.wallet else 0.0
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 2. Partner Wallet Endpoints ─────────────────────────────────────────────

@router.post("/{partner_id}/wallet/topup")
def topup_wallet(
    partner_id: str,
    payload: WalletTopupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Replenishes an LCO partner's prepaid wallet float and posts double-entry GL."""
    try:
        tx = topup_partner_wallet(
            db=db,
            partner_id=partner_id,
            amount=Decimal(str(payload.amount)),
            reference_id=payload.reference_id,
            notes=payload.notes
        )
        return {
            "transaction_code": tx.transaction_code,
            "direction": tx.direction,
            "amount": float(tx.amount),
            "balance_before": float(tx.balance_before),
            "balance_after": float(tx.balance_after),
            "reference_id": tx.reference_id,
            "created_at": tx.created_at.isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{partner_id}/wallet/deduct")
def deduct_wallet(
    partner_id: str,
    payload: WalletDeductRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Deducts wholesale operator fee from partner float on customer activation/renewal."""
    try:
        tx = deduct_partner_wallet(
            db=db,
            partner_id=partner_id,
            wholesale_amount=Decimal(str(payload.wholesale_amount)),
            transaction_type=payload.transaction_type,
            reference_id=payload.reference_id,
            notes=payload.notes
        )
        return {
            "transaction_code": tx.transaction_code,
            "direction": tx.direction,
            "amount": float(tx.amount),
            "balance_before": float(tx.balance_before),
            "balance_after": float(tx.balance_after),
            "reference_id": tx.reference_id,
            "created_at": tx.created_at.isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{partner_id}/wallet/transactions")
def list_wallet_transactions(
    partner_id: str,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Returns the immutable transaction audit trail for an LCO partner's wallet."""
    pid = uuid.UUID(partner_id) if not isinstance(partner_id, uuid.UUID) else partner_id
    txs = db.query(WalletTransaction).filter(
        WalletTransaction.partner_id == pid
    ).order_by(WalletTransaction.created_at.desc()).limit(limit).all()

    return [
        {
            "id": str(t.id),
            "transaction_code": t.transaction_code,
            "transaction_type": t.transaction_type,
            "direction": t.direction,
            "amount": float(t.amount),
            "balance_before": float(t.balance_before),
            "balance_after": float(t.balance_after),
            "reference_id": t.reference_id,
            "notes": t.notes,
            "created_at": t.created_at.isoformat()
        }
        for t in txs
    ]


# ─── 3. Commission Statements & Statutory TDS ────────────────────────────────

@router.get("/commission/statements")
def list_commission_statements(
    partner_id: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lists monthly partner commission statements with TDS breakdown."""
    query = db.query(PartnerCommissionStatement)
    if partner_id:
        pid = uuid.UUID(partner_id) if not isinstance(partner_id, uuid.UUID) else partner_id
        query = query.filter(PartnerCommissionStatement.partner_id == pid)
    stmts = query.order_by(PartnerCommissionStatement.created_at.desc()).limit(limit).all()

    return [
        {
            "id": str(s.id),
            "statement_code": s.statement_code,
            "partner_code": s.partner.partner_code if s.partner else "N/A",
            "partner_name": s.partner.trade_name if s.partner else "N/A",
            "period_start": s.period_start.isoformat(),
            "period_end": s.period_end.isoformat(),
            "active_subscribers_count": s.active_subscribers_count,
            "gross_billed_amount": float(s.gross_billed_amount),
            "taxable_revenue": float(s.taxable_revenue),
            "commission_rate": float(s.commission_rate),
            "gross_commission": float(s.gross_commission),
            "tds_rate": float(s.tds_rate),
            "tds_amount": float(s.tds_amount),
            "net_commission_payable": float(s.net_commission_payable),
            "status": s.status,
            "settled_at": s.settled_at.isoformat() if s.settled_at else None,
            "created_at": s.created_at.isoformat()
        }
        for s in stmts
    ]


@router.post("/{partner_id}/commission/generate")
def generate_commission(
    partner_id: str,
    payload: CommissionGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Generates a monthly partner commission statement with Section 194H 5% TDS."""
    try:
        stmt = generate_partner_commission_statement(
            db=db,
            partner_id=partner_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            gross_collections=Decimal(str(payload.gross_collections)),
            taxable_revenue=Decimal(str(payload.taxable_revenue)) if payload.taxable_revenue else None,
            subscribers_count=payload.subscribers_count
        )
        return {
            "id": str(stmt.id),
            "statement_code": stmt.statement_code,
            "gross_commission": float(stmt.gross_commission),
            "tds_amount": float(stmt.tds_amount),
            "net_commission_payable": float(stmt.net_commission_payable),
            "status": stmt.status
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/commission/{statement_id}/settle")
def settle_commission(
    statement_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Settles an approved commission statement to partner wallet float with balanced GL."""
    try:
        res = settle_partner_commission_statement(db=db, statement_id=statement_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 4. Bank Statement Ingestion & Auto-Reconciliation ───────────────────────

@router.post("/bank-recon/ingest")
def ingest_bank_statement(
    payload: BankReconIngestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Ingests MT940 / CSV bank statement records and runs regex automated reconciliation."""
    try:
        summary = ingest_bank_statement_records(
            db=db,
            batch_reference=payload.batch_reference,
            records_data=payload.records
        )
        return summary
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/bank-recon/records")
def list_bank_recon_records(
    batch_reference: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lists ingested bank feed records with match status and confidence score."""
    query = db.query(BankStatementRecord)
    if batch_reference:
        query = query.filter(BankStatementRecord.batch_reference == batch_reference)
    recs = query.order_by(BankStatementRecord.transaction_date.desc()).limit(limit).all()

    return [
        {
            "id": str(r.id),
            "batch_reference": r.batch_reference,
            "bank_name": r.bank_name,
            "transaction_date": r.transaction_date.isoformat(),
            "utr_number": r.utr_number,
            "amount": float(r.amount),
            "direction": r.direction,
            "narration": r.narration,
            "reconciliation_status": r.reconciliation_status,
            "matched_invoice_code": r.matched_invoice.invoice_code if r.matched_invoice else None,
            "confidence_score": float(r.confidence_score),
            "reconciled_at": r.reconciled_at.isoformat() if r.reconciled_at else None,
            "reconciled_by": r.reconciled_by
        }
        for r in recs
    ]
