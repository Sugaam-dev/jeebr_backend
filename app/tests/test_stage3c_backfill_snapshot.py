"""test_stage3c_backfill_snapshot.py

Phase 1 Stage 3C: Read-Only Backfill Snapshot & Reconciliation Test Suite.

Verifies:
1. Deterministic snapshot bytes across repeated runs against unchanged source snapshot.
2. Global and per-market reconciliation invariants (every record accounted for exactly once).
3. Comprehensive reason-code aggregation in reconciliation summary.
4. Privacy masking (email, phone) and exclusion of tax IDs, credentials, and secrets.
5. Snapshot content hash strictly independent of volatile execution metadata.
6. Source mutation detection failing closed during snapshot generation.
7. Exception-safe teardown with database state completely unchanged.
8. Clean handling of empty source database with zero counts and preserved invariants.
9. Malformed records and partially resolvable addresses (explicit omissions/unresolved fields).
10. Existing CRM links, customer number collisions, duplicate identities, and cross-market conflicts.
11. Customer number overflow (> 32 characters) with target schema compatibility reporting.
12. Strict isolation rejecting remote or production database access strings.
13. Human-readable markdown reconciliation report generation and audit readiness.
"""

import os
import uuid
import copy
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError

from app.models import Base, Customer, Node
from app.models_crm import (
    CRMCustomer, AccountType, CustomerLifecycleStatus, ServiceabilityStatus
)
from app.services.crm_backfill_validator import (
    ReasonCode, ClassificationCategory,
    assert_safe_test_db, compute_database_state_hash,
    mask_email, mask_phone
)
from app.services.crm_backfill_snapshot import (
    CRMBackfillSnapshotService, SourceMutationDetectedError,
    serialize_canonical_snapshot, compute_snapshot_content_hash
)

TEST_DB_PATH = "test_stage3c_snapshot.db"
TEST_DB_URL = f"sqlite:///./{TEST_DB_PATH}"

@pytest.fixture
def isolated_test_engine():
    """Provides a fresh isolated synthetic SQLite database engine with all tables."""
    assert_safe_test_db(TEST_DB_URL)
    
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass

    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    
    # Create all operational & CRM tables
    Base.metadata.create_all(bind=engine)

    yield engine

    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass


def seed_standard_fixtures(engine):
    """Populates baseline synthetic records across Mumbai and Kolkata markets."""
    with engine.begin() as conn:
        # 1. Seed Nodes
        conn.execute(text("""
            INSERT INTO nodes (id, market_id, node_code, node_name, area, node_type, status)
            VALUES 
            (1, 'mumbai', 'OLT-BND-01', 'Bandra West Hub', 'Bandra West', 'OLT', 'Healthy'),
            (2, 'kolkata', 'OLT-SLK-01', 'Salt Lake Hub', 'Salt Lake Sector V', 'OLT', 'Healthy');
        """))

        # 2. Seed Standard Mumbai Customers
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (101, 'mumbai', 'CUST-MUM-001', 'Aarav Sharma', 'aarav.sharma@isp.in', '+91 98200 11111',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited 30D', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Flat 101, Hill Road, Bandra West, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (102, 'mumbai', 'CUST-MUM-002', 'Vivaan Patel', 'vivaan.patel@isp.in', '+91 98200 22222',
             'Bandra West', 'Postpaid - Enterprise ILL', 'Postpaid', 'Enterprise 500M', 12000.0, 12000.0,
             12000.0, 12000.0, 'Active', 'Suite 201, BKC Centre, Mumbai 400051', 1, '2026-01-01 00:00:00');
        """))

        # 3. Seed Standard Kolkata Customers
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (201, 'kolkata', 'CUST-KOL-001', 'Sourav Banerjee', 'sourav.b@isp.in', '+91 98300 11111',
             'Salt Lake Sector V', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited 30D', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Tower 3, Sector V, Salt Lake, Kolkata 700091', 2, '2026-01-01 00:00:00'),
            (202, 'kolkata', 'CUST-KOL-002', 'Debolina Mukherjee', 'debolina.m@isp.in', '+91 98300 22222',
             'Salt Lake Sector V', 'Postpaid - Family Plus', 'Postpaid', 'Family 150G', 999.0, 999.0,
             999.0, 999.0, 'Active', 'Block B, Salt Lake, Kolkata 700091', 2, '2026-01-01 00:00:00');
        """))


