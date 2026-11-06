"""
app/models_batch.py

SentinelOS High-Throughput Batch Processing & Resilience Domain Models.
Supports 100k+ to 1M+ subscriber scale batch billing execution:
1. BillingRunApproval: Pre-run preview audit, anomaly detection, and CFO/Billing Manager sign-off.
2. BillingAccountChunk: Work sharding / partition slices for distributed parallel workers.
3. BillingDeadLetterQueue: Isolated dead-letter storage for unbillable accounts with replay policies.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Numeric, CheckConstraint, Index, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID
from app.models_finance import BillingJobRun, BillingSubscription, BillingAccount


class BillingRunStatus:
    PREVIEW_GENERATED = "PREVIEW_GENERATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    ALL = (PREVIEW_GENERATED, PENDING_APPROVAL, APPROVED, REJECTED, EXECUTING, EXECUTED, CANCELLED)


class ChunkStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"
    ALL = (PENDING, PROCESSING, COMPLETED, FAILED, PARTIALLY_FAILED)


# ─── 1. Billing Run Preview & Approval ───────────────────────────────────────

class BillingRunApproval(Base):
    __tablename__ = "billing_run_approvals"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_code = Column(String(32), unique=True, nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)
    billing_date = Column(DateTime, nullable=False)
    is_dry_run = Column(Boolean, default=False, nullable=False)
    status = Column(String(32), default=BillingRunStatus.PREVIEW_GENERATED, nullable=False, index=True)

    # Pre-Run Projected Metrics
    projected_invoices_count = Column(Integer, default=0, nullable=False)
    projected_taxable_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    projected_gst_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    projected_total_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    # Pre-Run Anomaly Audit
    anomaly_count = Column(Integer, default=0, nullable=False)
    anomaly_details = Column(JSON, default=list, nullable=False)

    # Human-In-The-Loop Governance
    approved_by_id = Column(Integer, nullable=True)
    approval_notes = Column(Text, nullable=True)
    approved_at = Column(DateTime, nullable=True)

    # Execution Linkage
    execution_job_id = Column(GUID(), ForeignKey("billing_job_runs.id", ondelete="SET NULL"), nullable=True)
    actual_invoices_count = Column(Integer, default=0, nullable=False)
    actual_total_billed = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    execution_job = relationship("BillingJobRun")

    __table_args__ = (
        CheckConstraint(f"status IN {BillingRunStatus.ALL}", name="ck_billing_run_status"),
        Index("ix_billing_run_approvals_market_status", "market_id", "status"),
    )


# ─── 2. Partitioned Account Chunks ───────────────────────────────────────────

class BillingAccountChunk(Base):
    __tablename__ = "billing_account_chunks"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    job_run_id = Column(GUID(), ForeignKey("billing_job_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_number = Column(Integer, nullable=False)
    total_chunks = Column(Integer, nullable=False)
    accounts_count = Column(Integer, default=0, nullable=False)

    status = Column(String(32), default=ChunkStatus.PENDING, nullable=False, index=True)
    worker_id = Column(String(100), nullable=True, index=True)

    records_succeeded = Column(Integer, default=0, nullable=False)
    records_failed = Column(Integer, default=0, nullable=False)
    total_billed = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    error_details = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    job_run = relationship("BillingJobRun")

    __table_args__ = (
        CheckConstraint(f"status IN {ChunkStatus.ALL}", name="ck_chunk_status"),
        Index("ix_billing_account_chunks_job_status", "job_run_id", "status"),
    )


# ─── 3. Dead Letter Queue (DLQ) ──────────────────────────────────────────────

class BillingDeadLetterQueue(Base):
    __tablename__ = "billing_dead_letter_queue"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    job_run_id = Column(GUID(), ForeignKey("billing_job_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    chunk_id = Column(GUID(), ForeignKey("billing_account_chunks.id", ondelete="SET NULL"), nullable=True)
    subscription_id = Column(GUID(), ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    billing_account_id = Column(GUID(), ForeignKey("billing_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    market_id = Column(String(32), default="mumbai", nullable=False, index=True)

    failure_reason = Column(Text, nullable=False)
    stack_trace = Column(Text, nullable=True)
    attempt_count = Column(Integer, default=1, nullable=False)

    resolved = Column(Boolean, default=False, nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String(100), nullable=True)
    resolution_notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    subscription = relationship("BillingSubscription")
    billing_account = relationship("BillingAccount")
    job_run = relationship("BillingJobRun")
    chunk = relationship("BillingAccountChunk")

    __table_args__ = (
        Index("ix_billing_dlq_resolved_market", "resolved", "market_id"),
    )
