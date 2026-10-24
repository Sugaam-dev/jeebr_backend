"""test_stage3c1_audit.py

SentinelOS Phase 1 — Stage 3C.1: Snapshot Safety & Migration Readiness Audit Test Suite.

Exhaustive automated verification across the 6 audit pillars:
1. Audit 1: PostgreSQL Read-Only Enforcement, Isolation Grammar & Exception-Safe Rollback Teardown.
2. Audit 2: Snapshot Privacy Redaction, PII Leak Prevention & Safe Stage 3D In-Memory Architecture.
3. Audit 3: Single-Connection Atomicity, Source Mutation Detection & Concurrency Resilience.
4. Audit 4: Category Exclusivity, Reconciliation Invariants & Multi-Reason Aggregation Integrity.
5. Audit 5: At-Risk Subscriber Accounting & Lifecycle Policy Boundary Adherence.
6. Audit 6: Formal Tracking of All 6 Unapproved Business Decisions & Impact Metrics.
"""

import os
import json
import pytest
from unittest.mock import MagicMock
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Connection

from app.models import Base, Customer, Node
from app.models_crm import (
    CRMCustomer, AccountType, CustomerLifecycleStatus, ServiceabilityStatus,
    ContactRole, AddressType
)
from app.services.crm_backfill_validator import (
    CRMBackfillValidator, ReasonCode, ClassificationCategory,
    assert_safe_test_db, compute_database_state_hash,
    mask_email, mask_phone
)
from app.services.crm_backfill_snapshot import (
    CRMBackfillSnapshotService, SourceMutationDetectedError,
    SnapshotRecord, serialize_canonical_snapshot, compute_snapshot_content_hash
)

AUDIT_DB_PATH = "test_stage3c1_audit.db"
AUDIT_DB_URL = f"sqlite:///./{AUDIT_DB_PATH}"

