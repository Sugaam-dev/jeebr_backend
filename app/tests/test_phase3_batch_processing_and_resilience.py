"""
app/tests/test_phase3_batch_processing_and_resilience.py

Comprehensive Enterprise Test Suite for Phase 3:
1. High-Throughput Batch Billing Run Pre-Run Preview & Anomaly Detection.
2. CFO & Billing Manager Human-in-the-Loop Approval Governance.
3. Sharded Batch Execution, Partition Chunking & Distributed Worker Tracking.
4. Failure Isolation via Dead Letter Queue (DLQ).
5. Operator DLQ Replay, Tariff Correction & Resolution Invariants.
"""

import uuid
import pytest
from decimal import Decimal
from datetime import datetime, timedelta

from app.models import Customer, Invoice
from app.models_crm import CRMCustomer, CRMAddress, AccountType, CustomerLifecycleStatus
from app.models_finance import (
    BillingAccount, BillingSubscription, BillingJobRun, BillingAccountStatus,
    BillingType, SubscriptionStatus, InvoiceStatus, ChartOfAccounts, JournalEntry
)
from app.models_batch import (
    BillingRunApproval, BillingAccountChunk, BillingDeadLetterQueue,
    BillingRunStatus, ChunkStatus
)
from app.services.ledger_service import verify_ledger_balancing, quantize_money
from app.services.batch_billing_service import (
    generate_billing_run_preview, approve_billing_run, reject_billing_run,
    execute_approved_batch_billing_run, retry_dead_letter_queue_item,
    get_billing_run_details, list_dead_letter_queue
)


def _create_test_subscriber(
    db,
    idx: int,
    market_id: str = "mumbai",
    state: str = "Maharashtra",
    unit_price: Decimal = Decimal('1000.00'),
    acct_status: str = BillingAccountStatus.ACTIVE,
    sub_status: str = SubscriptionStatus.ACTIVE,
    billing_type: str = BillingType.POSTPAID,
    due_days_ago: int = 0
) -> tuple:
    """Helper to provision an isolated customer, billing account, and subscription."""
    uid = uuid.uuid4().hex[:6].upper()
    now = datetime.utcnow()
    period_start = now - timedelta(days=30 + due_days_ago)
    period_end = now - timedelta(days=due_days_ago)

    cust = Customer(
        name=f"Subscriber {idx} {uid}",
        email=f"sub_{idx}_{uid.lower()}@testisp.in",
        phone=f"98200{idx:05d}",
        customer_code=f"CUST-P3-{idx:03d}-{uid}",
        market_id=market_id,
        locality="Bandra West",
        segment="Retail",
        plan_name="UltraFiber 300M"
    )
    db.add(cust)
    db.flush()

    crm_cust = CRMCustomer(
        customer_number=f"CRM-P3-{idx:03d}-{uid}",
        legal_name=f"Subscriber {idx} {uid}",
        account_type=AccountType.RESIDENTIAL,
        status=CustomerLifecycleStatus.ACTIVE,
        market_id=market_id,
        legacy_customer_id=cust.id
    )
    db.add(crm_cust)
    db.flush()

    if state:
        addr = CRMAddress(
            crm_customer_id=crm_cust.id,
            address_type="INSTALLATION",
            address_line1="Plot 42, Bandra Kurla Complex",
            city="Mumbai",
            state=state,
            postal_code="400051",
            is_archived=False
        )
        db.add(addr)
        db.flush()

    acct = BillingAccount(
        id=uuid.uuid4(),
        account_number=f"BA-P3-{idx:03d}-{uid}",
        customer_id=cust.id,
        crm_customer_id=crm_cust.id,
        market_id=market_id,
        billing_type=billing_type,
        currency="INR",
        balance=Decimal('0.00'),
        credit_limit=Decimal('5000.00'),
        status=acct_status,
        payment_term_days=15
    )
    db.add(acct)
    db.flush()

    sub = BillingSubscription(
        id=uuid.uuid4(),
        subscription_code=f"SUB-P3-{idx:03d}-{uid}",
        billing_account_id=acct.id,
        customer_id=cust.id,
        market_id=market_id,
        plan_id="plan_300m_ultra",
        plan_name="UltraFiber 300M",
        billing_type=billing_type,
        unit_price=unit_price,
        currency="INR",
        current_period_start=period_start,
        current_period_end=period_end,
        next_billing_date=period_end,
        status=sub_status,
        created_at=period_start
    )
    db.add(sub)
    db.commit()

    return cust, acct, sub


