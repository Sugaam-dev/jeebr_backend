"""test_stage3b_backfill_validator.py

Phase 1 Stage 3B: Read-Only Customer Backfill Dry-Run Validator Test Suite.

Verifies:
1. Zero writes across all operational and CRM tables.
2. Read-only enforcement against attempted writes (PRAGMA query_only).
3. Repeatable deterministic output for an unchanged source snapshot.
4. Source-field changes altering the source snapshot hash.
5. All approved and unapproved status/segment mappings.
6. Customer codes exceeding 32 characters.
7. Shared contacts, duplicate identities, and cross-market conflicts.
8. Missing or unverifiable address metadata (no fabrication).
9. Already-linked records and CRM customer-number collisions.
10. Reconciliation counts across markets (each record counted exactly once).
11. Database state unchanged after success, exceptions, and teardown.
12. No remote or production database access (strict fail-closed isolation).
"""

import os
import uuid
import pytest
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError

from app.models import Base, Customer, Node, User
from app.models_crm import (
    CRMCustomer, CRMContact, CRMAddress, CRMDocument, CRMAccountUser, CRMActivity,
    AccountType, CustomerLifecycleStatus, ServiceabilityStatus
)
from app.services.crm_backfill_validator import (
    CRMBackfillValidator, ReasonCode, ClassificationCategory,
    assert_safe_test_db, compute_database_state_hash,
    mask_email, mask_phone
)

TEST_DB_PATH = "test_stage3b_validator.db"
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
    
    # Create all tables (operational + CRM)
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


# ─── 1. Zero Writes and Read-Only Enforcement Tests ──────────────────────────

def test_zero_writes_across_operational_and_crm_tables(isolated_test_engine):
    """Verify that running the validator performs zero writes and leaves state hash identical."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    # Initial state hash
    with engine.connect() as conn:
        initial_hash = compute_database_state_hash(conn)

    validator = CRMBackfillValidator(engine)
    result = validator.run()

    assert result.execution_metadata.zero_mutation_verified is True
    assert result.execution_metadata.pre_state_hash == initial_hash
    assert result.execution_metadata.post_state_hash == initial_hash

    # Re-verify independently with a fresh connection
    with engine.connect() as conn:
        final_hash = compute_database_state_hash(conn)
        # Check table counts explicitly
        crm_count = conn.execute(text("SELECT count(*) FROM crm_customers")).scalar()
        contact_count = conn.execute(text("SELECT count(*) FROM crm_contacts")).scalar()
        addr_count = conn.execute(text("SELECT count(*) FROM crm_addresses")).scalar()
        cust_count = conn.execute(text("SELECT count(*) FROM customers")).scalar()

    assert initial_hash == final_hash
    assert crm_count == 0, "No CRM customers should be written"
    assert contact_count == 0, "No CRM contacts should be written"
    assert addr_count == 0, "No CRM addresses should be written"
    assert cust_count == 4, "Customer table count must remain unmodified"


def test_read_only_enforcement_against_attempted_writes(isolated_test_engine):
    """Verify that database connection under query_only mode rejects any write operation."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.connect() as conn:
        conn.execute(text("PRAGMA query_only = ON;"))
        # Attempt to insert into crm_customers
        with pytest.raises(OperationalError) as exc_info:
            conn.execute(text("""
                INSERT INTO crm_customers (id, customer_number, legal_name, market_id)
                VALUES ('00000000-0000-0000-0000-000000000001', 'ILLEGAL-001', 'Illegal Corp', 'mumbai')
            """))
        assert "attempt to write a readonly database" in str(exc_info.value).lower()


# ─── 2. Determinism and Source Snapshot Hash Tests ───────────────────────────