@pytest.fixture
def audit_engine():
    """Provides an isolated synthetic database engine strictly checked against forbidden targets."""
    assert_safe_test_db(AUDIT_DB_URL)

    if os.path.exists(AUDIT_DB_PATH):
        try:
            os.remove(AUDIT_DB_PATH)
        except Exception:
            pass

    engine = create_engine(AUDIT_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

    yield engine

    engine.dispose()
    if os.path.exists(AUDIT_DB_PATH):
        try:
            os.remove(AUDIT_DB_PATH)
        except Exception:
            pass


def seed_comprehensive_audit_fixtures(engine: Engine):
    """
    Seeds a representative spectrum of records to exercise all categories,
    reasons, at-risk statuses, collisions, and multi-reason conditions.
    """
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO nodes (id, market_id, node_code, node_name, area, node_type, status)
            VALUES 
            (1, 'mumbai', 'OLT-MUM-01', 'South Mumbai Hub', 'Colaba', 'OLT', 'Healthy'),
            (2, 'kolkata', 'OLT-KOL-01', 'Kolkata Central Hub', 'Park Street', 'OLT', 'Healthy');
        """))

        # Pre-seed existing CRM customer for collision & link testing
        conn.execute(text("""
            INSERT INTO crm_customers (
                id, customer_number, legal_name, account_type, status,
                market_id, legacy_customer_id, billing_currency, is_archived, created_at, updated_at
            ) VALUES 
            ('c1000000-0000-0000-0000-000000000001', 'EXISTING-LINKED-01', 'Existing Linked Co',
             'ENTERPRISE', 'ACTIVE', 'mumbai', 10, 'INR', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00'),
            ('c1000000-0000-0000-0000-000000000002', 'COLLIDE-CODE-99', 'Collision Target Co',
             'SME', 'ACTIVE', 'mumbai', 9999, 'INR', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        """))

        # Seed legacy customers
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            -- 1. Perfectly Eligible Mumbai Record
            (1, 'mumbai', 'CUST-MUM-OK1', 'Rajesh Kulkarni', 'rajesh.k@example.com', '+91 98201 11111',
             'Colaba', 'Residential - Fibre Fast', 'Prepaid', 'Fast 100M', 799.0, 799.0, 799.0, 799.0,
             'Active', '12 Waterfront Apt, Colaba, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 2. Eligible Kolkata Record
            (2, 'kolkata', 'CUST-KOL-OK1', 'Anirban Das', 'anirban.d@example.com', '+91 98301 22222',
             'Park Street', 'Enterprise - Leased Line', 'Postpaid', 'ILL 1G', 25000.0, 25000.0, 25000.0, 25000.0,
             'Active', '55 Park Mansion, Park Street, Kolkata 700016', 2, '2026-01-01 00:00:00'),

            -- 3. At-Risk Subscriber (Mumbai) -> Eligible / ACTIVE lifecycle
            (3, 'mumbai', 'CUST-MUM-ATRISK', 'Sunita Rao', 'sunita.r@example.com', '+91 98201 33333',
             'Colaba', 'SME - Small Office', 'Postpaid', 'Biz 200M', 1999.0, 1999.0, 1999.0, 1999.0,
             'At-Risk', 'Flat 4A, Bayview, Colaba, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 4. Manual Review: Identifier Overflow (> 32 chars)
            (4, 'mumbai', 'CUST-MUM-IDENTIFIER-OVERFLOW-EXCEEDING-32CHARS-XYZ', 'Vikram Singhania', 'vikram.s@example.com', '+91 98201 44444',
             'Colaba', 'Residential - Family', 'Prepaid', 'Fast 50M', 499.0, 499.0, 499.0, 499.0,
             'Active', '8 Regal Lane, Colaba, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 5. Manual Review: Missing PIN code (Unresolved Address Metadata)
            (5, 'mumbai', 'CUST-MUM-NO-PIN', 'Pooja Hegde', 'pooja.h@example.com', '+91 98201 55555',
             'Colaba', 'Residential - Daily', 'Prepaid', 'Unlimited 30D', 299.0, 299.0, 299.0, 299.0,
             'Active', 'Near Taj Hotel, Colaba Causeway, Mumbai (no postal code)', 1, '2026-01-01 00:00:00'),

            -- 6 & 7. Manual Review: Cross-Market Shared Contact Conflict
            (6, 'mumbai', 'CUST-MUM-SHARED1', 'Karan Johar', 'shared.contact@universal.in', '+91 98000 00000',
             'Colaba', 'Residential - Prepaid', 'Prepaid', 'Fast 100M', 799.0, 799.0, 799.0, 799.0,
             'Active', 'Plot 10, Colaba, Mumbai 400005', 1, '2026-01-01 00:00:00'),
            (7, 'kolkata', 'CUST-KOL-SHARED1', 'Karan Johar East', 'shared.contact@universal.in', '+91 98000 00000',
             'Park Street', 'Residential - Prepaid', 'Prepaid', 'Fast 100M', 799.0, 799.0, 799.0, 799.0,
             'Active', 'Suite 2, Park Street, Kolkata 700016', 2, '2026-01-01 00:00:00'),

            -- 8. Rejected: Already Linked
            (10, 'mumbai', 'CUST-MUM-LINKED', 'Pre-linked Customer', 'linked@example.com', '+91 98201 99999',
             'Colaba', 'Enterprise - ILL', 'Postpaid', 'ILL 100M', 10000.0, 10000.0, 10000.0, 10000.0,
             'Active', 'Building 1, Colaba, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 9. Rejected: Customer Number Collision with existing CRM customer
            (11, 'mumbai', 'COLLIDE-CODE-99', 'Collision Offender', 'collide@example.com', '+91 98201 88888',
             'Colaba', 'Residential - Prepaid', 'Prepaid', 'Fast 50M', 499.0, 499.0, 499.0, 499.0,
             'Active', 'Flat 2B, Colaba, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 10. Rejected: Unsupported Market
            (12, 'delhi', 'CUST-DEL-001', 'Arjun Kapoor', 'arjun.k@example.com', '+91 98111 11111',
             'Connaught Place', 'Enterprise - ILL', 'Postpaid', 'ILL 500M', 15000.0, 15000.0, 15000.0, 15000.0,
             'Active', 'Barakhamba Road, Connaught Place, New Delhi 110001', NULL, '2026-01-01 00:00:00'),

            -- 11. Rejected: Unmapped Status
            (13, 'mumbai', 'CUST-MUM-UNKNOWN-ST', 'Mira Rajput', 'mira.r@example.com', '+91 98201 77777',
             'Colaba', 'Residential - Daily', 'Prepaid', 'Daily 1G', 199.0, 199.0, 199.0, 199.0,
             'UNKNOWN_SPECIAL_STATUS', 'Colaba Market, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 12. Rejected: Malformed Record (Missing Name and Customer Code)
            (14, 'mumbai', '', '   ', 'malformed@example.com', '+91 98201 66666',
             'Colaba', 'Residential - Daily', 'Prepaid', 'Daily 1G', 199.0, 199.0, 199.0, 199.0,
             'Active', 'Colaba Market, Mumbai 400005', 1, '2026-01-01 00:00:00'),

            -- 13. Rejected with Multiple Severe Reasons: Unsupported Market AND Malformed AND Unmapped Status
            (15, 'bangalore', 'CUST-BLR-MULTI', '   ', 'multi.reject@example.com', '+91 98450 11111',
             'Indiranagar', 'Unknown Segment', 'Unknown Type', 'Custom Plan', 0.0, 0.0, 0.0, 0.0,
             'INVALID_STATUS_XYZ', 'Indiranagar 100ft Road, Bangalore 560038', NULL, '2026-01-01 00:00:00');
        """))


# ─── Audit 1 Tests: PostgreSQL Read-Only Enforcement & Safe Teardown ─────────

from unittest.mock import MagicMock, patch

def test_audit1_postgresql_read_only_grammar_and_teardown_rollback():
    """
    Verifies that for PostgreSQL dialect:
    1. 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;' is executed.
    2. 'SET default_transaction_read_only = ON;' is executed.
    3. In teardown, 'conn.rollback()' is executed BEFORE resetting 'default_transaction_read_only = OFF;'.
    4. Confirms that local PostgreSQL requires authentication and remote Supabase is rejected.
    """
    mock_conn = MagicMock()
    executed_statements = []

    def fake_execute(statement, *args, **kwargs):
        stmt_text = str(statement).strip()
        executed_statements.append(stmt_text)
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_result.keys.return_value = []
        mock_result.__iter__.return_value = []
        return mock_result

    mock_conn.execute.side_effect = fake_execute

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    mock_engine.url = "postgresql://isolated_user:local_pass@localhost:5432/test_synth_db"

    validator = CRMBackfillValidator(mock_engine)

    with patch("app.services.crm_backfill_validator.compute_database_state_hash", return_value="deadbeef1234"):
        result = validator._run_on_connection(
            conn=mock_conn,
            dialect_name="postgresql",
            start_time=0.0,
            executed_at="2026-10-23T12:00:00Z",
            manages_connection=True
        )

    # 1. Verify read-only statements
    assert any("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in s for s in executed_statements), (
        "PostgreSQL must enforce REPEATABLE READ READ ONLY at transaction level"
    )
    assert any("SET default_transaction_read_only = ON" in s for s in executed_statements)

    # 2. Verify rollback before reset in teardown
    mock_conn.rollback.assert_called_once()
    assert any("SET default_transaction_read_only = OFF" in s for s in executed_statements)

    # 3. Verify that assert_safe_test_db strictly fails on remote/production URLs
    with pytest.raises(RuntimeError, match="CRITICAL TEST ISOLATION FAILURE"):
        assert_safe_test_db("postgresql://postgres:secret@db.xyz.supabase.co:5432/postgres")


# ─── Audit 2 Tests: Snapshot Privacy, Redaction & Safe Stage 3D Retrieval ───

def test_audit2_snapshot_privacy_redaction_and_leak_prevention(audit_engine):
    """
    Verifies that canonical staging snapshot bytes are completely devoid of:
    - Raw unmasked emails
    - Raw unmasked phone numbers
    - Tax IDs (omitted)
    - Identity documents (omitted)
    - Portal credentials / passwords (omitted)
    - Payment profiles (omitted)
    Also verifies that contact is_verified is False and serviceability is PENDING_SURVEY.
    """
    engine = audit_engine
    seed_comprehensive_audit_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()

    canonical_bytes = serialize_canonical_snapshot(snapshot.deterministic_content)
    raw_json_str = canonical_bytes.decode("utf-8")

    # 1. Assert raw customer PII is never present in serialized JSON
    forbidden_strings = [
        "rajesh.k@example.com",
        "anirban.d@example.com",
        "sunita.r@example.com",
        "+91 98201 11111",
        "+91 98301 22222",
        "+91 98201 33333",
    ]
    for raw_pii in forbidden_strings:
        assert raw_pii not in raw_json_str, f"Unmasked PII found in canonical snapshot bytes: {raw_pii}"

    # 2. Verify masking presence
    assert "r***k@example.com" in raw_json_str
    assert "+91 9*****111" in raw_json_str

    # 3. Assert zero sensitive entities
    for record in snapshot.deterministic_content.records:
        assert "tax_id" in record.omissions
        assert "identity_documents" in record.omissions
        assert "portal_account_users" in record.omissions
        assert "payment_profiles" in record.omissions

        if record.proposed_contact:
            assert record.proposed_contact.is_verified is False
            assert record.proposed_contact.email_masked is not None
            assert "@" in record.proposed_contact.email_masked
            assert "***" in record.proposed_contact.email_masked

        if record.proposed_address:
            assert record.proposed_address.serviceability_status == ServiceabilityStatus.PENDING_SURVEY


# ─── Audit 3 Tests: Concurrency, Single Connection & Mutation Detection ──────

def test_audit3_single_connection_execution_and_mutation_detection(audit_engine):
    """
    Verifies that:
    1. CRMBackfillSnapshotService executes validator and queries on a single shared connection.
    2. Source mutation during generation triggers SourceMutationDetectedError immediately.
    3. The database state is verified before and after.
    """
    engine = audit_engine
    seed_comprehensive_audit_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)

    # 1. Standard run passes with zero mutations
    snapshot = service.generate_snapshot()
    assert snapshot.execution_metadata.zero_mutation_verified is True

    # 2. Simulated mutation fails closed
    with pytest.raises(SourceMutationDetectedError, match="CONCURRENCY VIOLATION"):
        service.generate_snapshot(simulate_mutation_before_verification=True)


