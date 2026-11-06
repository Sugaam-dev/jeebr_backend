"""
app/services/batch_billing_service.py

SentinelOS High-Throughput Batch Processing & Resilience Service.
Designed for 100k+ to 1M+ subscribers scale:
1. Pre-run preview audit & anomaly detection (zero DB writes to invoices/GL).
2. Human-in-the-Loop CFO / Billing Manager approval governance.
3. Sharded chunk execution with distributed worker tracking.
4. Failure isolation via Dead Letter Queue (DLQ).
5. Operator DLQ replay & resolution policies.
6. Post-execution financial reconciliation (actual vs projected).
"""

import uuid
import traceback
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_

from app.models import Customer, Invoice
from app.models_crm import CRMCustomer, CRMAddress
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingJobRun, InvoiceLineItem,
    BillingAccountStatus, BillingType, SubscriptionStatus, InvoiceStatus,
    ChartOfAccounts
)
from app.models_batch import (
    BillingRunApproval, BillingAccountChunk, BillingDeadLetterQueue,
    BillingRunStatus, ChunkStatus
)
from app.services.billing_service import generate_invoice, _format_money
from app.services.tax_service import compute_gst, resolve_installation_state
from app.services.ledger_service import quantize_money, post_journal_entry
from app.services.sequence_service import generate_business_code, SequenceType
from app.services.provisioning_catalog import get_service_plan_by_id

logger = logging.getLogger(__name__)


# ─── 1. Pre-Run Preview Audit & Anomaly Detection ────────────────────────────

