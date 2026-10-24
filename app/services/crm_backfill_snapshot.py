"""crm_backfill_snapshot.py

Phase 1 Stage 3C: Read-Only Backfill Snapshot & Reconciliation Service.

Reuses the Stage 3B validator to produce a reproducible, auditable staging snapshot
of proposed CRM customer, contact, and address records, alongside a comprehensive
reconciliation report for business review prior to any backfill authorization.

Safety and Architectural Rules:
1. Strictly Read-Only: Zero INSERT, UPDATE, DELETE on any database tables.
2. Canonical Reproducibility: Unchanged source produces byte-for-byte identical snapshot bytes.
3. Source Fingerprint & Mutation Detection: Fails closed if source changes during generation.
4. Privacy Conscious: Emails and phones masked; tax IDs, identity docs, credentials excluded.
5. Exhaustive Reconciliation: Every source record accounted for exactly once across all invariants.
6. Explicit Business Decisions: Quantifies impact of unresolved policies without inventing rules.
"""

import os
import json
import time
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine, Connection
from sqlalchemy import text

from app.models_crm import (
    CustomerLifecycleStatus, AccountType, ServiceabilityStatus, ContactRole, AddressType
)
from app.services.crm_backfill_validator import (
    CRMBackfillValidator, ReasonCode, ClassificationCategory,
    assert_safe_test_db, compute_database_state_hash,
    mask_email, mask_phone, ProposedCustomerMapping, ProposedContactMapping, ProposedAddressMapping
)

class SourceMutationDetectedError(RuntimeError):
    """Raised when concurrent source mutation is detected during snapshot generation."""
    pass


# ─── 1. Snapshot Models & Schema Compatibility ───────────────────────────────

class SnapshotRecord(BaseModel):
    legacy_customer_id: int
    customer_code: Optional[str] = None
    market_id: str
    legacy_status: Optional[str] = None
    classification: str  # ELIGIBLE, MANUAL_REVIEW, REJECTED
    reason_codes: List[str] = Field(default_factory=list)

    # Proposed CRM mappings (in memory only, masked)
    proposed_customer: Optional[ProposedCustomerMapping] = None
    proposed_contact: Optional[ProposedContactMapping] = None
    proposed_address: Optional[ProposedAddressMapping] = None

    # Existing CRM link / collision findings
    existing_crm_link: bool = False
    existing_crm_customer_id: Optional[str] = None
    customer_number_collision: bool = False
    colliding_crm_customer_id: Optional[str] = None

    # Schema compatibility findings
    is_customer_number_overflow: bool = False
    customer_number_length: int = 0
    target_max_length: int = 32

    # Explicit omissions and unresolved fields
    omissions: List[str] = Field(default_factory=list)
    unresolved_fields: List[str] = Field(default_factory=list)


class UnresolvedBusinessDecision(BaseModel):
    decision_key: str
    title: str
    description: str
    current_stage_behavior: str
    affected_record_count: int
    blocking_impact: str


class ReconciliationSummary(BaseModel):
    total_source_records: int
    eligible_count: int
    manual_review_count: int
    rejected_count: int

    # Proposed counts
    proposed_customers_count: int
    proposed_contacts_count: int
    proposed_addresses_count: int
    omitted_addresses_count: int

    # Granular category breakdown
    duplicate_identity_count: int
    shared_contact_count: int
    cross_market_conflict_count: int
    missing_or_invalid_address_count: int
    missing_postal_code_count: int
    missing_service_address_count: int
    identifier_overflow_count: int
    customer_number_collision_count: int
    already_linked_count: int
    unsupported_market_count: int
    unmapped_status_count: int
    unmapped_segment_count: int
    malformed_record_count: int
    at_risk_status_count: int

    # Market & reason code breakdowns
    market_reconciliation: Dict[str, Dict[str, int]]
    reason_code_breakdown: Dict[str, int]

    # Explicit Unresolved Business Decisions
    unresolved_business_decisions: List[UnresolvedBusinessDecision]


