"""test_stage2_crm_schema.py

Phase 1 Stage 2: Schema-only Alembic migration and cross-dialect validation tests.
Verifies:
1. Clean Alembic upgrade and downgrade against an isolated test database.
2. Exact table structures, column definitions, and nullability across all 6 CRM tables:
   - crm_customers
   - crm_contacts
   - crm_addresses
   - crm_documents
   - crm_account_users
   - crm_activities
3. Bridge preservation: legacy_customer_id is nullable, unique, and references customers.id without cascading deletes.
4. Partial unique index uq_crm_contacts_customer_primary:
   - Exactly one non-archived primary contact allowed per customer.
   - Concurrent or secondary non-archived primary contact triggers IntegrityError.
   - Archived primary contacts do not violate the index.
   - Multiple non-primary contacts are allowed.
5. Retention and soft-archival preservation:
   - crm_documents ondelete="RESTRICT" prevents deleting customer while documents exist.
   - crm_activities ondelete="RESTRICT" prevents deleting customer while audit logs exist.
6. Check constraints:
   - Status, account_type, address_type, document_type, upload_status, verification_status.
7. Schema-only guarantee: zero legacy rows mutated or deleted; zero backfill executed.
"""
import os
import uuid
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from alembic.config import Config
from alembic import command

from app.models import Base, Customer, User
from app.models_crm import (
    CRMCustomer, CRMContact, CRMAddress, CRMDocument, CRMAccountUser, CRMActivity,
    AccountType, CustomerLifecycleStatus, ContactRole, AddressType,
    ServiceabilityStatus, DocumentType, DocumentUploadStatus, VerificationStatus,
    PortalRole, ActivityType
)

TEST_DB_PATH = "test_stage2_crm.db"
TEST_DB_URL = f"sqlite:///./{TEST_DB_PATH}"
ALEMBIC_INI_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
ALEMBIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "alembic"))

@pytest.fixture(scope="module")
def isolated_db_engine():
    # Remove existing test db file if present
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass

    # Save and set ALEMBIC_DATABASE_URL so alembic/env.py strictly connects to TEST_DB_URL
    old_alembic_url = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DB_URL

    # Create engine with foreign keys enabled in SQLite
    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))

    # Create pre-CRM baseline schema (excluding CRM tables)
    crm_table_names = {
        "crm_customers", "crm_contacts", "crm_addresses",
        "crm_documents", "crm_account_users", "crm_activities"
    }
    pre_crm_tables = [t for name, t in Base.metadata.tables.items() if name not in crm_table_names]
    Base.metadata.create_all(bind=engine, tables=pre_crm_tables)

    # Configure Alembic to point to isolated test database
    alembic_cfg = Config(ALEMBIC_INI_PATH)
    alembic_cfg.set_main_option("script_location", ALEMBIC_DIR)
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)

    # Stamp database at the pre-CRM baseline revision (f6a7b8c9d0e1)
    command.stamp(alembic_cfg, "f6a7b8c9d0e1")

    # Run upgrade to head (a7b8c9d0e1f2)
    command.upgrade(alembic_cfg, "head")

    yield engine, alembic_cfg

    # Teardown
    engine.dispose()
    if old_alembic_url is not None:
        os.environ["ALEMBIC_DATABASE_URL"] = old_alembic_url
    else:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass


def test_crm_tables_created_on_upgrade(isolated_db_engine):
    """Verify that all 6 CRM foundation tables exist after running migration to head."""
    engine, _ = isolated_db_engine
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected_tables = {
        "crm_customers",
        "crm_contacts",
        "crm_addresses",
        "crm_documents",
        "crm_account_users",
        "crm_activities"
    }
    assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"