def test_repeatable_deterministic_output_for_unchanged_snapshot(isolated_test_engine):
    """Verify repeatable deterministic output on unchanged source snapshot."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    validator = CRMBackfillValidator(engine)
    run1 = validator.run()
    run2 = validator.run()

    # Deterministic output must be identical byte-for-byte in structure and hash
    assert run1.deterministic_output.source_snapshot_hash == run2.deterministic_output.source_snapshot_hash
    assert run1.deterministic_output.model_dump() == run2.deterministic_output.model_dump()
    assert run1.deterministic_output.total_records == 4
    assert run1.deterministic_output.classification_counts[ClassificationCategory.ELIGIBLE] == 4


def test_source_field_changes_alter_source_snapshot_hash(isolated_test_engine):
    """Verify that altering any single relevant source field alters the snapshot hash."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    validator = CRMBackfillValidator(engine)
    baseline_run = validator.run()
    baseline_hash = baseline_run.deterministic_output.source_snapshot_hash

    # Mutate one source customer's email
    with engine.begin() as conn:
        conn.execute(text("UPDATE customers SET email = 'aarav.modified@isp.in' WHERE id = 101"))

    mutated_run = validator.run()
    mutated_hash = mutated_run.deterministic_output.source_snapshot_hash

    assert baseline_hash != mutated_hash, "Mutating email must change the source snapshot hash"

    # Mutate another source field (service_latitude)
    with engine.begin() as conn:
        conn.execute(text("UPDATE customers SET service_latitude = 19.0601 WHERE id = 101"))

    mutated_run2 = validator.run()
    assert mutated_run2.deterministic_output.source_snapshot_hash != mutated_hash


# ─── 3. Matching Matrix: Approved and Unapproved Status/Segments ─────────────

def test_approved_and_unapproved_status_mappings(isolated_test_engine):
    """Verify all approved and unapproved status mappings."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    # Insert customers with different statuses
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (301, 'mumbai', 'CUST-STAT-01', 'Test Active', 'test1@isp.in', '+91 98200 30001',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (302, 'mumbai', 'CUST-STAT-02', 'Test Suspended', 'test2@isp.in', '+91 98200 30002',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Suspended', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (303, 'mumbai', 'CUST-STAT-03', 'Test Churned', 'test3@isp.in', '+91 98200 30003',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Churned', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (304, 'mumbai', 'CUST-STAT-04', 'Test Invalid Status', 'test4@isp.in', '+91 98200 30004',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'UNKNOWN_STATUS_XYZ', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00');
        """))

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    records_by_id = {r.legacy_customer_id: r for r in res.deterministic_output.records}

    # 301 Active -> CustomerLifecycleStatus.ACTIVE
    assert records_by_id[301].classification == ClassificationCategory.ELIGIBLE
    assert records_by_id[301].proposed_customer.status == CustomerLifecycleStatus.ACTIVE

    # 302 Suspended -> CustomerLifecycleStatus.SUSPENDED
    assert records_by_id[302].classification == ClassificationCategory.ELIGIBLE
    assert records_by_id[302].proposed_customer.status == CustomerLifecycleStatus.SUSPENDED

    # 303 Churned -> CustomerLifecycleStatus.TERMINATED
    assert records_by_id[303].classification == ClassificationCategory.ELIGIBLE
    assert records_by_id[303].proposed_customer.status == CustomerLifecycleStatus.TERMINATED

    # 304 Unknown status -> REJECTED with ReasonCode.UNMAPPED_STATUS
    assert records_by_id[304].classification == ClassificationCategory.REJECTED
    assert ReasonCode.UNMAPPED_STATUS in records_by_id[304].reason_codes