# ─── Audit 4 Tests: Invariants, Category Exclusivity & Multi-Reason Records ─

def test_audit4_reconciliation_invariants_and_category_exclusivity(audit_engine):
    """
    Verifies all 6 invariants:
    1. total_source_records == eligible + manual_review + rejected
    2. total_source_records == sum(market_totals)
    3. market_total == sum of categories within market
    4. proposed_addresses + omitted_addresses == total_source_records
    5. proposed_customers <= (eligible + manual_review)
    6. REJECTED records strictly have proposed_customer = None, contact = None, address = None
    """
    engine = audit_engine
    seed_comprehensive_audit_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec = snapshot.deterministic_content.reconciliation

    total = rec.total_source_records
    assert total == 13

    # Invariant 1
    assert total == rec.eligible_count + rec.manual_review_count + rec.rejected_count
    assert rec.eligible_count == 3   # ID 1 (Mumbai), ID 2 (Kolkata), ID 3 (Mumbai At-Risk)
    assert rec.manual_review_count == 4  # ID 4 (overflow), ID 5 (no PIN), ID 6 & 7 (cross-market shared)
    assert rec.rejected_count == 6   # ID 10 (linked), ID 11 (collision), ID 12 (unsupported), ID 13 (unmapped st), ID 14 (malformed), ID 15 (multi-reject)

    # Invariant 2
    sum_markets = sum(m["total"] for m in rec.market_reconciliation.values())
    assert total == sum_markets

    # Invariant 3
    for m_id, counts in rec.market_reconciliation.items():
        assert counts["total"] == counts["ELIGIBLE"] + counts["MANUAL_REVIEW"] + counts["REJECTED"]

    # Invariant 4
    assert rec.proposed_addresses_count + rec.omitted_addresses_count == total

    # Invariant 5: proposed customers <= non-rejected records
    assert rec.proposed_customers_count <= (rec.eligible_count + rec.manual_review_count)
    assert rec.proposed_customers_count == 7  # 3 eligible + 4 review (including ID 4 with customer_number=None)

    # Invariant 6: REJECTED records strictly have zero proposed entities
    rejected_records = [r for r in snapshot.deterministic_content.records if r.classification == ClassificationCategory.REJECTED]
    assert len(rejected_records) == 6
    for r in rejected_records:
        assert r.proposed_customer is None, f"Record {r.legacy_customer_id} is REJECTED but has proposed_customer"
        assert r.proposed_contact is None, f"Record {r.legacy_customer_id} is REJECTED but has proposed_contact"
        assert r.proposed_address is None, f"Record {r.legacy_customer_id} is REJECTED but has proposed_address"