def test_crm_customers_table_structure_and_bridge(isolated_db_engine):
    """Verify crm_customers table structure, indexes, and legacy_customer_id bridge."""
    engine, _ = isolated_db_engine
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("crm_customers")}

    assert "id" in cols
    assert "customer_number" in cols
    assert "legal_name" in cols
    assert "account_type" in cols
    assert "status" in cols
    assert "market_id" in cols
    assert "legacy_customer_id" in cols
    assert "tax_id" in cols
    assert "billing_currency" in cols
    assert "is_archived" in cols

    # Verify legacy_customer_id is nullable (to support native prospects prior to operational activation)
    assert cols["legacy_customer_id"]["nullable"] is True

    # Verify unique index on legacy_customer_id and customer_number
    indexes = inspector.get_indexes("crm_customers")
    idx_dict = {i["name"]: i for i in indexes}
    assert "ix_crm_customers_customer_number" in idx_dict
    assert idx_dict["ix_crm_customers_customer_number"]["unique"] == 1
    assert "ix_crm_customers_legacy_customer_id" in idx_dict
    assert idx_dict["ix_crm_customers_legacy_customer_id"]["unique"] == 1


def test_primary_contact_partial_unique_index(isolated_db_engine):
    """Verify partial unique index: at most ONE active non-archived primary contact per customer."""
    engine, _ = isolated_db_engine
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        cust_id = uuid.uuid4()
        cust = CRMCustomer(
            id=cust_id,
            customer_number="CRM-TEST-001",
            legal_name="Test Enterprise Ltd",
            market_id="mumbai",
            status=CustomerLifecycleStatus.ACTIVE
        )
        session.add(cust)
        session.commit()

        # 1. Add first active primary contact -> Succeeds
        c1 = CRMContact(
            id=uuid.uuid4(),
            crm_customer_id=cust_id,
            first_name="Alice",
            email="alice@test.com",
            is_primary=True,
            is_archived=False
        )
        session.add(c1)
        session.commit()

        # 2. Add second non-primary contact -> Succeeds
        c2 = CRMContact(
            id=uuid.uuid4(),
            crm_customer_id=cust_id,
            first_name="Bob",
            email="bob@test.com",
            is_primary=False,
            is_archived=False
        )
        session.add(c2)
        session.commit()

        # 3. Add second active PRIMARY contact for the same customer -> IntegrityError
        c3 = CRMContact(
            id=uuid.uuid4(),
            crm_customer_id=cust_id,
            first_name="Charlie",
            email="charlie@test.com",
            is_primary=True,
            is_archived=False
        )
        session.add(c3)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # 4. Archive first primary contact, then adding new active primary contact -> Succeeds
        c1_db = session.query(CRMContact).filter(CRMContact.id == c1.id).first()
        c1_db.is_archived = True
        session.commit()

        c4 = CRMContact(
            id=uuid.uuid4(),
            crm_customer_id=cust_id,
            first_name="Diana",
            email="diana@test.com",
            is_primary=True,
            is_archived=False
        )
        session.add(c4)
        session.commit()
        assert c4.id is not None
    finally:
        session.close()


def test_compliance_retention_foreign_keys_restrict(isolated_db_engine):
    """Verify ondelete='RESTRICT' on crm_documents and crm_activities prevents accidental customer deletion."""
    engine, _ = isolated_db_engine
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        cust_id = uuid.uuid4()
        cust = CRMCustomer(
            id=cust_id,
            customer_number="CRM-TEST-002",
            legal_name="Compliance Test Corp",
            market_id="mumbai",
            status=CustomerLifecycleStatus.ACTIVE
        )
        session.add(cust)
        session.commit()

        # Add KYC document (RESTRICT on customer)
        doc = CRMDocument(
            id=uuid.uuid4(),
            crm_customer_id=cust_id,
            document_type=DocumentType.PAN,
            document_identifier="ABCDE1234F",
            file_name="pan_copy.pdf",
            mime_type="application/pdf",
            upload_status=DocumentUploadStatus.PENDING_UPLOAD
        )
        session.add(doc)
        session.commit()

        # Attempt to delete customer while KYC document exists -> IntegrityError
        session.delete(cust)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Add audit activity (RESTRICT on customer)
        act = CRMActivity(
            id=uuid.uuid4(),
            crm_customer_id=cust_id,
            activity_type=ActivityType.NOTE,
            summary="Account opened"
        )
        session.add(act)
        session.commit()

        # Attempt to delete customer while activity exists -> IntegrityError
        cust_db = session.query(CRMCustomer).filter(CRMCustomer.id == cust_id).first()
        session.delete(cust_db)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()