def test_approved_and_unapproved_segment_mappings(isolated_test_engine):
    """Verify all approved segment and customer_type mappings to AccountType."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (401, 'mumbai', 'CUST-SEG-01', 'Corp Client', 'corp@isp.in', '+91 98200 40001',
             'Bandra West', 'Postpaid - Enterprise ILL', 'Postpaid', 'ILL', 15000.0, 15000.0,
             15000.0, 15000.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (402, 'mumbai', 'CUST-SEG-02', 'SME Business', 'sme@isp.in', '+91 98200 40002',
             'Bandra West', 'SME Office Fiber Pro', 'Postpaid', 'SME', 3500.0, 3500.0,
             3500.0, 3500.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (403, 'mumbai', 'CUST-SEG-03', 'Home Family', 'res@isp.in', '+91 98200 40003',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (404, 'mumbai', 'CUST-SEG-04', 'Unknown Seg', 'unk@isp.in', '+91 98200 40004',
             'Bandra West', 'CustomGovtSatellite', 'Special', 'Govt', 50000.0, 50000.0,
             50000.0, 50000.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00');
        """))

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    records_by_id = {r.legacy_customer_id: r for r in res.deterministic_output.records}

    assert records_by_id[401].proposed_customer.account_type == AccountType.ENTERPRISE
    assert records_by_id[402].proposed_customer.account_type == AccountType.SME
    assert records_by_id[403].proposed_customer.account_type == AccountType.RESIDENTIAL

    assert records_by_id[404].classification == ClassificationCategory.REJECTED
    assert ReasonCode.UNMAPPED_SEGMENT in records_by_id[404].reason_codes


# ─── 4. Identifier Length Overflow Tests (> 32 characters) ───────────────────

def test_customer_code_exceeding_32_characters(isolated_test_engine):
    """Verify that customer codes > 32 characters trigger IDENTIFIER_OVERFLOW and manual review."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    # Insert customer with customer_code length 36 (exceeds String(32))
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

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    rec = next(r for r in res.deterministic_output.records if r.legacy_customer_id == 501)

    assert rec.classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.IDENTIFIER_OVERFLOW in rec.reason_codes
    # Proposed customer number must be None to prevent fatal truncation / DB error
    assert rec.proposed_customer.customer_number is None


# ─── 5. Shared Contacts, Duplicate Identities, Cross-Market Conflicts ────────

def test_shared_contacts_duplicate_identities_and_cross_market_conflicts(isolated_test_engine):
    """Verify detection of shared contacts, duplicate identities, and cross-market conflicts."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.begin() as conn:
        # A & B in Mumbai share the same email
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (601, 'mumbai', 'CUST-DUP-01', 'User Alpha', 'shared.mumbai@isp.in', '+91 98200 60001',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (602, 'mumbai', 'CUST-DUP-02', 'User Beta', 'shared.mumbai@isp.in', '+91 98200 60002',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00');
        """))

        # C in Mumbai and D in Kolkata share the same phone (Cross-market conflict!)
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            (603, 'mumbai', 'CUST-XMK-01', 'Cross Mum', 'cross.mum@isp.in', '+91 98000 99999',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00'),
            (604, 'kolkata', 'CUST-XMK-02', 'Cross Kol', 'cross.kol@isp.in', '+91 98000 99999',
             'Salt Lake Sector V', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Sector V, Kolkata 700091', 2, '2026-01-01 00:00:00');
        """))

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    records_by_id = {r.legacy_customer_id: r for r in res.deterministic_output.records}

    # 601 & 602: Same market duplicates
    assert records_by_id[601].classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.DUPLICATE_IDENTITY in records_by_id[601].reason_codes
    assert ReasonCode.SHARED_CONTACT in records_by_id[601].reason_codes

    assert records_by_id[602].classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.DUPLICATE_IDENTITY in records_by_id[602].reason_codes
    assert ReasonCode.SHARED_CONTACT in records_by_id[602].reason_codes

    # 603 & 604: Cross-market conflicts
    assert records_by_id[603].classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.CROSS_MARKET_CONFLICT in records_by_id[603].reason_codes

    assert records_by_id[604].classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.CROSS_MARKET_CONFLICT in records_by_id[604].reason_codes


# ─── 6. Missing or Unverifiable Address Metadata (No Fabrication) ────────────