# ─── 1. Pre-Run Preview Audit & Anomaly Detection Tests ───────────────────────

def test_billing_run_dry_run_preview_and_anomaly_detection(db_session):
    """
    Verifies that generate_billing_run_preview:
    1. Accurately projects counts, taxable amount, GST, and gross total.
    2. Identifies anomalies (zero tariff, suspended account, unresolvable place of supply).
    3. Produces zero mutations to invoices and general ledger.
    """
    # 1. Healthy account: Maharashtra, INR 1,000/mo
    _create_test_subscriber(db_session, idx=1, market_id="mumbai_prev", state="Maharashtra", unit_price=Decimal('1000.00'))

    # 2. Anomaly: Zero price tariff
    _create_test_subscriber(db_session, idx=2, market_id="mumbai_prev", state="Maharashtra", unit_price=Decimal('0.00'))

    # 3. Anomaly: Suspended billing account with active subscription
    _create_test_subscriber(
        db_session, idx=3, market_id="mumbai_prev", state="Maharashtra",
        unit_price=Decimal('1200.00'), acct_status=BillingAccountStatus.SUSPENDED
    )

    # 4. Anomaly: Missing state / Place of Supply
    _create_test_subscriber(db_session, idx=4, market_id="mumbai_prev", state="", unit_price=Decimal('800.00'))

    # Record initial counts
    inv_count_before = db_session.query(Invoice).count()
    ledger_count_before = db_session.query(JournalEntry).count()

    # Generate Dry-Run Preview
    preview = generate_billing_run_preview(db_session, market_id="mumbai_prev")

    # Assert Preview Structure
    assert preview.id is not None
    assert preview.run_code.startswith("RUN-MUM-")
    assert preview.status == BillingRunStatus.PREVIEW_GENERATED
    assert preview.is_dry_run is True

    # Assert Financial Projections
    assert preview.projected_invoices_count >= 1
    assert preview.projected_taxable_amount > Decimal('0.00')
    assert preview.projected_gst_amount > Decimal('0.00')
    assert preview.projected_total_amount > preview.projected_taxable_amount

    # Assert Anomaly Detection
    assert preview.anomaly_count >= 3
    anomaly_types = [a["type"] for a in preview.anomaly_details]
    assert "ZERO_PRICE_TARIFF" in anomaly_types
    assert "ACCOUNT_STATUS_MISMATCH" in anomaly_types
    assert "MISSING_PLACE_OF_SUPPLY" in anomaly_types

    # Assert Zero Database Invoicing Mutations
    inv_count_after = db_session.query(Invoice).count()
    ledger_count_after = db_session.query(JournalEntry).count()
    assert inv_count_after == inv_count_before, "Preview must not create Invoice records!"
    assert ledger_count_after == ledger_count_before, "Preview must not create JournalEntry records!"


# ─── 2. CFO / Billing Manager Approval Governance Tests ───────────────────────

