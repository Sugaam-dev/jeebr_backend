import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column, String, Text, Boolean, Integer, Float, ForeignKey,
    DateTime, Index, UniqueConstraint, CheckConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator, CHAR
from app.database import Base

# ─── Platform-Independent GUID Type ──────────────────────────────────────────

class GUID(TypeDecorator):
    """
    Platform-independent GUID type.
    Uses PostgreSQL's native UUID type where available, otherwise CHAR(36) in SQLite.
    """
    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PGUUID
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return str(value) if dialect.name != "postgresql" else value
        return str(uuid.UUID(str(value))) if dialect.name != "postgresql" else uuid.UUID(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


# ─── CRM Domain Constants & States ───────────────────────────────────────────

class AccountType:
    RESIDENTIAL = "RESIDENTIAL"
    ENTERPRISE = "ENTERPRISE"
    SME = "SME"
    ALL = (RESIDENTIAL, ENTERPRISE, SME)

class CustomerLifecycleStatus:
    PROSPECT = "PROSPECT"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    ALL = (PROSPECT, PENDING_VERIFICATION, ACTIVE, DORMANT, SUSPENDED, TERMINATED)

class ContactRole:
    PRIMARY = "PRIMARY"
    BILLING = "BILLING"
    TECHNICAL = "TECHNICAL"
    EMERGENCY = "EMERGENCY"
    ALL = (PRIMARY, BILLING, TECHNICAL, EMERGENCY)

class AddressType:
    INSTALLATION = "INSTALLATION"
    BILLING = "BILLING"
    REGISTERED = "REGISTERED"
    ALL = (INSTALLATION, BILLING, REGISTERED)

class ServiceabilityStatus:
    PENDING_SURVEY = "PENDING_SURVEY"
    FEASIBLE = "FEASIBLE"
    SERVICEABLE = "SERVICEABLE"
    UNSERVICEABLE = "UNSERVICEABLE"
    ALL = (PENDING_SURVEY, FEASIBLE, SERVICEABLE, UNSERVICEABLE)

class DocumentType:
    AADHAAR_MASKED = "AADHAAR_MASKED"
    PAN = "PAN"
    INCORPORATION_CERT = "INCORPORATION_CERT"
    LEASE_AGREEMENT = "LEASE_AGREEMENT"
    VOTER_ID = "VOTER_ID"
    PASSPORT = "PASSPORT"
    DRIVING_LICENSE = "DRIVING_LICENSE"
    ALL = (AADHAAR_MASKED, PAN, INCORPORATION_CERT, LEASE_AGREEMENT, VOTER_ID, PASSPORT, DRIVING_LICENSE)

class DocumentUploadStatus:
    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"
    ALL = (PENDING_UPLOAD, UPLOADED, FAILED, ARCHIVED)

class VerificationStatus:
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    ALL = (PENDING, VERIFIED, REJECTED, REVOKED)

class PortalRole:
    ACCOUNT_OWNER = "ACCOUNT_OWNER"
    ACCOUNT_ADMIN = "ACCOUNT_ADMIN"
    ACCOUNT_BILLING = "ACCOUNT_BILLING"
    ACCOUNT_VIEWER = "ACCOUNT_VIEWER"
    ALL = (ACCOUNT_OWNER, ACCOUNT_ADMIN, ACCOUNT_BILLING, ACCOUNT_VIEWER)

class ActivityType:
    NOTE = "NOTE"
    STATUS_CHANGE = "STATUS_CHANGE"
    CONTACT_ADDED = "CONTACT_ADDED"
    ADDRESS_UPDATED = "ADDRESS_UPDATED"
    KYC_VERIFIED = "KYC_VERIFIED"
    MIGRATION_BACKFILL = "MIGRATION_BACKFILL"
    SYSTEM = "SYSTEM"
    ALL = (NOTE, STATUS_CHANGE, CONTACT_ADDED, ADDRESS_UPDATED, KYC_VERIFIED, MIGRATION_BACKFILL, SYSTEM)


# ─── CRM ORM Models ──────────────────────────────────────────────────────────

class CRMCustomer(Base):
    __tablename__ = "crm_customers"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    customer_number = Column(String(32), unique=True, nullable=False, index=True)
    legal_name = Column(String(255), nullable=False, index=True)
    trade_name = Column(String(255), nullable=True)
    account_type = Column(String(32), nullable=False, default=AccountType.RESIDENTIAL)
    status = Column(String(32), nullable=False, default=CustomerLifecycleStatus.PROSPECT)
    market_id = Column(String(32), nullable=False, index=True)

    # 1:1 Bridge to legacy operational customer (nullable, unique, preserves legacy model)
    legacy_customer_id = Column(Integer, ForeignKey("customers.id", ondelete="SET NULL"), unique=True, nullable=True)

    # Encrypted / Masked Commercial Identifiers (Full Aadhaar strictly prohibited)
    tax_id = Column(String(64), nullable=True)  # GSTIN or Business PAN
    billing_currency = Column(String(3), nullable=False, default="INR")

    # Soft deletion & compliance preservation (No destructive cascade deletes)
    is_archived = Column(Boolean, nullable=False, default=False, index=True)
    archived_at = Column(DateTime, nullable=True)
    archived_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships with passive_deletes to honor DB retention policies
    legacy_customer = relationship("Customer", back_populates="crm_account", uselist=False)
    contacts = relationship("CRMContact", back_populates="customer", passive_deletes=True)
    addresses = relationship("CRMAddress", back_populates="customer", passive_deletes=True)
    documents = relationship("CRMDocument", back_populates="customer", passive_deletes=True)
    activities = relationship("CRMActivity", back_populates="customer", passive_deletes=True)
    portal_users = relationship("CRMAccountUser", back_populates="customer", passive_deletes=True)

    __table_args__ = (
        CheckConstraint(
            f"status IN {CustomerLifecycleStatus.ALL}",
            name="ck_crm_customer_status"
        ),
        CheckConstraint(
            f"account_type IN {AccountType.ALL}",
            name="ck_crm_customer_type"
        ),
        Index("ix_crm_customers_market_status", "market_id", "status"),
    )


class CRMContact(Base):
    __tablename__ = "crm_contacts"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="CASCADE"), nullable=False, index=True)
    first_name = Column(String(128), nullable=False)
    last_name = Column(String(128), nullable=True)
    email = Column(String(255), nullable=True, index=True)
    phone = Column(String(32), nullable=True, index=True)
    role = Column(String(64), nullable=False, default=ContactRole.PRIMARY)
    is_primary = Column(Boolean, nullable=False, default=False)
    is_verified = Column(Boolean, nullable=False, default=False)
    is_archived = Column(Boolean, nullable=False, default=False, index=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    customer = relationship("CRMCustomer", back_populates="contacts")

    __table_args__ = (
        CheckConstraint(
            f"role IN {ContactRole.ALL}",
            name="ck_crm_contact_role"
        ),
        # Partial unique index: at most one active non-archived primary contact per customer
        Index(
            "uq_crm_contacts_customer_primary",
            "crm_customer_id",
            unique=True,
            postgresql_where=(is_primary == True) & (is_archived == False),
            sqlite_where=(is_primary == 1) & (is_archived == 0)
        ),
    )


class CRMAddress(Base):
    __tablename__ = "crm_addresses"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="CASCADE"), nullable=False, index=True)
    address_type = Column(String(32), nullable=False, default=AddressType.INSTALLATION)
    address_line1 = Column(String(255), nullable=False)
    address_line2 = Column(String(255), nullable=True)
    city = Column(String(100), nullable=False)
    state = Column(String(100), nullable=False)
    postal_code = Column(String(20), nullable=False, index=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    serviceability_status = Column(String(32), nullable=False, default=ServiceabilityStatus.PENDING_SURVEY)
    candidate_node_id = Column(Integer, ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    distance_to_node_meters = Column(Float, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    customer = relationship("CRMCustomer", back_populates="addresses")

    __table_args__ = (
        CheckConstraint(
            f"address_type IN {AddressType.ALL}",
            name="ck_crm_address_type"
        ),
        CheckConstraint(
            f"serviceability_status IN {ServiceabilityStatus.ALL}",
            name="ck_crm_address_serviceability"
        ),
    )


class CRMDocument(Base):
    __tablename__ = "crm_documents"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    # ondelete="RESTRICT" protects KYC documents from accidental cascaded deletion
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    document_type = Column(String(64), nullable=False)
    document_identifier = Column(String(64), nullable=True)  # Masked e.g. XXXX-XXXX-1234 or XXXXX1234C

    # Deferred storage: file_key is NULL, upload_status is PENDING_UPLOAD in Phase 1
    file_key = Column(String(512), nullable=True)
    upload_status = Column(String(32), nullable=False, default=DocumentUploadStatus.PENDING_UPLOAD)
    file_name = Column(String(255), nullable=False)
    file_size_bytes = Column(Integer, nullable=False, default=0)
    mime_type = Column(String(128), nullable=False)

    verification_status = Column(String(32), nullable=False, default=VerificationStatus.PENDING)
    verified_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    customer = relationship("CRMCustomer", back_populates="documents")

    __table_args__ = (
        CheckConstraint(
            f"document_type IN {DocumentType.ALL}",
            name="ck_crm_document_type"
        ),
        CheckConstraint(
            f"upload_status IN {DocumentUploadStatus.ALL}",
            name="ck_crm_document_upload_status"
        ),
        CheckConstraint(
            f"verification_status IN ('PENDING', 'VERIFIED', 'REJECTED')",
            name="ck_crm_document_verification_status"
        ),
    )


class CRMAccountUser(Base):
    __tablename__ = "crm_account_users"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="CASCADE"), nullable=False, index=True)
    portal_role = Column(String(32), nullable=False, default=PortalRole.ACCOUNT_VIEWER)
    verification_status = Column(String(32), nullable=False, default=VerificationStatus.PENDING)
    is_active = Column(Boolean, nullable=False, default=True)
    verified_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    revocation_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    customer = relationship("CRMCustomer", back_populates="portal_users")
    user = relationship("User", foreign_keys=[user_id])
    verified_by = relationship("User", foreign_keys=[verified_by_id])

    __table_args__ = (
        UniqueConstraint("user_id", "crm_customer_id", name="uq_crm_account_user"),
        CheckConstraint(
            f"portal_role IN {PortalRole.ALL}",
            name="ck_crm_account_user_portal_role"
        ),
        CheckConstraint(
            f"verification_status IN {VerificationStatus.ALL}",
            name="ck_crm_account_user_verification_status"
        ),
    )


class CRMActivity(Base):
    __tablename__ = "crm_activities"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    # ondelete="RESTRICT" protects compliance audit trail from deletion
    crm_customer_id = Column(GUID(), ForeignKey("crm_customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    activity_type = Column(String(64), nullable=False)
    summary = Column(String(255), nullable=False)
    details = Column(Text, nullable=True)
    performed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    customer = relationship("CRMCustomer", back_populates="activities")

    __table_args__ = (
        CheckConstraint(
            f"activity_type IN {ActivityType.ALL}",
            name="ck_crm_activity_type"
        ),
    )