def test_missing_or_unverifiable_address_metadata(isolated_test_engine):
    """Verify that missing address or unresolvable postal code triggers UNRESOLVED_ADDRESS_METADATA."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            -- Missing address completely (NULL)
            (701, 'mumbai', 'CUST-ADDR-01', 'No Address', 'noaddr@isp.in', '+91 98200 70001',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', NULL, 1, '2026-01-01 00:00:00'),
            -- Address present but lacks valid 6-digit PIN code
            (702, 'mumbai', 'CUST-ADDR-02', 'No Pin Address', 'nopin@isp.in', '+91 98200 70002',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Plot 44, Somewhere in Mumbai with no pin', 1, '2026-01-01 00:00:00');
        """))

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    records_by_id = {r.legacy_customer_id: r for r in res.deterministic_output.records}

    # 701: Address is null -> UNRESOLVED_ADDRESS_METADATA, proposed_address is None
    assert records_by_id[701].classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.UNRESOLVED_ADDRESS_METADATA in records_by_id[701].reason_codes
    assert records_by_id[701].proposed_address is None

    # 702: Missing postal code -> UNRESOLVED_ADDRESS_METADATA, proposed_address is None
    assert records_by_id[702].classification == ClassificationCategory.MANUAL_REVIEW
    assert ReasonCode.UNRESOLVED_ADDRESS_METADATA in records_by_id[702].reason_codes
    assert records_by_id[702].proposed_address is None


def test_serviceability_and_contact_verification_flags(isolated_test_engine):
    """Verify that contact is_verified is strictly False and serviceability_status is PENDING_SURVEY."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    rec = res.deterministic_output.records[0]

    assert rec.proposed_contact.is_verified is False
    assert rec.proposed_address.serviceability_status == ServiceabilityStatus.PENDING_SURVEY


# ─── 7. Already-Linked Records and Customer-Number Collisions ────────────────

def test_already_linked_records_and_customer_number_collisions(isolated_test_engine):
    """Verify that already-linked records and customer number collisions trigger REJECTED."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    # Pre-populate CRM table:
    # 1. crm_cust1 links legacy_customer_id = 101
    # 2. crm_cust2 has customer_number = 'CUST-KOL-001' but legacy_customer_id = NULL (native prospect collision)
    with sessionmaker(bind=engine)() as session:
        session.add(CRMCustomer(
            customer_number='CUST-MUM-001',
            legal_name='Aarav Sharma Existing',
            market_id='mumbai',
            status=CustomerLifecycleStatus.ACTIVE,
            legacy_customer_id=101
        ))
        session.add(CRMCustomer(
            customer_number='CUST-KOL-001',
            legal_name='Native Prospect Collision',
            market_id='kolkata',
            status=CustomerLifecycleStatus.PROSPECT,
            legacy_customer_id=None
        ))
        session.commit()

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    records_by_id = {r.legacy_customer_id: r for r in res.deterministic_output.records}

    # 101: Already linked in CRM
    assert records_by_id[101].classification == ClassificationCategory.REJECTED
    assert ReasonCode.ALREADY_LINKED in records_by_id[101].reason_codes

    # 201: Customer number collides with existing native CRM customer
    assert records_by_id[201].classification == ClassificationCategory.REJECTED
    assert ReasonCode.CUSTOMER_NUMBER_COLLISION in records_by_id[201].reason_codes


# ─── 8. Unsupported Markets & Malformed Records ──────────────────────────────

def test_unsupported_markets_and_malformed_records(isolated_test_engine):
    """Verify that unsupported markets and malformed records trigger REJECTED."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO customers (
                id, market_id, customer_code, name, email, phone, locality,
                segment, customer_type, plan_name, plan_price, revenue_30d,
                actual_arpu, arpu, status, service_address, node_id, signup_date
            ) VALUES 
            -- Unsupported market 'delhi'
            (801, 'delhi', 'CUST-DEL-01', 'Delhi User', 'delhi@isp.in', '+91 98111 80001',
             'Connaught Place', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Connaught Place, Delhi 110001', NULL, '2026-01-01 00:00:00'),
            -- Malformed record (empty customer_code and missing name)
            (802, 'mumbai', '', '   ', 'blank@isp.in', '+91 98200 80002',
             'Bandra West', 'Prepaid - Daily Unlimited', 'Prepaid', 'Unlimited', 299.0, 299.0,
             299.0, 299.0, 'Active', 'Hill Road, Mumbai 400050', 1, '2026-01-01 00:00:00');
        """))

    validator = CRMBackfillValidator(engine)
    res = validator.run()
    records_by_id = {r.legacy_customer_id: r for r in res.deterministic_output.records}

    assert records_by_id[801].classification == ClassificationCategory.REJECTED
    assert ReasonCode.UNSUPPORTED_MARKET in records_by_id[801].reason_codes

    assert records_by_id[802].classification == ClassificationCategory.REJECTED
    assert ReasonCode.MALFORMED_RECORD in records_by_id[802].reason_codes