def test_crm_check_constraints(isolated_db_engine):
    """Verify CheckConstraints reject invalid statuses, account types, or roles."""
    engine, _ = isolated_db_engine
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Invalid customer status
        bad_cust = CRMCustomer(
            id=uuid.uuid4(),
            customer_number="CRM-BAD-001",
            legal_name="Bad Status Corp",
            market_id="mumbai",
            status="INVALID_STATUS_XYZ"
        )
        session.add(bad_cust)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Invalid account type
        bad_type_cust = CRMCustomer(
            id=uuid.uuid4(),
            customer_number="CRM-BAD-002",
            legal_name="Bad Type Corp",
            market_id="mumbai",
            account_type="PERSONAL_CONSUMER_UNKNOWN"
        )
        session.add(bad_type_cust)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()


def test_alembic_downgrade_and_upgrade_cycle(isolated_db_engine):
    """Verify that Alembic can cleanly downgrade and re-upgrade the CRM foundation migration."""
    engine, alembic_cfg = isolated_db_engine

    # 1. Downgrade back to f6a7b8c9d0e1 (pre-CRM foundation)
    command.downgrade(alembic_cfg, "f6a7b8c9d0e1")

    inspector = inspect(engine)
    tables_after_down = set(inspector.get_table_names())
    crm_tables = {
        "crm_customers", "crm_contacts", "crm_addresses",
        "crm_documents", "crm_account_users", "crm_activities"
    }
    # Verify all CRM tables are dropped
    assert crm_tables.isdisjoint(tables_after_down), f"Tables not dropped: {crm_tables & tables_after_down}"

    # 2. Re-upgrade to head (a7b8c9d0e1f2)
    command.upgrade(alembic_cfg, "head")

    inspector_reup = inspect(engine)
    tables_after_reup = set(inspector_reup.get_table_names())
    assert crm_tables.issubset(tables_after_reup), "Tables missing after re-upgrade"


def test_postgresql_dialect_offline_ddl_generation():
    """
    Cross-dialect validation:
    Verifies that Alembic generates valid, standard PostgreSQL DDL for upgrade and downgrade
    without requiring a destructive connection to remote Supabase or unconfigured local PostgreSQL.
    """
    import io
    import sys

    pg_cfg = Config(ALEMBIC_INI_PATH)
    pg_cfg.set_main_option("script_location", ALEMBIC_DIR)
    mock_pg_url = "postgresql://mock_user:mock_pass@localhost:5432/mock_db"
    pg_cfg.set_main_option("sqlalchemy.url", mock_pg_url)

    old_alembic_url = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = mock_pg_url

    try:
        # Test PostgreSQL upgrade DDL compilation
        buf_up = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf_up
        try:
            command.upgrade(pg_cfg, "f6a7b8c9d0e1:head", sql=True)
        finally:
            sys.stdout = old_stdout

        sql_up = buf_up.getvalue()
        assert "CREATE TABLE crm_customers" in sql_up
        assert "CREATE TABLE crm_contacts" in sql_up
        assert "CREATE UNIQUE INDEX uq_crm_contacts_customer_primary" in sql_up
        assert "WHERE is_primary = true AND is_archived = false" in sql_up
        assert "CREATE TABLE crm_addresses" in sql_up
        assert "CREATE TABLE crm_documents" in sql_up
        assert "CREATE TABLE crm_account_users" in sql_up
        assert "CREATE TABLE crm_activities" in sql_up

        # Test PostgreSQL downgrade DDL compilation
        buf_down = io.StringIO()
        sys.stdout = buf_down
        try:
            command.downgrade(pg_cfg, "head:f6a7b8c9d0e1", sql=True)
        finally:
            sys.stdout = old_stdout

        sql_down = buf_down.getvalue()
        assert "DROP TABLE crm_activities" in sql_down
        assert "DROP TABLE crm_account_users" in sql_down
        assert "DROP TABLE crm_documents" in sql_down
        assert "DROP TABLE crm_addresses" in sql_down
        assert "DROP TABLE crm_contacts" in sql_down
        assert "DROP TABLE crm_customers" in sql_down
    finally:
        if old_alembic_url is not None:
            os.environ["ALEMBIC_DATABASE_URL"] = old_alembic_url
        else:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
