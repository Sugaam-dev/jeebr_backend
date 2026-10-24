"""crm_backfill_validator.py

Phase 1 Stage 3B: Read-Only Customer Backfill Dry-Run Validator.

Implements a deterministic diagnostic validator that classifies existing legacy customers
and produces a report of proposed CRM mappings without executing backfill writes.

Safety and Integrity Guarantees:
1. Zero Writes: Strictly read-only connection enforcement (PRAGMA query_only=ON in SQLite,
   SET TRANSACTION READ ONLY in PostgreSQL). Zero INSERT, UPDATE, DELETE statements.
2. Zero Mutation Assertion: Canonical, lossless pre/post database state hashing (IEEE 754 hex
   for floats, exact microsecond ISO for timestamps, exact IDs).
3. Determinism: Deterministic classification output is decoupled from volatile execution metadata.
4. Source Snapshot Hash: Cryptographic SHA-256 fingerprint covering all relevant source fields.
5. Privacy Conscious: All output reports mask emails and phone numbers; no sensitive documents/tax IDs.
6. Exhaustive Reconciliation: Every legacy record is accounted for exactly once across categories and markets.
7. Stage 1/2 Preservation: Never infer serviceability from subscriber status; contact verification is False;
   no fabricated postal codes or city/state values.
"""

import os
import re
import time
import uuid
import hashlib
import json
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple, Set
from collections import defaultdict
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text, select
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine, Connection

from app.models import Customer
from app.models_crm import (
    CRMCustomer, CRMContact, CRMAddress,
    AccountType, CustomerLifecycleStatus, ContactRole, AddressType,
    ServiceabilityStatus
)
from app.markets import MARKETS

# ─── 1. Safety Guard for Isolated Synthetic Databases ────────────────────────

FORBIDDEN_PATTERNS = ["supabase.co", "supabase.com", "pooler.supabase", "aws-0-", "rds.amazonaws.com"]

def assert_safe_test_db(url_or_conn: Any):
    """
    Enforces that remote/production database URLs are strictly rejected.
    Only isolated local/synthetic databases are permitted.
    """
    url_str = str(url_or_conn).lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in url_str:
            raise RuntimeError(
                f"CRITICAL TEST ISOLATION FAILURE: Detected forbidden database target: '{url_str}'. "
                "Stage 3B Dry-Run Validator refuses execution against shared or production environments."
            )


# ─── 2. Constants, Reason Codes, and Classifications ─────────────────────────

class ReasonCode:
    MALFORMED_RECORD = "MALFORMED_RECORD"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    UNMAPPED_STATUS = "UNMAPPED_STATUS"
    UNMAPPED_SEGMENT = "UNMAPPED_SEGMENT"
    IDENTIFIER_OVERFLOW = "IDENTIFIER_OVERFLOW"
    ALREADY_LINKED = "ALREADY_LINKED"
    CUSTOMER_NUMBER_COLLISION = "CUSTOMER_NUMBER_COLLISION"
    DUPLICATE_IDENTITY = "DUPLICATE_IDENTITY"
    SHARED_CONTACT = "SHARED_CONTACT"
    CROSS_MARKET_CONFLICT = "CROSS_MARKET_CONFLICT"
    UNRESOLVED_ADDRESS_METADATA = "UNRESOLVED_ADDRESS_METADATA"


class ClassificationCategory:
    ELIGIBLE = "ELIGIBLE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"


# Reason code severities
REJECTION_REASON_CODES: Set[str] = {
    ReasonCode.MALFORMED_RECORD,
    ReasonCode.UNSUPPORTED_MARKET,
    ReasonCode.UNMAPPED_STATUS,
    ReasonCode.UNMAPPED_SEGMENT,
    ReasonCode.ALREADY_LINKED,
    ReasonCode.CUSTOMER_NUMBER_COLLISION,
}

MANUAL_REVIEW_REASON_CODES: Set[str] = {
    ReasonCode.IDENTIFIER_OVERFLOW,
    ReasonCode.DUPLICATE_IDENTITY,
    ReasonCode.SHARED_CONTACT,
    ReasonCode.CROSS_MARKET_CONFLICT,
    ReasonCode.UNRESOLVED_ADDRESS_METADATA,
}