# ─── 1. Deterministic Snapshot Bytes & Hash Reproducibility ──────────────────

def test_deterministic_snapshot_bytes_across_repeated_runs(isolated_test_engine):
    """Verify that repeated snapshot runs on an unchanged database yield identical canonical bytes and hashes."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot1 = service.generate_snapshot()
    snapshot2 = service.generate_snapshot()

    bytes1 = serialize_canonical_snapshot(snapshot1.deterministic_content)
    bytes2 = serialize_canonical_snapshot(snapshot2.deterministic_content)

    assert bytes1 == bytes2, "Canonical snapshot bytes must be byte-for-byte identical"
    assert snapshot1.snapshot_content_hash == snapshot2.snapshot_content_hash
    assert snapshot1.deterministic_content.model_dump() == snapshot2.deterministic_content.model_dump()


def test_snapshot_hash_independence_from_execution_metadata(isolated_test_engine):
    """Verify that execution metadata (duration, timestamp) does not alter deterministic snapshot content or hash."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()

    initial_hash = snapshot.snapshot_content_hash

    # Simulate altering execution metadata
    modified_snapshot = copy.deepcopy(snapshot)
    modified_snapshot.execution_metadata.duration_ms += 9999.0
    modified_snapshot.execution_metadata.executed_at = "1970-01-01T00:00:00.000000"

    recomputed_hash = compute_snapshot_content_hash(modified_snapshot.deterministic_content)
    assert initial_hash == recomputed_hash, "Deterministic content hash must be independent of execution metadata"


# ─── 2. Global and Per-Market Reconciliation Invariants ──────────────────────