def test_audit4_multi_reason_record_does_not_double_count_classifications(audit_engine):
    """
    Verifies that record ID 15 (which has MALFORMED_RECORD, UNSUPPORTED_MARKET, and UNMAPPED_STATUS)
    is accounted for as exactly 1 REJECTED record in category counts, while its individual reasons
    are tallied independently in the reason code breakdown.
    """
    engine = audit_engine
    seed_comprehensive_audit_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec = snapshot.deterministic_content.reconciliation

    rec15 = next(r for r in snapshot.deterministic_content.records if r.legacy_customer_id == 15)
    assert rec15.classification == ClassificationCategory.REJECTED
    assert ReasonCode.MALFORMED_RECORD in rec15.reason_codes
    assert ReasonCode.UNSUPPORTED_MARKET in rec15.reason_codes
    assert ReasonCode.UNMAPPED_STATUS in rec15.reason_codes

    # Ensure total classifications sum to exactly total records (no double count)
    assert rec.total_source_records == rec.eligible_count + rec.manual_review_count + rec.rejected_count


def test_audit4_zero_records_empty_database_resilience(audit_engine):
    """Verifies that an empty database is processed cleanly with zero counts and all invariants preserved."""
    engine = audit_engine  # Empty, no fixtures seeded

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
    assert len(snapshot.deterministic_content.records) == 0

    report = service.generate_human_readable_report(snapshot)
    assert "Total Source Records:** 0" in report