def test_cfo_approval_governance_blocks_unapproved_execution(db_session):
    """
    Verifies that execute_approved_batch_billing_run strictly enforces approval governance:
    - Blocks execution on PREVIEW_GENERATED.
    - Blocks execution on REJECTED.
    - Allows execution only when APPROVED.
    """
    # Setup test subscriber
    _create_test_subscriber(db_session, idx=10, market_id="mumbai_gov", unit_price=Decimal('999.00'))

    # Step 1: Generate Preview
    preview = generate_billing_run_preview(db_session, market_id="mumbai_gov")
    assert preview.status == BillingRunStatus.PREVIEW_GENERATED

    # Step 2: Attempt execution while unapproved -> Must raise PermissionError
    with pytest.raises(PermissionError) as exc_info:
        execute_approved_batch_billing_run(db_session, approval_id=preview.id)
    assert "Execution is forbidden until signed off with APPROVED status" in str(exc_info.value)

    # Step 3: Reject preview
    rejected = reject_billing_run(
        db_session,
        approval_id=preview.id,
        rejected_by_id=501,
        reason="Anomaly count exceeds risk tolerance."
    )
    assert rejected.status == BillingRunStatus.REJECTED
    assert "REJECTED:" in rejected.approval_notes

    # Step 4: Attempt execution while rejected -> Must raise PermissionError
    with pytest.raises(PermissionError):
        execute_approved_batch_billing_run(db_session, approval_id=preview.id)

    # Step 5: Generate a new preview and approve it
    preview2 = generate_billing_run_preview(db_session, market_id="mumbai_gov")
    approved = approve_billing_run(
        db_session,
        approval_id=preview2.id,
        approved_by_id=1001,
        notes="Sign-off granted by CFO after review."
    )
    assert approved.status == BillingRunStatus.APPROVED
    assert approved.approved_by_id == 1001
    assert approved.approved_at is not None

    # Step 6: Execute approved run -> Must succeed
    result = execute_approved_batch_billing_run(db_session, approval_id=approved.id, chunk_size=10)
    assert result["status"] == "EXECUTED"
    assert result["actual_invoices_count"] >= 1


# ─── 3. Sharded Chunk Execution & Worker Sharding Tests ───────────────────────

def test_chunked_batch_billing_execution_and_sharding(db_session):
    """
    Verifies high-throughput chunking, worker tracking, and ledger integrity:
    1. Partitions subscriptions into chunks of size `chunk_size`.
    2. Records BillingAccountChunk metadata and assigned worker_id.
    3. Advances subscription billing periods.
    4. Validates double-entry General Ledger balancing.
    5. Reconciles projected totals against actual billed totals.
    """
    # Provision 5 distinct subscribers
    prices = [Decimal('500.00'), Decimal('800.00'), Decimal('1200.00'), Decimal('1500.00'), Decimal('2000.00')]
    subs = []
    for i, p in enumerate(prices):
        _, _, s = _create_test_subscriber(db_session, idx=20 + i, market_id="mumbai_chunk", unit_price=p)
        subs.append(s)

    original_period_ends = {s.id: s.current_period_end for s in subs}

    # Generate and approve preview
    preview = generate_billing_run_preview(db_session, market_id="mumbai_chunk")
    approved = approve_billing_run(db_session, preview.id, approved_by_id=42, notes="Approved for chunk test")

    # Execute with chunk_size = 2 and dedicated worker
    worker_name = "billing_pod_node_03"
    result = execute_approved_batch_billing_run(
        db_session,
        approval_id=approved.id,
        chunk_size=2,
        worker_id=worker_name
    )

    assert result["status"] == "EXECUTED"
    assert result["total_chunks"] >= 3  # 5 items / 2 = 3 chunks
    assert result["records_succeeded"] >= 5
    assert result["records_failed"] == 0

    # Verify chunks in database
    chunks = db_session.query(BillingAccountChunk).filter(
        BillingAccountChunk.job_run_id == uuid.UUID(result["job_run_id"])
    ).order_by(BillingAccountChunk.chunk_number.asc()).all()

    assert len(chunks) == result["total_chunks"]
    for chunk in chunks:
        assert chunk.worker_id == worker_name
        assert chunk.status == ChunkStatus.COMPLETED
        assert chunk.records_succeeded > 0
        assert chunk.records_failed == 0
        assert chunk.total_billed > Decimal('0.00')

    # Verify subscription advancement
    for sub in subs:
        db_session.refresh(sub)
        assert sub.current_period_start == original_period_ends[sub.id], "Period start must match prior period end"
        assert sub.current_period_end > original_period_ends[sub.id], "Period end must advance forward"
        assert sub.next_billing_date == sub.current_period_end

    # Verify General Ledger balancing across all posted invoices
    ledger_check = verify_ledger_balancing(db_session, market_id="mumbai_chunk")
    assert ledger_check["is_fully_balanced"] is True, f"General Ledger out of balance: {ledger_check['unbalanced_entries']}"

    # Verify post-run details query
    details = get_billing_run_details(db_session, approved.id)
    assert details["status"] == BillingRunStatus.EXECUTED
    assert len(details["chunks"]) == len(chunks)
    assert details["actual_invoices_count"] == result["records_succeeded"]