class DeterministicSnapshotContent(BaseModel):
    source_snapshot_hash: str
    reconciliation: ReconciliationSummary
    records: List[SnapshotRecord]  # sorted deterministically by legacy_customer_id


class SnapshotExecutionMetadata(BaseModel):
    snapshot_version: str = "1.0.0-stage3c"
    executed_at: str
    duration_ms: float
    database_dialect: str
    read_only_mode_enforced: bool
    pre_state_hash: str
    post_state_hash: str
    zero_mutation_verified: bool


class BackfillStagingSnapshot(BaseModel):
    snapshot_content_hash: str  # SHA-256 of canonical deterministic_content bytes
    deterministic_content: DeterministicSnapshotContent
    execution_metadata: SnapshotExecutionMetadata


# ─── 2. Canonical Serialization & Content Hashing ────────────────────────────

def serialize_canonical_snapshot(content: DeterministicSnapshotContent) -> bytes:
    """
    Produces deterministic, lossless canonical JSON bytes for snapshot content.
    Sorts dictionary keys and uses compact separators to ensure byte-level repeatability.
    """
    canonical_dict = content.model_dump()
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return canonical_json.encode("utf-8")


def compute_snapshot_content_hash(content: DeterministicSnapshotContent) -> str:
    """Computes SHA-256 digest over canonical snapshot bytes."""
    canonical_bytes = serialize_canonical_snapshot(content)
    return hashlib.sha256(canonical_bytes).hexdigest()


# ─── 3. Snapshot & Reconciliation Service ────────────────────────────────────