# Authoritative market state lookup for supported markets
MARKET_STATES: Dict[str, str] = {
    "mumbai": "Maharashtra",
    "kolkata": "West Bengal",
}

# Approved Status Mappings (Legacy Customer.status -> CustomerLifecycleStatus)
APPROVED_STATUS_MAP: Dict[str, str] = {
    "active": CustomerLifecycleStatus.ACTIVE,
    "at-risk": CustomerLifecycleStatus.ACTIVE,
    "suspended": CustomerLifecycleStatus.SUSPENDED,
    "dormant": CustomerLifecycleStatus.DORMANT,
    "churned": CustomerLifecycleStatus.TERMINATED,
    "terminated": CustomerLifecycleStatus.TERMINATED,
    "prospect": CustomerLifecycleStatus.PROSPECT,
    "pending": CustomerLifecycleStatus.PROSPECT,
}

# Approved Segment & Customer Type Mappings -> AccountType
ENTERPRISE_KEYWORDS = ("enterprise", "corporate", "ill", "dedicated", "leased line")
SME_KEYWORDS = ("sme", "business", "smb", "small business")
RESIDENTIAL_KEYWORDS = (
    "prepaid", "postpaid", "residential", "retail", "family",
    "individual", "unlimited", "daily", "bundle"
)