def generate_billing_run_preview(
    db: Session,
    market_id: Optional[str] = None,
    billing_date: Optional[datetime] = None,
    subscription_ids: Optional[List[Any]] = None
) -> BillingRunApproval:
    """
    Simulates a recurring billing run (dry-run) without modifying invoices or general ledger.
    Aggregates projected counts, taxable amount, GST, and gross total.
    Identifies financial and configuration anomalies (missing address, zero price, inactive accounts).
    Generates a BillingRunApproval record requiring CFO / Billing Manager sign-off before execution.
    """
    effective_date = billing_date or datetime.utcnow()
    mkt = market_id or "mumbai"

    # Query eligible active postpaid subscriptions due on or before effective_date + 1 day
    cutoff = effective_date + timedelta(days=1)
    query = db.query(BillingSubscription).filter(
        BillingSubscription.status == SubscriptionStatus.ACTIVE,
        BillingSubscription.billing_type == BillingType.POSTPAID,
        BillingSubscription.current_period_end <= cutoff
    )
    if market_id and market_id != "all":
        query = query.filter(BillingSubscription.market_id == market_id)
    if subscription_ids:
        query = query.filter(BillingSubscription.id.in_(subscription_ids))

    subs = query.order_by(BillingSubscription.id.asc()).all()

    projected_invoices_count = 0
    projected_taxable_amount = Decimal('0.00')
    projected_gst_amount = Decimal('0.00')
    projected_total_amount = Decimal('0.00')
    anomalies: List[Dict[str, Any]] = []

    for sub in subs:
        account = sub.billing_account
        customer = sub.customer

        # Anomaly 1: Missing Billing Account or Customer record
        if not account or not customer:
            anomalies.append({
                "subscription_id": str(sub.id),
                "customer_id": sub.customer_id,
                "type": "MISSING_ENTITY",
                "severity": "CRITICAL",
                "message": "Subscription lacks linked BillingAccount or Customer master."
            })
            continue

        # Anomaly 2: Inactive or suspended billing account with active subscription
        if account.status != BillingAccountStatus.ACTIVE:
            anomalies.append({
                "subscription_id": str(sub.id),
                "billing_account_id": str(account.id),
                "account_number": account.account_number,
                "type": "ACCOUNT_STATUS_MISMATCH",
                "severity": "HIGH",
                "message": f"BillingAccount is '{account.status}' but subscription is '{sub.status}'."
            })

        # Anomaly 3: Invalid or Zero Price
        unit_price = _format_money(sub.unit_price or Decimal('0.00'))
        if unit_price <= Decimal('0.00'):
            anomalies.append({
                "subscription_id": str(sub.id),
                "subscription_code": sub.subscription_code,
                "plan_name": sub.plan_name,
                "type": "ZERO_PRICE_TARIFF",
                "severity": "HIGH",
                "message": "Subscription has zero or negative tariff price."
            })

        # Anomaly 4: Missing address / Section 12(11)(a) Place of Supply resolution failure
        pos = resolve_installation_state(db, customer.id)
        if not pos:
            anomalies.append({
                "subscription_id": str(sub.id),
                "customer_id": customer.id,
                "customer_code": customer.customer_code,
                "type": "MISSING_PLACE_OF_SUPPLY",
                "severity": "MEDIUM",
                "message": "Section 12(11)(a) Place of Supply cannot be resolved; lacks CRM installation address state."
            })

        # Anomaly 5: Overdue Balance Exceeds Credit Limit
        if account.credit_limit > Decimal('0.00') and account.balance > account.credit_limit:
            anomalies.append({
                "subscription_id": str(sub.id),
                "billing_account_id": str(account.id),
                "account_number": account.account_number,
                "type": "CREDIT_LIMIT_EXCEEDED",
                "severity": "LOW",
                "message": f"Account balance ({account.balance}) exceeds credit limit ({account.credit_limit})."
            })

        # Tax calculation projection
        if unit_price > Decimal('0.00'):
            try:
                tax_calc = compute_gst(
                    subtotal=unit_price,
                    discount_amount=Decimal('0.00'),
                    market_id=sub.market_id,
                    customer_state=pos or getattr(customer, 'state', None) or sub.market_id,
                    currency=sub.currency
                )
                projected_invoices_count += 1
                projected_taxable_amount += tax_calc.taxable_amount
                projected_gst_amount += tax_calc.total_tax
                projected_total_amount += tax_calc.total_amount
            except Exception as e:
                anomalies.append({
                    "subscription_id": str(sub.id),
                    "type": "TAX_PROJECTION_ERROR",
                    "severity": "HIGH",
                    "message": f"GST calculation failed: {str(e)}"
                })

    run_code = generate_business_code(
        db=db,
        sequence_type=SequenceType.BILLING_RUN,
        market_id=mkt
    )

    approval = BillingRunApproval(
        id=uuid.uuid4(),
        run_code=run_code,
        market_id=mkt,
        billing_date=effective_date,
        is_dry_run=True,
        status=BillingRunStatus.PREVIEW_GENERATED,
        projected_invoices_count=projected_invoices_count,
        projected_taxable_amount=quantize_money(projected_taxable_amount),
        projected_gst_amount=quantize_money(projected_gst_amount),
        projected_total_amount=quantize_money(projected_total_amount),
        anomaly_count=len(anomalies),
        anomaly_details=anomalies,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    return approval


# ─── 2. CFO / Billing Manager Approval Governance ────────────────────────────

def approve_billing_run(
    db: Session,
    approval_id: Any,
    approved_by_id: int,
    notes: Optional[str] = None
) -> BillingRunApproval:
    """
    Human-in-the-Loop governance:
    Authorized CFO or Billing Manager reviews preview anomalies and grants approval.
    """
    try:
        aid = uuid.UUID(str(approval_id)) if not isinstance(approval_id, uuid.UUID) else approval_id
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.id == aid).first()
    except (ValueError, AttributeError):
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.run_code == str(approval_id)).first()

    if not approval:
        raise ValueError(f"BillingRunApproval '{approval_id}' not found.")

    if approval.status not in (BillingRunStatus.PREVIEW_GENERATED, BillingRunStatus.PENDING_APPROVAL):
        raise ValueError(f"Cannot approve billing run with status '{approval.status}'.")

    approval.status = BillingRunStatus.APPROVED
    approval.approved_by_id = approved_by_id
    approval.approval_notes = notes or "Batch billing run approved by finance authority."
    approval.approved_at = datetime.utcnow()
    approval.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(approval)
    return approval


def reject_billing_run(
    db: Session,
    approval_id: Any,
    rejected_by_id: int,
    reason: str
) -> BillingRunApproval:
    """
    Rejects a billing preview run, preventing automated or manual execution.
    """
    try:
        aid = uuid.UUID(str(approval_id)) if not isinstance(approval_id, uuid.UUID) else approval_id
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.id == aid).first()
    except (ValueError, AttributeError):
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.run_code == str(approval_id)).first()

    if not approval:
        raise ValueError(f"BillingRunApproval '{approval_id}' not found.")

    approval.status = BillingRunStatus.REJECTED
    approval.approved_by_id = rejected_by_id
    approval.approval_notes = f"REJECTED: {reason}"
    approval.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(approval)
    return approval


# ─── 3. Sharded High-Throughput Batch Execution with Failure Isolation ───────