# ─── 9. Market Reconciliation Counts ─────────────────────────────────────────

def test_reconciliation_counts_across_markets(isolated_test_engine):
    """Verify that every single source customer is accounted for exactly once across markets."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    validator = CRMBackfillValidator(engine)
    res = validator.run()

    det = res.deterministic_output
    total = det.total_records
    assert total == 4

    # Grand total reconciliation
    eligible = det.classification_counts[ClassificationCategory.ELIGIBLE]
    manual = det.classification_counts[ClassificationCategory.MANUAL_REVIEW]
    rejected = det.classification_counts[ClassificationCategory.REJECTED]
    assert total == (eligible + manual + rejected)

    # Market subtotals reconciliation
    market_sum = sum(m["total"] for m in det.market_counts.values())
    assert total == market_sum

    for m_id, m_counts in det.market_counts.items():
        assert m_counts["total"] == (
            m_counts[ClassificationCategory.ELIGIBLE] +
            m_counts[ClassificationCategory.MANUAL_REVIEW] +
            m_counts[ClassificationCategory.REJECTED]
        )


# ─── 10. Database State Unchanged After Exceptions & Teardown ────────────────

def test_database_state_unchanged_after_exceptions(isolated_test_engine, monkeypatch):
    """Verify database state remains unaltered even if an exception occurs during dry-run."""
    engine = isolated_test_engine
    seed_standard_fixtures(engine)

    with engine.connect() as conn:
        baseline_hash = compute_database_state_hash(conn)

    validator = CRMBackfillValidator(engine)

    # Simulate an error during execution
    def faulty_snapshot_hash(*args, **kwargs):
        raise RuntimeError("Simulated transient processing fault")

    monkeypatch.setattr(
        "app.services.crm_backfill_validator.compute_source_snapshot_hash",
        faulty_snapshot_hash
    )

    with pytest.raises(RuntimeError) as exc_info:
        validator.run()
    assert "Simulated transient processing fault" in str(exc_info.value)

    # Verify database state after exception
    with engine.connect() as conn:
        post_exception_hash = compute_database_state_hash(conn)

    assert baseline_hash == post_exception_hash, "Database state must be unaffected by dry-run exceptions"


# ─── 11. Strict Isolation: Remote / Production Rejection ─────────────────────

def test_no_remote_or_production_database_access():
    """Verify that forbidden database URLs trigger fail-closed RuntimeError."""
    forbidden_urls = [
        "postgresql://user:pass@db.supabase.co:5432/prod",
        "postgresql://user:pass@pooler.supabase.com:6543/postgres",
        "postgresql://user:pass@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
        "postgresql://user:pass@production-jeebr.rds.amazonaws.com:5432/live",
    ]

    for f_url in forbidden_urls:
        with pytest.raises(RuntimeError) as exc_info:
            assert_safe_test_db(f_url)
        assert "CRITICAL TEST ISOLATION FAILURE" in str(exc_info.value)


# ─── 12. Privacy Masking Verification ────────────────────────────────────────

def test_privacy_masking_enforcement(isolated_test_engine):
    """Verify masking logic for email and phone numbers in output reports."""
    assert mask_email("aarav.sharma@isp.in") == "a***a@isp.in"
    assert mask_email("a@b.com") == "a***@b.com"
    assert mask_email("ab@c.com") == "a***b@c.com"
    assert mask_email(None) is None

    assert mask_phone("+91 98200 11111") == "+91 9*****111"
    assert mask_phone("9820011111") == "98*****111"
    assert mask_phone(None) is None