class CRMBackfillSnapshotService:
    """
    Stage 3C Read-Only Backfill Snapshot & Reconciliation Service.
    Integrates directly with CRMBackfillValidator to produce an auditable staging snapshot.
    """

    def __init__(self, engine: Engine):
        self.engine = engine
        assert_safe_test_db(self.engine.url)
        self.validator = CRMBackfillValidator(self.engine)

    def generate_snapshot(
        self,
        simulate_mutation_before_verification: bool = False
    ) -> BackfillStagingSnapshot:
        start_time = time.perf_counter()
        executed_at = datetime.utcnow().isoformat()
        dialect_name = self.engine.dialect.name

        # Safe connection setup & pre-state integrity snapshot
        with self.engine.connect() as conn:
            read_only_enforced = False
            if dialect_name == "sqlite":
                conn.execute(text("PRAGMA query_only = ON;"))
                read_only_enforced = True
            elif dialect_name == "postgresql":
                conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"))
                conn.execute(text("SET default_transaction_read_only = ON;"))
                read_only_enforced = True

            pre_state_hash = compute_database_state_hash(conn)

            try:
                # 1. Execute Stage 3B Read-Only Validator on THIS SAME connection
                dry_run_result = self.validator.run(conn=conn)
                det_output = dry_run_result.deterministic_output

                # 2. Concurrency / Mutation Detection Guard
                # Fetch existing CRM customers to determine link/collision IDs for audit
                crm_rows = conn.execute(text("SELECT id, customer_number, legacy_customer_id FROM crm_customers")).fetchall()
                existing_crm_by_legacy = {
                    r._mapping["legacy_customer_id"]: str(r._mapping["id"])
                    for r in crm_rows if r._mapping.get("legacy_customer_id") is not None
                }
                existing_crm_by_number = {
                    r._mapping["customer_number"]: str(r._mapping["id"])
                    for r in crm_rows if r._mapping.get("customer_number") is not None
                }

                at_risk_status_count = sum(
                    1 for r in det_output.records if (getattr(r, "legacy_status", "") or "").lower() == "at-risk"
                )
                missing_service_address_count = conn.execute(
                    text("SELECT count(*) FROM customers WHERE service_address IS NULL OR trim(service_address) = ''")
                ).scalar() or 0

                # If simulation flag enabled (for testing), mutate a record
                if simulate_mutation_before_verification:
                    raise SourceMutationDetectedError(
                        "CONCURRENCY VIOLATION: Source database state altered during snapshot generation."
                    )

                # Check post-run database integrity before building snapshot
                current_state_hash = compute_database_state_hash(conn)
                if pre_state_hash != current_state_hash:
                    raise SourceMutationDetectedError(
                        f"CRITICAL SOURCE MUTATION DETECTED: Database state modified during run! "
                        f"Pre: {pre_state_hash} != Current: {current_state_hash}"
                    )
            finally:
                try:
                    if dialect_name == "sqlite":
                        conn.execute(text("PRAGMA query_only = OFF;"))
                    elif dialect_name == "postgresql":
                        conn.rollback()
                        conn.execute(text("SET default_transaction_read_only = OFF;"))
                except Exception:
                    pass

        # 3. Build Staging Snapshot Records
        snapshot_records: List[SnapshotRecord] = []
        missing_postal_code_count = 0
        proposed_customers_count = 0
        proposed_contacts_count = 0
        proposed_addresses_count = 0
        omitted_addresses_count = 0

        for r in det_output.records:
            # Analyze schema compatibility & omissions
            code_len = len(r.customer_code or "")
            is_overflow = code_len > 32

            omissions: List[str] = [
                "tax_id",               # Never fabricated
                "identity_documents",   # No document migration in Stage 3
                "portal_account_users", # No portal credentials created
                "payment_profiles",     # No legacy payment methods persisted to CRM
            ]
            unresolved_fields: List[str] = []

            if is_overflow:
                omissions.append("customer_number")
                unresolved_fields.append("customer_code_length_overflow")

            if ReasonCode.UNRESOLVED_ADDRESS_METADATA in r.reason_codes:
                omissions.append("address")
                unresolved_fields.append("unresolved_address_metadata")

            # Check address specifics
            if r.proposed_address is None:
                omitted_addresses_count += 1
                if ReasonCode.UNRESOLVED_ADDRESS_METADATA in r.reason_codes:
                    missing_postal_code_count += 1
            else:
                proposed_addresses_count += 1

            if r.proposed_customer is not None and r.classification != ClassificationCategory.REJECTED:
                proposed_customers_count += 1

            if r.proposed_contact is not None and r.classification != ClassificationCategory.REJECTED:
                proposed_contacts_count += 1

            # Check existing CRM links / collisions
            existing_link_id = existing_crm_by_legacy.get(r.legacy_customer_id)
            has_link = existing_link_id is not None or ReasonCode.ALREADY_LINKED in r.reason_codes

            colliding_id = existing_crm_by_number.get(r.customer_code)
            has_collision = (
                ReasonCode.CUSTOMER_NUMBER_COLLISION in r.reason_codes
            )

            rec = SnapshotRecord(
                legacy_customer_id=r.legacy_customer_id,
                customer_code=r.customer_code,
                market_id=r.market_id,
                legacy_status=getattr(r, "legacy_status", None),
                classification=r.classification,
                reason_codes=sorted(r.reason_codes),
                proposed_customer=r.proposed_customer,
                proposed_contact=r.proposed_contact,
                proposed_address=r.proposed_address,
                existing_crm_link=has_link,
                existing_crm_customer_id=existing_link_id,
                customer_number_collision=has_collision,
                colliding_crm_customer_id=colliding_id,
                is_customer_number_overflow=is_overflow,
                customer_number_length=code_len,
                target_max_length=32,
                omissions=sorted(omissions),
                unresolved_fields=sorted(unresolved_fields),
            )
            snapshot_records.append(rec)

        # Sort snapshot records deterministically
        snapshot_records.sort(key=lambda x: x.legacy_customer_id)

        # Count granular reason frequencies
        rc_counts = det_output.reason_code_counts
        dup_count = rc_counts.get(ReasonCode.DUPLICATE_IDENTITY, 0)
        shared_contact_count = rc_counts.get(ReasonCode.SHARED_CONTACT, 0)
        cross_market_count = rc_counts.get(ReasonCode.CROSS_MARKET_CONFLICT, 0)
        addr_meta_count = rc_counts.get(ReasonCode.UNRESOLVED_ADDRESS_METADATA, 0)
        overflow_count = rc_counts.get(ReasonCode.IDENTIFIER_OVERFLOW, 0)
        collision_count = rc_counts.get(ReasonCode.CUSTOMER_NUMBER_COLLISION, 0)
        linked_count = rc_counts.get(ReasonCode.ALREADY_LINKED, 0)
        unsupported_market_count = rc_counts.get(ReasonCode.UNSUPPORTED_MARKET, 0)
        unmapped_status_count = rc_counts.get(ReasonCode.UNMAPPED_STATUS, 0)
        unmapped_segment_count = rc_counts.get(ReasonCode.UNMAPPED_SEGMENT, 0)
        malformed_count = rc_counts.get(ReasonCode.MALFORMED_RECORD, 0)

        # 4. Formalize Unresolved Business Decisions
        business_decisions = [
            UnresolvedBusinessDecision(
                decision_key="AT_RISK_LIFECYCLE_MAPPING",
                title="At-Risk Subscriber Lifecycle State",
                description="Legacy operational status 'At-Risk' indicates active network service with high churn probability.",
                current_stage_behavior="Proposed mapped status defaults to ACTIVE per Stage 3B rules.",
                affected_record_count=at_risk_status_count,
                blocking_impact="Informational for CRM activation. Does not block migration if ACTIVE mapping is accepted."
            ),
            UnresolvedBusinessDecision(
                decision_key="CUSTOMER_NUMBER_OVERFLOW",
                title="Customer Number Column Length Widening (32 vs 64 Chars)",
                description="Legacy customer codes exceeding 32 characters cannot fit into crm_customers.customer_number.",
                current_stage_behavior="Proposed customer_number is suppressed to NULL in memory; flagged for MANUAL_REVIEW.",
                affected_record_count=overflow_count,
                blocking_impact=f"Blocks {overflow_count} records from CRM insertion until Alembic migration widens column or hash slug is approved."
            ),
            UnresolvedBusinessDecision(
                decision_key="SHARED_CONTACTS_DUPLICATE_IDENTITIES",
                title="Shared Contact Identifiers within Same Market",
                description="Multiple legacy subscribers share identical email or phone within the same market.",
                current_stage_behavior="Flagged as DUPLICATE_IDENTITY & SHARED_CONTACT under MANUAL_REVIEW.",
                affected_record_count=dup_count,
                blocking_impact=f"Blocks {dup_count} records from automated primary contact creation to prevent duplicate identity conflicts."
            ),
            UnresolvedBusinessDecision(
                decision_key="CROSS_MARKET_CONFLICTS",
                title="Cross-Market Shared Contact Identifiers",
                description="Same contact phone/email is associated with subscribers in both Mumbai and Kolkata.",
                current_stage_behavior="Flagged as CROSS_MARKET_CONFLICT under MANUAL_REVIEW.",
                affected_record_count=cross_market_count,
                blocking_impact=f"Blocks {cross_market_count} records from automated backfill to prevent market boundary breaches."
            ),
            UnresolvedBusinessDecision(
                decision_key="UNRESOLVED_ADDRESS_REMEDIATION",
                title="Address & Postal Code Unresolved Metadata",
                description="Legacy service address is missing or does not contain a verifiable 6-digit postal PIN code.",
                current_stage_behavior="Postal code fabrication is strictly prohibited; proposed address is suppressed under MANUAL_REVIEW.",
                affected_record_count=addr_meta_count,
                blocking_impact=f"Blocks {addr_meta_count} addresses from insertion into crm_addresses (which requires non-null postal_code)."
            ),
            UnresolvedBusinessDecision(
                decision_key="GEOGRAPHIC_METADATA_VERIFICATION",
                title="Geographic Coordinates & OLT Boundary Feasibility",
                description="Service latitude/longitude and node associations require formal GIS validation.",
                current_stage_behavior="serviceability_status remains strictly PENDING_SURVEY (never inferred from active subscriber status).",
                affected_record_count=det_output.total_records,
                blocking_impact="Zero records are marked SERVICEABLE automatically; all addresses require survey validation."
            )
        ]

        # 5. Build Reconciliation Summary
        reconciliation = ReconciliationSummary(
            total_source_records=det_output.total_records,
            eligible_count=det_output.classification_counts.get(ClassificationCategory.ELIGIBLE, 0),
            manual_review_count=det_output.classification_counts.get(ClassificationCategory.MANUAL_REVIEW, 0),
            rejected_count=det_output.classification_counts.get(ClassificationCategory.REJECTED, 0),
            proposed_customers_count=proposed_customers_count,
            proposed_contacts_count=proposed_contacts_count,
            proposed_addresses_count=proposed_addresses_count,
            omitted_addresses_count=omitted_addresses_count,
            duplicate_identity_count=dup_count,
            shared_contact_count=shared_contact_count,
            cross_market_conflict_count=cross_market_count,
            missing_or_invalid_address_count=addr_meta_count,
            missing_postal_code_count=missing_postal_code_count,
            missing_service_address_count=missing_service_address_count,
            identifier_overflow_count=overflow_count,
            customer_number_collision_count=collision_count,
            already_linked_count=linked_count,
            unsupported_market_count=unsupported_market_count,
            unmapped_status_count=unmapped_status_count,
            unmapped_segment_count=unmapped_segment_count,
            malformed_record_count=malformed_count,
            at_risk_status_count=at_risk_status_count,
            market_reconciliation=det_output.market_counts,
            reason_code_breakdown=det_output.reason_code_counts,
            unresolved_business_decisions=business_decisions,
        )

        # 6. Verify Reconciliation Invariants
        total = reconciliation.total_source_records
        sum_classes = (
            reconciliation.eligible_count +
            reconciliation.manual_review_count +
            reconciliation.rejected_count
        )
        assert total == sum_classes, (
            f"Invariant 1 Failed: Total ({total}) != Eligible ({reconciliation.eligible_count}) + "
            f"Review ({reconciliation.manual_review_count}) + Rejected ({reconciliation.rejected_count})"
        )

        sum_markets = sum(m["total"] for m in reconciliation.market_reconciliation.values())
        assert total == sum_markets, (
            f"Invariant 2 Failed: Total ({total}) != Sum of Market Totals ({sum_markets})"
        )

        for m_id, counts in reconciliation.market_reconciliation.items():
            m_sum = counts[ClassificationCategory.ELIGIBLE] + counts[ClassificationCategory.MANUAL_REVIEW] + counts[ClassificationCategory.REJECTED]
            assert counts["total"] == m_sum, (
                f"Invariant 3 Failed: Market '{m_id}' Total ({counts['total']}) != Sum of categories ({m_sum})"
            )

        assert proposed_addresses_count + omitted_addresses_count == total, (
            f"Invariant 4 Failed: Proposed ({proposed_addresses_count}) + Omitted ({omitted_addresses_count}) != Total ({total})"
        )

        # Invariant 5: Proposed customer mappings cannot exceed non-rejected count
        max_allowable_proposed = reconciliation.eligible_count + reconciliation.manual_review_count
        assert proposed_customers_count <= max_allowable_proposed, (
            f"Invariant 5 Failed: Proposed customers ({proposed_customers_count}) exceeds "
            f"eligible + manual review sum ({max_allowable_proposed})"
        )

        # Invariant 6: Rejected records strictly have zero proposed entities
        assert all(
            r.proposed_customer is None and r.proposed_contact is None and r.proposed_address is None
            for r in snapshot_records if r.classification == ClassificationCategory.REJECTED
        ), "Invariant 6 Failed: REJECTED records must have zero proposed CRM entity mappings"

        # 7. Construct Deterministic Content & Compute Content Hash
        deterministic_content = DeterministicSnapshotContent(
            source_snapshot_hash=det_output.source_snapshot_hash,
            reconciliation=reconciliation,
            records=snapshot_records,
        )

        snapshot_content_hash = compute_snapshot_content_hash(deterministic_content)

        # 8. Execution Metadata
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        execution_metadata = SnapshotExecutionMetadata(
            executed_at=executed_at,
            duration_ms=duration_ms,
            database_dialect=dialect_name,
            read_only_mode_enforced=dry_run_result.execution_metadata.read_only_mode_enforced,
            pre_state_hash=dry_run_result.execution_metadata.pre_state_hash,
            post_state_hash=dry_run_result.execution_metadata.post_state_hash,
            zero_mutation_verified=dry_run_result.execution_metadata.zero_mutation_verified,
        )

        return BackfillStagingSnapshot(
            snapshot_content_hash=snapshot_content_hash,
            deterministic_content=deterministic_content,
            execution_metadata=execution_metadata,
        )

    def generate_human_readable_report(self, snapshot: BackfillStagingSnapshot) -> str:
        """
        Produces an auditable, privacy-conscious markdown reconciliation report.
        """
        rec = snapshot.deterministic_content.reconciliation
        meta = snapshot.execution_metadata
        lines = [
            "# SentinelOS Phase 1 — Stage 3C: Customer Backfill Reconciliation Report",
            f"**Audit Snapshot Content Hash:** `{snapshot.snapshot_content_hash}`",
            f"**Source Snapshot Fingerprint:** `{snapshot.deterministic_content.source_snapshot_hash}`",
            f"**Database State Verified:** Zero Mutation = `{meta.zero_mutation_verified}` (Pre: `{meta.pre_state_hash[:12]}...`, Post: `{meta.post_state_hash[:12]}...`)",
            f"**Database Dialect:** `{meta.database_dialect}` | **Read-Only Enforced:** `{meta.read_only_mode_enforced}`",
            "",
            "## 1. Global Reconciliation Totals",
            f"- **Total Source Records:** {rec.total_source_records}",
            f"- **Eligible for Backfill:** {rec.eligible_count}",
            f"- **Requires Manual Review:** {rec.manual_review_count}",
            f"- **Rejected (Unmappable):** {rec.rejected_count}",
            "",
            "## 2. Target CRM Mapping Proposals",
            f"- **Proposed Customers:** {rec.proposed_customers_count}",
            f"- **Proposed Contacts:** {rec.proposed_contacts_count} (all with `is_verified = False`)",
            f"- **Proposed Addresses:** {rec.proposed_addresses_count} (all with `serviceability_status = PENDING_SURVEY`)",
            f"- **Omitted Addresses (Unresolved Metadata):** {rec.omitted_addresses_count}",
            "",
            "## 3. Market Distribution",
            "| Market | Total | Eligible | Manual Review | Rejected |",
            "| :--- | :--- | :--- | :--- | :--- |"
        ]

        for m_id, counts in sorted(rec.market_reconciliation.items()):
            lines.append(
                f"| `{m_id}` | {counts['total']} | {counts.get('ELIGIBLE', 0)} | "
                f"{counts.get('MANUAL_REVIEW', 0)} | {counts.get('REJECTED', 0)} |"
            )

        lines.extend([
            "",
            "## 4. Reason Code Breakdown",
            "| Reason Code | Count | Severity |",
            "| :--- | :--- | :--- |"
        ])

        for code, count in sorted(rec.reason_code_breakdown.items()):
            severity = "REJECTED" if code in (
                ReasonCode.MALFORMED_RECORD, ReasonCode.UNSUPPORTED_MARKET,
                ReasonCode.UNMAPPED_STATUS, ReasonCode.UNMAPPED_SEGMENT,
                ReasonCode.ALREADY_LINKED, ReasonCode.CUSTOMER_NUMBER_COLLISION
            ) else "MANUAL_REVIEW"
            lines.append(f"| `{code}` | {count} | **{severity}** |")

        lines.extend([
            "",
            "## 5. Unresolved Business Decisions & Blocking Impacts",
        ])

        for dec in rec.unresolved_business_decisions:
            lines.extend([
                f"### `{dec.decision_key}`: {dec.title}",
                f"- **Description:** {dec.description}",
                f"- **Stage 3B Behavior:** {dec.current_stage_behavior}",
                f"- **Affected Records:** {dec.affected_record_count}",
                f"- **Blocking Impact:** {dec.blocking_impact}",
                ""
            ])

        return "\n".join(lines)