def execute_approved_batch_billing_run(
    db: Session,
    approval_id: Any,
    chunk_size: int = 500,
    worker_id: str = "batch_worker_1",
    subscription_ids: Optional[List[Any]] = None
) -> Dict[str, Any]:
    """
    Executes an APPROVED billing run partitioned into chunks of `chunk_size` accounts.
    Enforces:
    - Block execution unless status is APPROVED.
    - Transaction boundary per chunk.
    - Failure isolation: errors for one account are captured in BillingDeadLetterQueue (DLQ),
      allowing the remaining accounts in the chunk to complete successfully.
    - Durable job execution tracking via BillingJobRun.
    - Subscription billing period advancement.
    - Post-execution financial reconciliation comparing actuals against projected totals.
    """
    try:
        aid = uuid.UUID(str(approval_id)) if not isinstance(approval_id, uuid.UUID) else approval_id
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.id == aid).first()
    except (ValueError, AttributeError):
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.run_code == str(approval_id)).first()

    if not approval:
        raise ValueError(f"BillingRunApproval '{approval_id}' not found.")

    if approval.status != BillingRunStatus.APPROVED:
        raise PermissionError(
            f"Billing run '{approval.run_code}' is in status '{approval.status}'. "
            f"Execution is forbidden until signed off with APPROVED status."
        )

    # Transition to EXECUTING
    approval.status = BillingRunStatus.EXECUTING
    approval.updated_at = datetime.utcnow()

    job_run = BillingJobRun(
        id=uuid.uuid4(),
        job_type="RECURRING_BILLING",
        market_id=approval.market_id,
        status="RUNNING",
        attempt_count=1,
        start_time=datetime.utcnow(),
        summary_metadata={"run_code": approval.run_code, "approval_id": str(approval.id)}
    )
    db.add(job_run)
    approval.execution_job_id = job_run.id
    db.commit()

    # Query eligible subscriptions
    cutoff = approval.billing_date + timedelta(days=1)
    query = db.query(BillingSubscription).filter(
        BillingSubscription.status == SubscriptionStatus.ACTIVE,
        BillingSubscription.billing_type == BillingType.POSTPAID,
        BillingSubscription.current_period_end <= cutoff
    )
    if approval.market_id and approval.market_id != "all":
        query = query.filter(BillingSubscription.market_id == approval.market_id)
    if subscription_ids:
        query = query.filter(BillingSubscription.id.in_(subscription_ids))

    subs = query.order_by(BillingSubscription.id.asc()).all()

    # Shard into chunks
    chunks_list = [subs[i:i + chunk_size] for i in range(0, len(subs), chunk_size)] if subs else []
    total_chunks = len(chunks_list)

    total_succeeded = 0
    total_failed = 0
    total_billed = Decimal('0.00')

    for chunk_idx, sub_batch in enumerate(chunks_list):
        chunk_record = BillingAccountChunk(
            id=uuid.uuid4(),
            job_run_id=job_run.id,
            chunk_number=chunk_idx + 1,
            total_chunks=total_chunks,
            accounts_count=len(sub_batch),
            status=ChunkStatus.PROCESSING,
            worker_id=worker_id,
            started_at=datetime.utcnow(),
            records_succeeded=0,
            records_failed=0,
            total_billed=Decimal('0.00')
        )
        db.add(chunk_record)
        db.commit()

        for sub in sub_batch:
            try:
                # Tariff sanity check
                price = _format_money(sub.unit_price or Decimal('0.00'))
                if price <= Decimal('0.00'):
                    raise ValueError(f"Subscription #{sub.id} ({sub.plan_name}) has invalid price: {price}")

                inv = generate_invoice(
                    db=db,
                    subscription_id=str(sub.id),
                    billing_period_start=sub.current_period_start,
                    billing_period_end=sub.current_period_end
                )

                # Advance subscription billing cycle
                plan = get_service_plan_by_id(sub.plan_id)
                validity = plan.get("validity_days", 30) if plan else 30
                sub.current_period_start = sub.current_period_end
                sub.current_period_end = sub.current_period_start + timedelta(days=validity)
                sub.next_billing_date = sub.current_period_end
                sub.updated_at = datetime.utcnow()

                chunk_record.records_succeeded += 1
                chunk_record.total_billed += _format_money(inv.total_amount)
                total_succeeded += 1
                total_billed += _format_money(inv.total_amount)
                db.commit()

            except Exception as exc:
                db.rollback()
                # Isolate failure: Route to Dead Letter Queue (DLQ)
                chunk_record.records_failed += 1
                total_failed += 1

                dlq_item = BillingDeadLetterQueue(
                    id=uuid.uuid4(),
                    job_run_id=job_run.id,
                    chunk_id=chunk_record.id,
                    subscription_id=sub.id,
                    billing_account_id=sub.billing_account_id,
                    market_id=sub.market_id,
                    failure_reason=str(exc),
                    stack_trace=traceback.format_exc(),
                    attempt_count=1,
                    resolved=False,
                    created_at=datetime.utcnow()
                )
                db.add(dlq_item)
                # Re-merge chunk_record into session after rollback
                db.add(chunk_record)
                db.commit()

        # Update chunk completion status
        if chunk_record.records_failed == 0:
            chunk_record.status = ChunkStatus.COMPLETED
        elif chunk_record.records_succeeded > 0:
            chunk_record.status = ChunkStatus.PARTIALLY_FAILED
        else:
            chunk_record.status = ChunkStatus.FAILED
        chunk_record.completed_at = datetime.utcnow()
        db.commit()

    # Finalize Job Run and Approval
    job_run.finish_time = datetime.utcnow()
    job_run.records_processed = total_succeeded + total_failed
    job_run.records_succeeded = total_succeeded
    job_run.records_failed = total_failed
    job_run.status = "COMPLETED" if total_failed == 0 else "PARTIALLY_FAILED"

    approval.status = BillingRunStatus.EXECUTED
    approval.actual_invoices_count = total_succeeded
    approval.actual_total_billed = total_billed
    approval.updated_at = datetime.utcnow()
    db.commit()

    return {
        "status": "EXECUTED",
        "run_code": approval.run_code,
        "approval_id": str(approval.id),
        "job_run_id": str(job_run.id),
        "total_chunks": total_chunks,
        "records_processed": total_succeeded + total_failed,
        "records_succeeded": total_succeeded,
        "records_failed": total_failed,
        "projected_invoices_count": approval.projected_invoices_count,
        "actual_invoices_count": total_succeeded,
        "projected_total": float(approval.projected_total_amount),
        "actual_total_billed": float(total_billed),
        "variance": float(total_billed - approval.projected_total_amount),
        "worker_id": worker_id
    }