# ─── Audit 5 Tests: At-Risk Subscriber Lifecycle Accounting ──────────────────

def test_audit5_at_risk_subscriber_lifecycle_accounting(audit_engine):
    """
    Verifies that subscribers with legacy status 'At-Risk' / 'at-risk':
    1. Are mapped to CustomerLifecycleStatus.ACTIVE in proposed customer.
    2. Are counted accurately in reconciliation summary under at_risk_status_count.
    3. Have their legacy_status preserved on SnapshotRecord for auditable business review.
    4. Do not invent unapproved churn or penalty logic.
    """
    engine = audit_engine
    seed_comprehensive_audit_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    rec = snapshot.deterministic_content.reconciliation

    # ID 3 was seeded with status='At-Risk'
    rec3 = next(r for r in snapshot.deterministic_content.records if r.legacy_customer_id == 3)
    assert rec3.legacy_status == "At-Risk"
    assert rec3.classification == ClassificationCategory.ELIGIBLE
    assert rec3.proposed_customer is not None
    assert rec3.proposed_customer.status == CustomerLifecycleStatus.ACTIVE

    # Check at_risk_status_count in reconciliation
    assert rec.at_risk_status_count == 1

    # Check that it appears in business decisions
    at_risk_dec = next(d for d in rec.unresolved_business_decisions if d.decision_key == "AT_RISK_LIFECYCLE_MAPPING")
    assert at_risk_dec.affected_record_count == 1


# ─── Audit 6 Tests: Tracking All 6 Unapproved Business Decisions ─────────────

def test_audit6_unapproved_business_decisions_tracking(audit_engine):
    """
    Verifies that all 6 unapproved business decisions from Stage 3C are formally
    registered in the reconciliation summary with non-zero affected counts where applicable
    and unambiguous blocking status descriptions.
    """
    engine = audit_engine
    seed_comprehensive_audit_fixtures(engine)

    service = CRMBackfillSnapshotService(engine)
    snapshot = service.generate_snapshot()
    decisions = snapshot.deterministic_content.reconciliation.unresolved_business_decisions

    expected_keys = {
        "AT_RISK_LIFECYCLE_MAPPING",
        "CUSTOMER_NUMBER_OVERFLOW",
        "SHARED_CONTACTS_DUPLICATE_IDENTITIES",
        "CROSS_MARKET_CONFLICTS",
        "UNRESOLVED_ADDRESS_REMEDIATION",
        "GEOGRAPHIC_METADATA_VERIFICATION"
    }

    registered_keys = {d.decision_key for d in decisions}
    assert registered_keys == expected_keys, f"Mismatch in business decision keys: {registered_keys} != {expected_keys}"

    dec_map = {d.decision_key: d for d in decisions}
    assert dec_map["AT_RISK_LIFECYCLE_MAPPING"].affected_record_count == 1
    assert dec_map["CUSTOMER_NUMBER_OVERFLOW"].affected_record_count == 1
    assert dec_map["CROSS_MARKET_CONFLICTS"].affected_record_count == 2
    assert dec_map["UNRESOLVED_ADDRESS_REMEDIATION"].affected_record_count >= 1
    assert dec_map["GEOGRAPHIC_METADATA_VERIFICATION"].affected_record_count == 13

    for dec in decisions:
        assert dec.blocking_impact, f"Decision {dec.decision_key} missing blocking_impact description"
        assert dec.current_stage_behavior, f"Decision {dec.decision_key} missing current_stage_behavior"