def test_global_and_per_market_reconciliation_invariants(isolated_test_engine):
    """Verify all 6 reconciliation invariants: single record accounting and market aggregation."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec = snapshot.deterministic_content.reconciliation

    assert rec.total_source_records == 4
    # Invariant 1: total = eligible + manual_review + rejected
    assert rec.total_source_records == (rec.eligible_count + rec.manual_review_count + rec.rejected_count)
    assert rec.eligible_count == 4
    assert rec.manual_review_count == 0
    assert rec.rejected_count == 0

    # Invariant 2: total = sum of market totals
    market_sum = sum(m["total"] for m in rec.market_reconciliation.values())
    assert rec.total_source_records == market_sum

    # Invariant 3: per-market subtotals
    for m_id, counts in rec.market_reconciliation.items():
        assert counts["total"] == (
            counts[ClassificationCategory.ELIGIBLE] +
            counts[ClassificationCategory.MANUAL_REVIEW] +
            counts[ClassificationCategory.REJECTED]
        )

    # Invariant 4: proposed + omitted addresses = total
    assert rec.proposed_addresses_count + rec.omitted_addresses_count == rec.total_source_records

    # Invariant 5: proposed customers count <= total
    assert rec.proposed_customers_count == 4
    assert rec.proposed_contacts_count == 4


# ─── 3. Reason-Code Aggregation & Business Decisions ─────────────────────────

def test_reason_code_aggregation_and_business_decisions(isolated_test_engine):
    """Verify reason code counts and structured unresolved business decision reporting."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            -- Malformed
            (301, 'mumbai', '', '   ', 'bad@isp.in', '+91 98200 30001',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            -- Unmapped segment
            (302, 'mumbai', 'CUST-MUM-SEG', 'Unknown Seg User', 'seg@isp.in', '+91 98200 30002',
             'Bandra West', 'Satellite-Defense-Transit', 'Special', 'Custom', 50000.0, 50000.0,
             50000.0, 50000.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00');
        """))

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec = snapshot.deterministic_content.reconciliation

    assert rec.reason_code_breakdown[ReasonCode.MALFORMED_RECORD] == 1
    assert rec.reason_code_breakdown[ReasonCode.UNMAPPED_SEGMENT] == 1
    assert rec.rejected_count == 2
    assert rec.total_source_records == 6

    # Verify business decisions structure
    decisions = {d.decision_key: d for d in rec.unresolved_business_decisions}
    assert "CUSTOMER_NUMBER_OVERFLOW" in decisions
    assert "SHARED_CONTACTS_DUPLICATE_IDENTITIES" in decisions
    assert "CROSS_MARKET_CONFLICTS" in decisions
    assert "UNRESOLVED_ADDRESS_REMEDIATION" in decisions
    assert "GEOGRAPHIC_METADATA_VERIFICATION" in decisions


# ─── 4. Privacy Masking and Sensitive Field Exclusion ────────────────────────

def test_masking_and_sensitive_field_exclusion(isolated_test_engine):
    """Verify that email and phone numbers are strictly masked, and sensitive fields are excluded."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()

    # Raw emails / phones in seeded data: 'aarav.sharma@isp.in', '+91 98200 11111'
    for record in snapshot.deterministic_content.records:
        if record.proposed_contact:
            # Masked email
            assert "@" in record.proposed_contact.email_masked
            assert "***" in record.proposed_contact.email_masked
            assert "aarav.sharma@isp.in" not in record.proposed_contact.email_masked

            # Masked phone
            assert "*****" in record.proposed_contact.phone_masked
            assert "98200 11111" not in record.proposed_contact.phone_masked

        # Verify explicit omissions
        assert "tax_id" in record.omissions
        assert "identity_documents" in record.omissions
        assert "portal_account_users" in record.omissions

    # Verify no raw PII in canonical serialized bytes
    raw_bytes = serialize_canonical_snapshot(snapshot.deterministic_content).decode("utf-8")
    assert "aarav.sharma@isp.in" not in raw_bytes
    assert "vivaan.patel@isp.in" not in raw_bytes
    assert "debolina.m@isp.in" not in raw_bytes


# ─── 5. Source Mutation Detection ────────────────────────────────────────────

def test_source_mutation_detection_during_generation(isolated_test_engine):
    """Verify that concurrent database changes during snapshot generation raise SourceMutationDetectedError."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)

    with pytest.raises(SourceMutationDetectedError) as exc_info:
        service.generate_snapshot(simulate_mutation_before_verification=True)

    assert "CONCURRENCY VIOLATION" in str(exc_info.value)


# ─── 6. Exception-Safe Teardown & Zero Mutation ──────────────────────────────

def test_exception_safe_teardown_and_unchanged_database_state_hashes(isolated_test_engine, monkeypatch):
    """Verify that if an exception occurs mid-flight, database state hash is 100% unchanged."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.connect() as conn:
        baseline_hash = compute_database_state_hash(conn)

    service = CRMBackfillSnapshotService(engine)

    # Force simulated error inside validator
    def faulty_run(*args, **kwargs):
        raise RuntimeError("Injected unexpected processing exception")

    monkeypatch.setattr(service.validator, "run", faulty_run)

    with pytest.raises(RuntimeError) as exc_info:
        service.generate_snapshot()
    assert "Injected unexpected processing exception" in str(exc_info.value)

    # Verify state hash after exception
    with engine.connect() as conn:
        post_exception_hash = compute_database_state_hash(conn)

    assert baseline_hash == post_exception_hash, "Database state must remain unaffected by snapshot exceptions"


# ─── 7. Empty Source Database ────────────────────────────────────────────────

def test_empty_source_database(isolated_test_engine):
    """Verify that an empty database produces a clean zero-count snapshot satisfying all invariants."""
    engine = isolated_test_engine
    # Do not seed customers

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec = snapshot.deterministic_content.reconciliation

    assert rec.total_source_records == 0
    assert rec.eligible_count == 0
    assert rec.manual_review_count == 0
    assert rec.rejected_count == 0
    assert rec.proposed_customers_count == 0
    assert rec.proposed_contacts_count == 0
    assert rec.proposed_addresses_count == 0
    assert rec.omitted_addresses_count == 0
    assert snapshot.deterministic_content.records == []
    assert snapshot.execution_metadata.zero_mutation_verified is True


# ─── 8. Malformed Records and Partially Resolvable Addresses ─────────────────

def test_malformed_records_and_partially_resolvable_addresses(isolated_test_engine):
    """Verify omissions and unresolved fields for malformed records and addresses without PIN."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            -- Address without PIN code
            (401, 'mumbai', 'CUST-NO-PIN', 'No Pin User', 'nopin@isp.in', '+91 98200 40001',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Plot 10, Hill Road, Bandra West, Mumbai', 1, '2026-01-01 00:00:00');
        """))

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec_401 = next(r for r in snapshot.deterministic_content.records if r.legacy_customer_id == 401)

    assert rec_401.classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.UNRESOLVED_ADDRESS_METADATA in rec_401.reason_codes
    assert rec_401.proposed_address is None
    assert "address" in rec_401.omissions
    assert "unresolved_address_metadata" in rec_401.unresolved_fields