# ─── 4. Dead Letter Queue (DLQ) Replay & Resolution ──────────────────────────

def retry_dead_letter_queue_item(
    db: Session,
    dlq_id: Any,
    operator_id: str,
    fixed_price: Optional[Decimal] = None
) -> Dict[str, Any]:
    """
    Replays billing for an unbillable subscription quarantined in the Dead Letter Queue.
    Allows operator to inject missing price or corrected metadata before retrying.
    Upon successful invoice generation and ledger posting, marks the DLQ item resolved.
    """
    try:
        qid = uuid.UUID(str(dlq_id)) if not isinstance(dlq_id, uuid.UUID) else dlq_id
        dlq = db.query(BillingDeadLetterQueue).filter(BillingDeadLetterQueue.id == qid).first()
    except (ValueError, AttributeError):
        dlq = None

    if not dlq:
        raise ValueError(f"DLQ record '{dlq_id}' not found.")

    if dlq.resolved:
        return {
            "status": "ALREADY_RESOLVED",
            "dlq_id": str(dlq.id),
            "message": f"DLQ item was previously resolved on {dlq.resolved_at} by {dlq.resolved_by}."
        }

    sub = db.query(BillingSubscription).filter(BillingSubscription.id == dlq.subscription_id).first()
    if not sub:
        raise ValueError(f"Subscription '{dlq.subscription_id}' associated with DLQ record not found.")

    if fixed_price is not None and fixed_price > Decimal('0.00'):
        sub.unit_price = quantize_money(fixed_price)
        db.flush()

    try:
        price = _format_money(sub.unit_price or Decimal('0.00'))
        if price <= Decimal('0.00'):
            raise ValueError(f"Subscription #{sub.id} still has invalid price: {price}")

        inv = generate_invoice(
            db=db,
            subscription_id=str(sub.id),
            billing_period_start=sub.current_period_start,
            billing_period_end=sub.current_period_end
        )

        plan = get_service_plan_by_id(sub.plan_id)
        validity = plan.get("validity_days", 30) if plan else 30
        sub.current_period_start = sub.current_period_end
        sub.current_period_end = sub.current_period_start + timedelta(days=validity)
        sub.next_billing_date = sub.current_period_end
        sub.updated_at = datetime.utcnow()

        dlq.resolved = True
        dlq.resolved_at = datetime.utcnow()
        dlq.resolved_by = operator_id
        dlq.resolution_notes = f"Re-billed successfully: generated invoice {inv.invoice_code} for INR {inv.total_amount:.2f}."
        db.commit()

        return {
            "status": "RESOLVED",
            "dlq_id": str(dlq.id),
            "invoice_id": inv.id,
            "invoice_code": inv.invoice_code,
            "total_amount": float(inv.total_amount),
            "resolved_by": operator_id,
            "notes": dlq.resolution_notes
        }

    except Exception as exc:
        db.rollback()
        dlq.attempt_count += 1
        dlq.failure_reason = f"Retry #{dlq.attempt_count} failed: {str(exc)}"
        db.commit()
        raise exc