# ─── 4. Dead Letter Queue (DLQ) & Operator Replay Tests ───────────────────────

def test_dead_letter_queue_failure_isolation_and_retry(db_session):
    """
    Verifies fault tolerance and failure isolation:
    1. A corrupted/zero-price subscription inside a chunk fails and is routed to DLQ.
    2. Healthy subscriptions in the same chunk succeed without interruption.
    3. The chunk status reflects PARTIALLY_FAILED.
    4. Operator investigates DLQ, corrects tariff price, and triggers replay.
    5. Replay generates the invoice, advances the subscription, and marks DLQ item RESOLVED.
    """
    # 1. Healthy Sub A (INR 1,000)
    _, _, sub_a = _create_test_subscriber(db_session, idx=31, market_id="mumbai_dlq", unit_price=Decimal('1000.00'))

    # 2. Corrupted Sub B (unit_price = 0, will fail validation during billing)
    _, _, sub_b = _create_test_subscriber(db_session, idx=32, market_id="mumbai_dlq", unit_price=Decimal('0.00'))

    # 3. Healthy Sub C (INR 1,500)
    _, _, sub_c = _create_test_subscriber(db_session, idx=33, market_id="mumbai_dlq", unit_price=Decimal('1500.00'))

    # Generate preview & approve
    preview = generate_billing_run_preview(db_session, market_id="mumbai_dlq")
    approved = approve_billing_run(db_session, preview.id, approved_by_id=777, notes="Sign-off with DLQ test")

    # Execute all 3 in a single chunk of size 10
    result = execute_approved_batch_billing_run(
        db_session,
        approval_id=approved.id,
        chunk_size=10,
        worker_id="dlq_test_worker"
    )

    # Assert failure isolation: Run executed with partial failure
    assert result["status"] == "EXECUTED"
    assert result["records_failed"] == 1
    assert result["records_succeeded"] >= 2

    # Query DLQ
    dlq_items = db_session.query(BillingDeadLetterQueue).filter(
        BillingDeadLetterQueue.subscription_id == sub_b.id
    ).all()
    assert len(dlq_items) == 1
    dlq = dlq_items[0]

    assert dlq.resolved is False
    assert dlq.attempt_count == 1
    assert "has invalid price" in dlq.failure_reason

    # Verify Healthy Subscriptions A and C got invoices
    inv_a = db_session.query(Invoice).filter(Invoice.subscription_id == sub_a.id).first()
    inv_c = db_session.query(Invoice).filter(Invoice.subscription_id == sub_c.id).first()
    assert inv_a is not None, "Sub A invoice must be generated despite Sub B failure"
    assert inv_c is not None, "Sub C invoice must be generated despite Sub B failure"

    # Verify DLQ triage listing
    pending_dlq = list_dead_letter_queue(db_session, market_id="mumbai_dlq", resolved=False)
    assert any(item["id"] == str(dlq.id) for item in pending_dlq)

    # Operator Action: Replay DLQ with fixed price of INR 1,200
    retry_result = retry_dead_letter_queue_item(
        db=db_session,
        dlq_id=dlq.id,
        operator_id="billing_ops_lead_42",
        fixed_price=Decimal('1200.00')
    )

    assert retry_result["status"] == "RESOLVED"
    assert retry_result["invoice_code"].startswith("INV-")
    assert retry_result["total_amount"] > 1200.00  # includes GST

    # Verify DLQ item updated
    db_session.refresh(dlq)
    assert dlq.resolved is True
    assert dlq.resolved_by == "billing_ops_lead_42"
    assert dlq.resolved_at is not None
    assert "Re-billed successfully" in dlq.resolution_notes

    # Verify Sub B subscription advanced
    db_session.refresh(sub_b)
    assert sub_b.unit_price == Decimal('1200.00')
    assert sub_b.current_period_end > sub_b.current_period_start

    # Final Ledger check: double-entry balance must hold
    ledger_check = verify_ledger_balancing(db_session, market_id="mumbai_dlq")
    assert ledger_check["is_fully_balanced"] is True, f"General Ledger invariant violated after DLQ replay: {ledger_check['unbalanced_entries']}"