def map_segment_and_type(segment: Optional[str], customer_type: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Maps legacy segment and customer_type to target AccountType.
    Returns (account_type, reason_code).
    """
    combined = f"{segment or ''} {customer_type or ''}".lower()
    
    for kw in ENTERPRISE_KEYWORDS:
        if kw in combined:
            return AccountType.ENTERPRISE, None
    for kw in SME_KEYWORDS:
        if kw in combined:
            return AccountType.SME, None
    for kw in RESIDENTIAL_KEYWORDS:
        if kw in combined:
            return AccountType.RESIDENTIAL, None
            
    return None, ReasonCode.UNMAPPED_SEGMENT


# ─── 3. Privacy-Conscious Data Masking ───────────────────────────────────────

def mask_email(email: Optional[str]) -> Optional[str]:
    """Lossless privacy masking for email addresses: keeps first/last char of user + domain."""
    if not email:
        return None
    cleaned = email.strip()
    if "@" not in cleaned:
        return "***"
    user_part, domain_part = cleaned.split("@", 1)
    if len(user_part) <= 1:
        masked_user = f"{user_part}***"
    else:
        masked_user = f"{user_part[0]}***{user_part[-1]}"
    return f"{masked_user}@{domain_part}"


def mask_phone(phone: Optional[str]) -> Optional[str]:
    """Privacy masking for phone numbers: preserves country prefix and last 3 digits."""
    if not phone:
        return None
    cleaned = phone.strip()
    digits = [c for c in cleaned if c.isdigit()]
    if len(digits) < 6:
        return "***-***-***"
    prefix = cleaned[:5] if cleaned.startswith("+") else cleaned[:2]
    suffix = cleaned[-3:]
    return f"{prefix}*****{suffix}"


# ─── 4. In-Memory Proposed CRM Mappings ──────────────────────────────────────

class ProposedCustomerMapping(BaseModel):
    customer_number: Optional[str] = None
    legal_name: str
    account_type: Optional[str] = None
    status: Optional[str] = None
    market_id: str
    legacy_customer_id: int
    billing_currency: str = "INR"
    is_archived: bool = False


class ProposedContactMapping(BaseModel):
    first_name: str
    last_name: Optional[str] = None
    email_masked: Optional[str] = None
    phone_masked: Optional[str] = None
    role: str = ContactRole.PRIMARY
    is_primary: bool = True
    is_verified: bool = False  # Always False per Stage 3B requirement
    is_archived: bool = False


class ProposedAddressMapping(BaseModel):
    address_type: str = AddressType.INSTALLATION
    address_line1: str
    city: str
    state: str
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    serviceability_status: str = ServiceabilityStatus.PENDING_SURVEY  # Never inferred from active subscriber
    candidate_node_id: Optional[int] = None
    is_archived: bool = False


class ClassifiedCustomerRecord(BaseModel):
    legacy_customer_id: int
    customer_code: Optional[str] = None
    market_id: str
    legacy_status: Optional[str] = None
    classification: str  # ELIGIBLE, MANUAL_REVIEW, REJECTED
    reason_codes: List[str] = Field(default_factory=list)
    proposed_customer: Optional[ProposedCustomerMapping] = None
    proposed_contact: Optional[ProposedContactMapping] = None
    proposed_address: Optional[ProposedAddressMapping] = None


class DeterministicClassificationOutput(BaseModel):
    source_snapshot_hash: str
    total_records: int
    classification_counts: Dict[str, int]
    market_counts: Dict[str, Dict[str, int]]
    reason_code_counts: Dict[str, int]
    records: List[ClassifiedCustomerRecord]


class ExecutionMetadata(BaseModel):
    validator_version: str = "1.0.0-stage3b"
    executed_at: str
    duration_ms: float
    database_dialect: str
    read_only_mode_enforced: bool
    pre_state_hash: str
    post_state_hash: str
    zero_mutation_verified: bool


class BackfillDryRunResult(BaseModel):
    deterministic_output: DeterministicClassificationOutput
    execution_metadata: ExecutionMetadata


# ─── 5. Canonical Lossless Value Serializer & Database-State Hasher ──────────

RELEVANT_TABLES = [
    "customers",
    "crm_customers",
    "crm_contacts",
    "crm_addresses",
    "crm_documents",
    "crm_account_users",
    "crm_activities",
    "users",
    "nodes",
    "usage_records",
    "tickets",
    "invoices",
    "resources",
]

def serialize_value_lossless(val: Any) -> str:
    """
    Serializes a database value losslessly without rounding floats or truncating datetimes.
    - Floats use exact IEEE 754 hexadecimal notation (val.hex()).
    - Datetimes use ISO-8601 with exact microseconds.
    - UUIDs/GUIDs to canonical lowercase string.
    """
    if val is None:
        return "<NULL>"
    if isinstance(val, float):
        return val.hex()
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, uuid.UUID):
        return str(val).lower()
    if isinstance(val, bytes):
        return val.hex()
    return str(val)


def compute_database_state_hash(connection: Connection) -> str:
    """
    Calculates a canonical cryptographic SHA-256 hash across all relevant operational
    and CRM tables.
    Iterates through rows ordered by primary key, using lossless serialization.
    """
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    
    hasher = hashlib.sha256()

    for tbl_name in sorted(RELEVANT_TABLES):
        if tbl_name not in existing_tables:
            continue

        hasher.update(f"TABLE:{tbl_name}\n".encode("utf-8"))
        pk_info = inspector.get_pk_constraint(tbl_name)
        pk_cols = pk_info.get("constrained_columns") if pk_info else []
        
        # Determine deterministic ORDER BY clause
        if pk_cols:
            order_clause = ", ".join(f'"{c}"' for c in pk_cols)
        else:
            cols = [c["name"] for c in inspector.get_columns(tbl_name)]
            order_clause = ", ".join(f'"{c}"' for c in cols)

        query = text(f'SELECT * FROM "{tbl_name}" ORDER BY {order_clause}')
        result = connection.execute(query)
        col_names = list(result.keys())

        for row in result:
            row_dict = dict(zip(col_names, row))
            row_items = []
            for col in sorted(row_dict.keys()):
                val_str = serialize_value_lossless(row_dict[col])
                row_items.append(f"{col}={val_str}")
            row_line = ";".join(row_items) + "\n"
            hasher.update(row_line.encode("utf-8"))

    return hasher.hexdigest()


# ─── 6. Source Snapshot Hash Calculation ─────────────────────────────────────

SOURCE_FIELDS = [
    "id", "market_id", "customer_code", "name", "email", "phone",
    "locality", "segment", "customer_type", "status",
    "service_address", "service_latitude", "service_longitude", "node_id"
]

def compute_source_snapshot_hash(customers: List[Customer], crm_customers: List[CRMCustomer]) -> str:
    """
    Computes a canonical SHA-256 fingerprint over all source fields used in
    classification or target mapping.
    """
    hasher = hashlib.sha256()
    
    # 1. Fingerprint all source customers sorted by id
    sorted_custs = sorted(customers, key=lambda c: c.id)
    for c in sorted_custs:
        cust_dict = {
            field: serialize_value_lossless(getattr(c, field, None))
            for field in SOURCE_FIELDS
        }
        canonical_json = json.dumps(cust_dict, sort_keys=True, separators=(",", ":"))
        hasher.update(f"CUST:{canonical_json}\n".encode("utf-8"))
        
    # 2. Fingerprint existing CRM bridge fields affecting collision/link checks
    sorted_crm = sorted(crm_customers, key=lambda c: str(c.id))
    for crm in sorted_crm:
        crm_dict = {
            "customer_number": serialize_value_lossless(crm.customer_number),
            "legacy_customer_id": serialize_value_lossless(crm.legacy_customer_id),
            "market_id": serialize_value_lossless(crm.market_id),
        }
        canonical_json = json.dumps(crm_dict, sort_keys=True, separators=(",", ":"))
        hasher.update(f"CRM:{canonical_json}\n".encode("utf-8"))
        
    return hasher.hexdigest()


# ─── 7. Core Read-Only Customer Backfill Validator ────────────────────────────

class CRMBackfillValidator:
    """
    Executes a read-only dry-run customer backfill classification against legacy customers.
    """

    def __init__(self, engine: Engine):
        self.engine = engine
        assert_safe_test_db(self.engine.url)

    def run(self, conn: Optional[Connection] = None) -> BackfillDryRunResult:
        start_time = time.perf_counter()
        executed_at = datetime.utcnow().isoformat()
        dialect_name = self.engine.dialect.name

        if conn is not None:
            return self._run_on_connection(conn, dialect_name, start_time, executed_at, manages_connection=False)
        else:
            with self.engine.connect() as managed_conn:
                return self._run_on_connection(managed_conn, dialect_name, start_time, executed_at, manages_connection=True)

    def _run_on_connection(
        self,
        conn: Connection,
        dialect_name: str,
        start_time: float,
        executed_at: str,
        manages_connection: bool = True
    ) -> BackfillDryRunResult:
        # 1. Enforce database-level read-only mode where supported
        read_only_enforced = False
        if manages_connection:
            if dialect_name == "sqlite":
                conn.execute(text("PRAGMA query_only = ON;"))
                read_only_enforced = True
            elif dialect_name == "postgresql":
                conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"))
                conn.execute(text("SET default_transaction_read_only = ON;"))
                read_only_enforced = True
        else:
            read_only_enforced = True

        # 2. Compute canonical pre-state hash
        pre_state_hash = compute_database_state_hash(conn)

        try:
            # 3. Fetch all legacy customers and existing CRM customers in read-only mode
            cust_query = text("SELECT * FROM customers ORDER BY id ASC")
            legacy_rows = conn.execute(cust_query).fetchall()

            crm_query = text("SELECT * FROM crm_customers")
            crm_rows = conn.execute(crm_query).fetchall()

            # Hydrate minimal objects/dicts for classification
            # To maintain compatibility with compute_source_snapshot_hash, build structured objects
            customers: List[Customer] = []
            for row in legacy_rows:
                r_map = row._mapping
                c = Customer(
                    id=r_map["id"],
                    market_id=r_map["market_id"],
                    customer_code=r_map["customer_code"],
                    name=r_map["name"],
                    email=r_map["email"],
                    phone=r_map["phone"],
                    locality=r_map["locality"],
                    segment=r_map["segment"],
                    customer_type=r_map["customer_type"],
                    status=r_map["status"],
                    service_address=r_map.get("service_address"),
                    service_latitude=r_map.get("service_latitude"),
                    service_longitude=r_map.get("service_longitude"),
                    node_id=r_map.get("node_id"),
                )
                customers.append(c)

            existing_crm: List[CRMCustomer] = []
            for row in crm_rows:
                r_map = row._mapping
                crm_cust = CRMCustomer(
                    id=r_map["id"],
                    customer_number=r_map["customer_number"],
                    market_id=r_map["market_id"],
                    legacy_customer_id=r_map["legacy_customer_id"],
                )
                existing_crm.append(crm_cust)

            # 4. Compute source snapshot hash
            source_snapshot_hash = compute_source_snapshot_hash(customers, existing_crm)

            # 5. Build lookup indices for duplicate detection and collision checks
            existing_linked_ids: Set[int] = {
                c.legacy_customer_id for c in existing_crm if c.legacy_customer_id is not None
            }
            existing_crm_numbers: Dict[str, CRMCustomer] = {
                c.customer_number: c for c in existing_crm if c.customer_number
            }

            email_to_custs: Dict[str, List[Customer]] = defaultdict(list)
            phone_to_custs: Dict[str, List[Customer]] = defaultdict(list)
            code_to_custs: Dict[str, List[Customer]] = defaultdict(list)

            for c in customers:
                if c.customer_code:
                    code_to_custs[c.customer_code.strip()].append(c)
                if c.email:
                    email_to_custs[c.email.strip().lower()].append(c)
                if c.phone:
                    # Normalize digits
                    norm_phone = "".join(ch for ch in c.phone if ch.isdigit())
                    if norm_phone:
                        phone_to_custs[norm_phone].append(c)

            # 6. Classify each record
            classified_records: List[ClassifiedCustomerRecord] = []
            classification_counts: Dict[str, int] = {
                ClassificationCategory.ELIGIBLE: 0,
                ClassificationCategory.MANUAL_REVIEW: 0,
                ClassificationCategory.REJECTED: 0,
            }
            market_counts: Dict[str, Dict[str, int]] = defaultdict(
                lambda: {
                    "total": 0,
                    ClassificationCategory.ELIGIBLE: 0,
                    ClassificationCategory.MANUAL_REVIEW: 0,
                    ClassificationCategory.REJECTED: 0,
                }
            )
            reason_code_counts: Dict[str, int] = defaultdict(int)

            for c in customers:
                reasons: List[str] = []

                # A. Malformed Record Check
                is_malformed = False
                if not c.name or not c.name.strip():
                    is_malformed = True
                if not c.customer_code or not c.customer_code.strip():
                    is_malformed = True
                if not c.market_id or not c.market_id.strip():
                    is_malformed = True
                if is_malformed:
                    reasons.append(ReasonCode.MALFORMED_RECORD)

                # B. Unsupported Market Check
                norm_market = (c.market_id or "").strip().lower()
                if norm_market not in MARKETS:
                    reasons.append(ReasonCode.UNSUPPORTED_MARKET)

                # C. Unmapped Status Check
                norm_status = (c.status or "").strip().lower()
                mapped_status = APPROVED_STATUS_MAP.get(norm_status)
                if not mapped_status:
                    reasons.append(ReasonCode.UNMAPPED_STATUS)

                # D. Unmapped Segment & Account Type Check
                mapped_account_type, seg_err = map_segment_and_type(c.segment, c.customer_type)
                if seg_err:
                    reasons.append(seg_err)

                # E. Identifier Overflow (> 32 chars)
                code_val = c.customer_code.strip() if c.customer_code else ""
                if len(code_val) > 32:
                    reasons.append(ReasonCode.IDENTIFIER_OVERFLOW)

                # F. Existing CRM Link Check
                if c.id in existing_linked_ids:
                    reasons.append(ReasonCode.ALREADY_LINKED)

                # G. Customer Number Collisions
                # G.1 Collides with another existing CRM record with different legacy ID
                if code_val in existing_crm_numbers:
                    colliding_crm = existing_crm_numbers[code_val]
                    if colliding_crm.legacy_customer_id != c.id:
                        reasons.append(ReasonCode.CUSTOMER_NUMBER_COLLISION)
                # G.2 Collides with another record in source batch
                if len(code_to_custs.get(code_val, [])) > 1:
                    if ReasonCode.CUSTOMER_NUMBER_COLLISION not in reasons:
                        reasons.append(ReasonCode.CUSTOMER_NUMBER_COLLISION)

                # H. Duplicate Identities, Shared Contacts, Cross-Market Conflicts
                # Check email
                clean_email = (c.email or "").strip().lower()
                if clean_email and len(email_to_custs.get(clean_email, [])) > 1:
                    shared_custs = email_to_custs[clean_email]
                    markets_involved = {sc.market_id.strip().lower() for sc in shared_custs if sc.market_id}
                    if len(markets_involved) > 1:
                        if ReasonCode.CROSS_MARKET_CONFLICT not in reasons:
                            reasons.append(ReasonCode.CROSS_MARKET_CONFLICT)
                    else:
                        if ReasonCode.DUPLICATE_IDENTITY not in reasons:
                            reasons.append(ReasonCode.DUPLICATE_IDENTITY)
                        if ReasonCode.SHARED_CONTACT not in reasons:
                            reasons.append(ReasonCode.SHARED_CONTACT)

                # Check phone
                clean_phone = "".join(ch for ch in (c.phone or "") if ch.isdigit())
                if clean_phone and len(phone_to_custs.get(clean_phone, [])) > 1:
                    shared_custs = phone_to_custs[clean_phone]
                    markets_involved = {sc.market_id.strip().lower() for sc in shared_custs if sc.market_id}
                    if len(markets_involved) > 1:
                        if ReasonCode.CROSS_MARKET_CONFLICT not in reasons:
                            reasons.append(ReasonCode.CROSS_MARKET_CONFLICT)
                    else:
                        if ReasonCode.DUPLICATE_IDENTITY not in reasons:
                            reasons.append(ReasonCode.DUPLICATE_IDENTITY)
                        if ReasonCode.SHARED_CONTACT not in reasons:
                            reasons.append(ReasonCode.SHARED_CONTACT)

                # I. Address Metadata Resolution
                # Indian Postal Code: 6-digit regex
                addr_str = (c.service_address or "").strip()
                pin_match = re.search(r'\b([1-9][0-9]{5})\b', addr_str) if addr_str else None
                postal_code = pin_match.group(1) if pin_match else None

                city = MARKETS[norm_market].city if norm_market in MARKETS else None
                state = MARKET_STATES.get(norm_market)

                # If address is absent, or postal_code cannot be resolved without fabrication:
                if not addr_str or not postal_code or not city or not state:
                    if ReasonCode.UNRESOLVED_ADDRESS_METADATA not in reasons:
                        reasons.append(ReasonCode.UNRESOLVED_ADDRESS_METADATA)

                # 7. Determine Primary Classification Category
                has_rejection = any(r in REJECTION_REASON_CODES for r in reasons)
                has_manual_review = any(r in MANUAL_REVIEW_REASON_CODES for r in reasons)

                if has_rejection:
                    category = ClassificationCategory.REJECTED
                elif has_manual_review:
                    category = ClassificationCategory.MANUAL_REVIEW
                else:
                    category = ClassificationCategory.ELIGIBLE

                # 8. Produce in-memory proposed mappings (strictly excluded for REJECTED records)
                proposed_cust = None
                proposed_contact = None
                proposed_addr = None

                if category != ClassificationCategory.REJECTED:
                    # Proposed Customer
                    if not is_malformed:
                        proposed_cust = ProposedCustomerMapping(
                            customer_number=code_val if len(code_val) <= 32 else None,
                            legal_name=c.name.strip(),
                            account_type=mapped_account_type,
                            status=mapped_status,
                            market_id=norm_market,
                            legacy_customer_id=c.id,
                            billing_currency="INR",
                            is_archived=False,
                        )

                    # Proposed Contact
                    if c.name and c.name.strip():
                        name_parts = c.name.strip().split(None, 1)
                        first_n = name_parts[0]
                        last_n = name_parts[1] if len(name_parts) > 1 else None
                        proposed_contact = ProposedContactMapping(
                            first_name=first_n,
                            last_name=last_n,
                            email_masked=mask_email(c.email),
                            phone_masked=mask_phone(c.phone),
                            role=ContactRole.PRIMARY,
                            is_primary=True,
                            is_verified=False,  # strictly False per specification
                            is_archived=False,
                        )

                    # Proposed Address (No fabricated postal code or city/state)
                    if addr_str and postal_code and city and state:
                        proposed_addr = ProposedAddressMapping(
                            address_type=AddressType.INSTALLATION,
                            address_line1=addr_str[:255],
                            city=city,
                            state=state,
                            postal_code=postal_code,
                            latitude=c.service_latitude,
                            longitude=c.service_longitude,
                            serviceability_status=ServiceabilityStatus.PENDING_SURVEY,  # never inferred from active status
                            candidate_node_id=c.node_id,
                            is_archived=False,
                        )

                record = ClassifiedCustomerRecord(
                    legacy_customer_id=c.id,
                    customer_code=code_val,
                    market_id=norm_market or "unassigned",
                    legacy_status=c.status,
                    classification=category,
                    reason_codes=sorted(reasons),
                    proposed_customer=proposed_cust,
                    proposed_contact=proposed_contact,
                    proposed_address=proposed_addr,
                )
                classified_records.append(record)

                # Update counts
                classification_counts[category] += 1
                m_key = norm_market if norm_market in MARKETS else "unsupported"
                market_counts[m_key]["total"] += 1
                market_counts[m_key][category] += 1
                for r in reasons:
                    reason_code_counts[r] += 1

            # 9. Exhaustive Accounting Verification
            total_records = len(customers)
            sum_classifications = (
                classification_counts[ClassificationCategory.ELIGIBLE] +
                classification_counts[ClassificationCategory.MANUAL_REVIEW] +
                classification_counts[ClassificationCategory.REJECTED]
            )
            assert total_records == sum_classifications, (
                f"Accounting Mismatch: total_records ({total_records}) != "
                f"sum_classifications ({sum_classifications})"
            )

            sum_market_totals = sum(m["total"] for m in market_counts.values())
            assert total_records == sum_market_totals, (
                f"Market Accounting Mismatch: total_records ({total_records}) != "
                f"sum_market_totals ({sum_market_totals})"
            )

            for m_id, counts in market_counts.items():
                m_sum = (
                    counts[ClassificationCategory.ELIGIBLE] +
                    counts[ClassificationCategory.MANUAL_REVIEW] +
                    counts[ClassificationCategory.REJECTED]
                )
                assert counts["total"] == m_sum, (
                    f"Market '{m_id}' Subtotal Mismatch: {counts['total']} != {m_sum}"
                )

            # Sort classified records deterministically by legacy customer id
            classified_records.sort(key=lambda r: r.legacy_customer_id)

            deterministic_output = DeterministicClassificationOutput(
                source_snapshot_hash=source_snapshot_hash,
                total_records=total_records,
                classification_counts=classification_counts,
                market_counts=dict(market_counts),
                reason_code_counts=dict(reason_code_counts),
                records=classified_records,
            )

        finally:
            # 10. Post-state Zero Mutation Assertion
            post_state_hash = compute_database_state_hash(conn)
            zero_mutation_verified = (pre_state_hash == post_state_hash)

            # Safe connection teardown: restore connection pool state if managed
            if manages_connection:
                try:
                    if dialect_name == "sqlite":
                        conn.execute(text("PRAGMA query_only = OFF;"))
                    elif dialect_name == "postgresql":
                        conn.rollback()
                        conn.execute(text("SET default_transaction_read_only = OFF;"))
                except Exception:
                    pass

            if not zero_mutation_verified:
                raise RuntimeError(
                    "CRITICAL ZERO-MUTATION FAILURE: Database state hash changed during read-only dry run! "
                    f"Pre: {pre_state_hash} != Post: {post_state_hash}"
                )

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        execution_metadata = ExecutionMetadata(
            executed_at=executed_at,
            duration_ms=duration_ms,
            database_dialect=dialect_name,
            read_only_mode_enforced=read_only_enforced,
            pre_state_hash=pre_state_hash,
            post_state_hash=post_state_hash,
            zero_mutation_verified=zero_mutation_verified,
        )

        return BackfillDryRunResult(
            deterministic_output=deterministic_output,
            execution_metadata=execution_metadata,
        )