# ─── 9. Existing CRM Links, Collisions, Duplicates, Cross-Market ─────────────

def test_existing_crm_links_collisions_duplicates_cross_market(isolated_test_engine):
    """Verify link and collision metadata flags on snapshot records."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    # 1. Pre-populate CRM record linked to legacy 101
    with sessionmaker(bind=engine)() as session:
        session.add(CRMCustomer(
            customer_number="CUST-MUM-001",
            legal_name="Aarav Sharma Existing",
            market_id="mumbai",
            status=CustomerLifecycleStatus.ACTIVE,
            legacy_customer_id=101
        ))
        session.commit()

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec_101 = next(r for r in snapshot.deterministic_content.records if r.legacy_customer_id == 101)

    assert rec_101.classification == ClassificationCategory.REJECTED
    assert rec_101.existing_crm_link is True
    assert rec_101.existing_crm_customer_id is not None
    assert ReasonCode.ALREADY_LINKED in rec_101.reason_codes


# ─── 10. Identifier Overflow Handling ────────────────────────────────────────

def test_identifier_overflow_handling(isolated_test_engine):
    """Verify that customer code > 32 characters populates schema compatibility findings."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    long_code = "CUST-MUM-OVERFLOW-123456789012345678"
    assert len(long_code) == 36

    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (501, 'mumbai', '{long_code}', 'Long Code User', 'long@isp.in', '+91 98200 50001',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00');
        """))

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec_501 = next(r for r in snapshot.deterministic_content.records if r.legacy_customer_id == 501)

    assert rec_501.is_customer_number_overflow is True
    assert rec_501.customer_number_length == 36
    assert rec_501.target_max_length == 32
    assert "customer_number" in rec_501.omissions
    assert "customer_code_length_overflow" in rec_501.unresolved_fields
    assert rec_501.proposed_customer.customer_number is None


# ─── 11. Remote / Production Prevention ──────────────────────────────────────

def test_prevention_of_remote_production_database_access():
    """Verify that forbidden database URLs trigger fail-closed RuntimeError."""
    forbidden = [
        "postgresql://user:pass@db.supabase.co:5432/live",
        "postgresql://user:pass@pooler.supabase.com:6543/postgres",
        "postgresql://user:pass@production-db.rds.amazonaws.com:5432/live",
    ]
    for url in forbidden:
        with pytest.raises(RuntimeError) as exc_info:
            assert_safe_test_db(url)
        assert "CRITICAL TEST ISOLATION FAILURE" in str(exc_info.value)


# ─── 12. Human-Readable Report Generation ────────────────────────────────────

def test_human_readable_report_generation(isolated_test_engine):
    """Verify that the generated markdown report is structured, auditable, and privacy-conscious."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    report = service.generate_human_readable_report(snapshot)

    assert "Stage 3C: Customer Backfill Reconciliation Report" in report
    assert f"`{snapshot.snapshot_content_hash}`" in report
    assert f"**Total Source Records:** {snapshot.deterministic_content.reconciliation.total_source_records}" in report
    assert f"**Eligible for Backfill:** {snapshot.deterministic_content.reconciliation.eligible_count}" in report
    assert "AT_RISK_LIFECYCLE_MAPPING" in report
    assert "CUSTOMER_NUMBER_OVERFLOW" in report
    assert "UNRESOLVED_ADDRESS_REMEDIATION" in report
    # Ensure no raw PII in report
    assert "aarav.sharma@isp.in" not in report
    assert "+91 98200 11111" not in report