# ─── 5. Auditing & Reporting Helpers ─────────────────────────────────────────

def get_billing_run_details(db: Session, approval_id: Any) -> Dict[str, Any]:
    """Fetches comprehensive audit record of a billing run including preview, chunks, and DLQ."""
    try:
        aid = uuid.UUID(str(approval_id)) if not isinstance(approval_id, uuid.UUID) else approval_id
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.id == aid).first()
    except (ValueError, AttributeError):
        approval = db.query(BillingRunApproval).filter(BillingRunApproval.run_code == str(approval_id)).first()

    if not approval:
        raise ValueError(f"Billing run '{approval_id}' not found.")

    chunks = []
    dlq_items = []
    if approval.execution_job_id:
        chunks = db.query(BillingAccountChunk).filter(
            BillingAccountChunk.job_run_id == approval.execution_job_id
        ).order_by(BillingAccountChunk.chunk_number.asc()).all()

        dlq_items = db.query(BillingDeadLetterQueue).filter(
            BillingDeadLetterQueue.job_run_id == approval.execution_job_id
        ).all()

    return {
        "run_code": approval.run_code,
        "market_id": approval.market_id,
        "billing_date": approval.billing_date.isoformat() if approval.billing_date else None,
        "status": approval.status,
        "projected_invoices_count": approval.projected_invoices_count,
        "projected_total_amount": float(approval.projected_total_amount),
        "actual_invoices_count": approval.actual_invoices_count,
        "actual_total_billed": float(approval.actual_total_billed),
        "anomaly_count": approval.anomaly_count,
        "anomalies": approval.anomaly_details,
        "approved_by_id": approval.approved_by_id,
        "approved_at": approval.approved_at.isoformat() if approval.approved_at else None,
        "approval_notes": approval.approval_notes,
        "chunks": [
            {
                "chunk_number": c.chunk_number,
                "status": c.status,
                "accounts_count": c.accounts_count,
                "records_succeeded": c.records_succeeded,
                "records_failed": c.records_failed,
                "total_billed": float(c.total_billed),
                "worker_id": c.worker_id
            }
            for c in chunks
        ],
        "dlq_count": len(dlq_items),
        "dlq_items": [
            {
                "id": str(d.id),
                "subscription_id": str(d.subscription_id),
                "failure_reason": d.failure_reason,
                "resolved": d.resolved,
                "attempt_count": d.attempt_count
            }
            for d in dlq_items
        ]
    }


def list_dead_letter_queue(
    db: Session,
    market_id: Optional[str] = None,
    resolved: Optional[bool] = False
) -> List[Dict[str, Any]]:
    """Lists dead-letter queue records for operational triage."""
    query = db.query(BillingDeadLetterQueue)
    if market_id:
        query = query.filter(BillingDeadLetterQueue.market_id == market_id)
    if resolved is not None:
        query = query.filter(BillingDeadLetterQueue.resolved == resolved)

    records = query.order_by(BillingDeadLetterQueue.created_at.desc()).all()
    return [
        {
            "id": str(r.id),
            "job_run_id": str(r.job_run_id) if r.job_run_id else None,
            "chunk_id": str(r.chunk_id) if r.chunk_id else None,
            "subscription_id": str(r.subscription_id),
            "billing_account_id": str(r.billing_account_id),
            "market_id": r.market_id,
            "failure_reason": r.failure_reason,
            "attempt_count": r.attempt_count,
            "resolved": r.resolved,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolved_by": r.resolved_by,
            "resolution_notes": r.resolution_notes,
            "created_at": r.created_at.isoformat()
        }
        for r in records
    ]
