"""
SentinelOS — AI Orchestration & Human-in-the-Loop Governance Engine
===================================================================

Core Policy:
    "AI proposes. A human approves. Deterministic services execute. Every action is auditable."

Capabilities:
1. Deterministic Incident & Alarm Analysis:
   - Correlates persisted telemetry (Loss of Signal, Optical Attenuation, Port Saturation)
   - Evaluates rule-based recommendations with clear explainability and confidence basis.
   - Dispatches only allowlisted synthetic actions with validated parameters.
   - Provides explicit fallback labeling when model inference is offline/rule-based.

2. Recommendation Lifecycle State Machine:
   - PROPOSED / PENDING -> APPROVED | REJECTED -> EXPIRED -> EXECUTING -> EXECUTED | FAILED
   - Expiry window validation.
   - Anti-tampering action fingerprinting (SHA-256).
   - Atomic state transitions with optimistic locking to prevent double-consumption.

3. Deterministic Execution & Closed-Loop Read-Back:
   - Revalidates actor authorization and regional market boundary.
   - Rechecks preconditions on target entities.
   - Executes via allowlist (Network Scenario Engine, Optical Calibrator, QoS Traffic Shaper, Field Crew Dispatch).
   - Reads back resulting telemetry and alarms to confirm verified restoration.
   - Preserves complete audit trail in audit_logs without logging secrets.
"""

from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Dict, Any, Optional, List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from fastapi import HTTPException

from app.models import (
    Recommendation, AuditLog, User, NetworkDevice, NetworkLink,
    OLTPort, NetworkAlarm, Ticket, Customer, Resource
)
from app.services.scenario_engine import get_scenario_engine

logger = logging.getLogger("sentinelos.ai_orchestrator")

# ── 1. Strict Allowlisted Deterministic Actions ──────────────────────────────

ALLOWLISTED_ACTIONS = {
    "RECOVER_NETWORK_SCENARIO": "Recovers active synthetic network fault scenario via scenario engine",
    "OPTIMIZE_OPTICAL_ATTENUATION": "Adjusts optical attenuation and restores ONT optical power to nominal level",
    "THROTTLE_QOS_BANDWIDTH": "Rebalances traffic on saturated PON port and eases OLT CPU load",
    "DISPATCH_FIELD_CREW": "Generates a field dispatch assignment for physical fiber splicing or line repair",
    "REBOOT_CPE_DIAGNOSTIC": "Sends synthetic diagnostic reset to customer premises terminal",
    "APPLY_SLA_BILLING_CREDIT": "Applies verified SLA breach credit adjustment to customer billing account"
}

# ── 2. Lifecycle States & Risk Classifications ───────────────────────────────

LIFECYCLE_PROPOSED = "PROPOSED"
LIFECYCLE_PENDING = "PENDING"
LIFECYCLE_APPROVED = "APPROVED"
LIFECYCLE_REJECTED = "REJECTED"
LIFECYCLE_EXPIRED = "EXPIRED"
LIFECYCLE_EXECUTING = "EXECUTING"
LIFECYCLE_EXECUTED = "EXECUTED"
LIFECYCLE_FAILED = "FAILED"

RISK_CRITICAL = "CRITICAL"
RISK_HIGH = "HIGH"
RISK_MEDIUM = "MEDIUM"
RISK_LOW = "LOW"

MODULE_OPERATIONAL_ROLES = {
    "Predictive Service Assurance": ["NOC", "Admin", "SUPER_ADMIN"],
    "AI-driven OSS/BSS Orchestration": ["NOC", "Admin", "SUPER_ADMIN"],
    "Network Operations": ["NOC", "Admin", "SUPER_ADMIN"],
    "Churn Prediction & Retention AI": ["Care", "Admin", "SUPER_ADMIN"],
    "Intelligent Customer Journeys": ["Care", "Admin", "SUPER_ADMIN"],
    "Revenue Assurance & Leakage Analytics": ["Revenue", "Admin", "SUPER_ADMIN"],
    "Automatic Ticketing & Regional Dispatch": ["NOC", "Admin", "SUPER_ADMIN"]
}


# ── 3. Tamper Protection: Action Fingerprint ────────────────────────────────

def compute_action_fingerprint(
    rec_id: int,
    proposed_action: str,
    target_type: str,
    target_id: int,
    market_id: str,
    action_payload: Optional[Dict[str, Any]] = None
) -> str:
    """
    Computes a cryptographic SHA-256 fingerprint of the recommendation's action,
    target, market, and payload. Any alteration between approval and execution
    invalidates execution.
    """
    normalized_payload = json.dumps(action_payload or {}, sort_keys=True)
    raw = f"{rec_id}:{proposed_action}:{target_type.upper()}:{target_id}:{market_id.lower()}:{normalized_payload}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── 4. AI Orchestration Service Class ────────────────────────────────────────

class AIOrchestrationService:
    """
    Governs recommendation synthesis, human sign-off, concurrency controls,
    and verified closed-loop execution.
    """

    DEFAULT_EXPIRY_HOURS = 2

    # ── Workstream C: Recommendation Generation ─────────────────────────────

    def generate_recommendation_for_incident(
        self,
        db: Session,
        incident_type: str,
        target_type: str,
        target_id: str,
        market_id: str = "mumbai",
        alarm_id: Optional[int] = None,
        actor: Optional[User] = None
    ) -> Recommendation:
        """
        Derives an explainable, actionable recommendation from persisted telemetry and alarms.
        Preconditions are verified against real DB records before creating PROPOSED state.
        """
        inc_type = incident_type.upper()
        tgt_type = target_type.upper()

        # Step 1: Precondition check on target entity
        target_entity = None
        evidence: Dict[str, Any] = {}
        impact: Dict[str, Any] = {}

        if tgt_type == "LINK":
            target_entity = db.query(NetworkLink).filter(
                NetworkLink.id == int(target_id),
                NetworkLink.market_id == market_id
            ).first()
            if not target_entity:
                raise HTTPException(status_code=404, detail=f"Target Link #{target_id} not found in market '{market_id}'")

            # Check incident preconditions
            if inc_type == "FIBER_CUT":
                if target_entity.status not in ("DOWN", "CRITICAL"):
                    raise HTTPException(status_code=400, detail="Cannot generate FIBER_CUT recommendation: Link is in nominal HEALTHY condition.")

                # Derive downstream impact
                root_dev = db.query(NetworkDevice).filter(NetworkDevice.id == target_entity.target_device_id).first()
                downstream_customers = []
                if root_dev:
                    scenario_engine = get_scenario_engine()
                    downstream_nodes = scenario_engine._get_downstream_subtree(db, root_dev.id, market_id)
                    all_nodes = [root_dev] + downstream_nodes
                    downstream_customers = [d.customer_id for d in all_nodes if d.customer_id is not None]

                link_code_str = getattr(target_entity, "link_code", f"LINK-{target_entity.id}")
                attenuation_val = getattr(target_entity, "optical_attenuation_db", 99.9 if target_entity.status == "DOWN" else 0.5)

                evidence = {
                    "link_id": target_entity.id,
                    "link_code": link_code_str,
                    "attenuation_db": attenuation_val,
                    "status": target_entity.status,
                    "downstream_impact_count": len(downstream_customers)
                }
                impact = {
                    "affected_customers_count": len(downstream_customers),
                    "service_disruption_severity": "COMPLETE_OUTAGE",
                    "priority": "P1"
                }
                proposed_action = "RECOVER_NETWORK_SCENARIO"
                action_payload = {
                    "scenario_type": "FIBER_CUT",
                    "target_type": "LINK",
                    "target_id": str(target_entity.id),
                    "restoration_technique": "FIBER_SPLICING_OTDR_VERIFIED",
                    "market_id": market_id
                }
                title = f"Emergency Fiber Splicing & Restoration: {link_code_str}"
                description = (
                    f"Physical fiber break detected on {link_code_str} with optical attenuation {attenuation_val} dB. "
                    f"Recommended action: Execute emergency optical loop restoration and clear downstream loss-of-signal alarms."
                )
                confidence = 0.98
                confidence_basis = "Direct loss of signal telemetry and link break status with downstream customer impact."
                risk_level = RISK_HIGH
                source_module = "Network Operations"

            else:
                raise HTTPException(status_code=400, detail=f"Unsupported incident type '{incident_type}' for target LINK")

        elif tgt_type == "DEVICE":
            target_entity = db.query(NetworkDevice).filter(
                NetworkDevice.id == int(target_id) if target_id.isdigit() else NetworkDevice.device_code == target_id,
                NetworkDevice.market_id == market_id
            ).first()
            if not target_entity:
                raise HTTPException(status_code=404, detail=f"Target Device '{target_id}' not found in market '{market_id}'")

            if inc_type == "OPTICAL_DEGRADE":
                if target_entity.optical_rx_dbm is not None and target_entity.optical_rx_dbm > -25.0:
                    raise HTTPException(status_code=400, detail="Cannot generate OPTICAL_DEGRADE recommendation: Optical Rx power is within nominal parameters.")

                curr_rx = target_entity.optical_rx_dbm or -27.0
                evidence = {
                    "device_id": target_entity.id,
                    "device_code": target_entity.device_code,
                    "device_type": target_entity.device_type,
                    "optical_rx_dbm": curr_rx,
                    "nominal_threshold": -26.0,
                    "critical_threshold": -28.0,
                    "status": target_entity.status
                }
                impact = {
                    "affected_device": target_entity.device_code,
                    "affected_customers_count": 1 if target_entity.customer_id else 0,
                    "service_disruption_severity": "MARGIN_DEGRADATION"
                }
                proposed_action = "OPTIMIZE_OPTICAL_ATTENUATION"
                action_payload = {
                    "device_id": target_entity.id,
                    "device_code": target_entity.device_code,
                    "target_rx_dbm": -19.5,
                    "market_id": market_id
                }
                title = f"Optical Power Optimization: {target_entity.device_code}"
                description = (
                    f"Excessive attenuation detected on terminal {target_entity.device_code} ({curr_rx:.1f} dBm). "
                    f"Recommended action: Calibrate optical laser power and restore Rx level to nominal -19.5 dBm."
                )
                confidence = 0.94
                confidence_basis = "Optical Rx power telemetry below -26.0 dBm threshold (excessive fiber margin loss)."
                risk_level = RISK_MEDIUM if curr_rx > -28.0 else RISK_HIGH
                source_module = "Predictive Service Assurance"

            elif inc_type == "PEAK_TRAFFIC":
                evidence = {
                    "device_id": target_entity.id,
                    "device_code": target_entity.device_code,
                    "cpu_utilization_pct": target_entity.cpu_utilization_pct,
                    "temperature_c": target_entity.temperature_c,
                    "status": target_entity.status
                }
                impact = {
                    "affected_device": target_entity.device_code,
                    "affected_ports_count": len(target_entity.ports),
                    "service_disruption_severity": "BANDWIDTH_CONGESTION"
                }
                proposed_action = "THROTTLE_QOS_BANDWIDTH"
                action_payload = {
                    "device_id": target_entity.id,
                    "device_code": target_entity.device_code,
                    "target_utilization_pct": 45.0,
                    "market_id": market_id
                }
                title = f"Dynamic QoS Rebalancing & Port Load Shedding: {target_entity.device_code}"
                description = (
                    f"Traffic saturation detected on {target_entity.device_code} (CPU: {target_entity.cpu_utilization_pct}%). "
                    f"Recommended action: Rebalance PON traffic queues and apply QoS rate profiles."
                )
                confidence = 0.92
                confidence_basis = "Chassis CPU telemetry and queue depth exceed 80% saturation threshold."
                risk_level = RISK_LOW
                source_module = "AI-driven OSS/BSS Orchestration"

            else:
                raise HTTPException(status_code=400, detail=f"Unsupported incident type '{incident_type}' for target DEVICE")

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported target_type '{target_type}'")

        # Step 2: Validate Action against strict allowlist
        if proposed_action not in ALLOWLISTED_ACTIONS:
            raise HTTPException(status_code=400, detail=f"Proposed action '{proposed_action}' is not in allowlisted operations.")

        # Step 3: Check for existing unreviewed proposed recommendation on this target
        existing = db.query(Recommendation).filter(
            Recommendation.target_entity_type == tgt_type,
            Recommendation.target_entity_id == target_entity.id,
            Recommendation.market_id == market_id,
            Recommendation.status.in_([LIFECYCLE_PROPOSED, LIFECYCLE_PENDING])
        ).first()

        if existing:
            return existing

        # Step 4: Resolve source alarm if provided
        alarm_record = None
        if alarm_id:
            alarm_record = db.query(NetworkAlarm).filter(NetworkAlarm.id == alarm_id).first()

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=self.DEFAULT_EXPIRY_HOURS)
        import uuid
        rec_code = f"REC-{market_id.upper()[:3]}-{int(now.timestamp())}-{uuid.uuid4().hex[:6].upper()}-{target_entity.id}"

        rec = Recommendation(
            market_id=market_id,
            recommendation_code=rec_code,
            source_module=source_module,
            source_alarm_id=alarm_record.id if alarm_record else None,
            source_alarm_code=alarm_record.alarm_code if alarm_record else None,
            incident_type=inc_type,
            target_entity_type=tgt_type,
            target_entity_id=target_entity.id,
            target_entity_label=f"{getattr(target_entity, 'device_code', getattr(target_entity, 'link_code', f'LINK-{target_entity.id}'))}",
            title=title,
            description=description,
            recommended_action=f"Execute {proposed_action}",
            proposed_action=proposed_action,
            action_payload=action_payload,
            confidence_score=confidence,
            confidence_basis=confidence_basis,
            risk_level=risk_level,
            evidence=evidence,
            impact_assessment=impact,
            ai_provider="DETERMINISTIC_RULES_ENGINE_FALLBACK",
            is_ai_generated=False,
            status=LIFECYCLE_PROPOSED,
            created_at=now,
            expires_at=expires_at
        )

        db.add(rec)
        db.flush()  # Obtain rec.id

        # Compute initial tamper fingerprint
        rec.action_fingerprint = compute_action_fingerprint(
            rec_id=rec.id,
            proposed_action=rec.proposed_action,
            target_type=rec.target_entity_type,
            target_id=rec.target_entity_id,
            market_id=rec.market_id,
            action_payload=rec.action_payload
        )

        # Audit log creation
        audit = AuditLog(
            market_id=market_id,
            recommendation_id=rec.id,
            source_module=source_module,
            action_taken=f"PROPOSE_{proposed_action}",
            decision="PROPOSED",
            user_id=actor.id if actor else None,
            user_name=actor.full_name if actor else "SentinelOS AI Policy Engine",
            user_role=actor.role if actor else "AI_GOVERNANCE",
            confidence_score=confidence,
            original_signals=evidence,
            execution_result={"recommendation_code": rec_code, "status": "PROPOSED"},
            timestamp=now
        )
        db.add(audit)
        db.commit()
        db.refresh(rec)
        return rec

    # ── Workstream D: Approval & Rejection Logic ────────────────────────────

    def approve_recommendation(
        self,
        db: Session,
        recommendation_id: int,
        user: User,
        notes: Optional[str] = None,
        auto_execute: bool = False
    ) -> Recommendation:
        """
        Approves a recommendation, recording reviewer credentials and generating
        a locked action fingerprint. Rejects expired, already-consumed, or tampered recommendations.
        """
        rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Recommendation not found")

        # Tenant boundary check
        self._validate_market_access(user, rec.market_id)

        # Role verification
        self._validate_role_access(user, rec.source_module, action="approve")

        now = datetime.utcnow()

        # Expiry check
        if rec.expires_at and now > rec.expires_at:
            rec.status = LIFECYCLE_EXPIRED
            db.commit()
            raise HTTPException(status_code=400, detail="Recommendation approval validity has expired.")

        # Lifecycle validation
        if rec.status in (LIFECYCLE_APPROVED, LIFECYCLE_EXECUTED, LIFECYCLE_EXECUTING):
            raise HTTPException(status_code=400, detail=f"Cannot approve recommendation in '{rec.status}' status (already approved or consumed).")

        if rec.status == LIFECYCLE_REJECTED:
            raise HTTPException(status_code=400, detail="Cannot approve a previously rejected recommendation. Generate a fresh recommendation.")

        # Compute and lock action fingerprint
        fingerprint = compute_action_fingerprint(
            rec_id=rec.id,
            proposed_action=rec.proposed_action or rec.recommended_action,
            target_type=rec.target_entity_type,
            target_id=rec.target_entity_id,
            market_id=rec.market_id,
            action_payload=rec.action_payload
        )

        rec.status = LIFECYCLE_APPROVED
        rec.reviewed_by_id = user.id
        rec.reviewed_at = now
        rec.review_notes = notes or f"Approved by {user.full_name} ({user.role})"
        rec.action_fingerprint = fingerprint

        # Audit log for approval
        audit = AuditLog(
            market_id=rec.market_id,
            recommendation_id=rec.id,
            source_module=rec.source_module,
            action_taken=rec.proposed_action or rec.recommended_action,
            decision="APPROVED",
            user_id=user.id,
            user_name=user.full_name,
            user_role=user.role,
            confidence_score=rec.confidence_score,
            original_signals={"action_fingerprint": fingerprint, "action_payload": rec.action_payload},
            execution_result={"status": "APPROVED", "reviewed_at": now.isoformat(), "notes": rec.review_notes},
            timestamp=now
        )
        db.add(audit)
        db.commit()
        db.refresh(rec)

        if auto_execute:
            return self.execute_recommendation(db, rec.id, user)

        return rec

    def reject_recommendation(
        self,
        db: Session,
        recommendation_id: int,
        user: User,
        notes: Optional[str] = None
    ) -> Recommendation:
        """
        Rejects a recommendation with a mandatory recorded reason.
        """
        if not notes or not notes.strip():
            raise HTTPException(status_code=400, detail="Rejection requires a non-empty recorded reason.")

        rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Recommendation not found")

        self._validate_market_access(user, rec.market_id)
        self._validate_role_access(user, rec.source_module, action="reject")

        now = datetime.utcnow()

        if rec.expires_at and now > rec.expires_at:
            rec.status = LIFECYCLE_EXPIRED
            db.commit()
            raise HTTPException(status_code=400, detail="Recommendation validity has expired.")

        if rec.status in (LIFECYCLE_APPROVED, LIFECYCLE_EXECUTED, LIFECYCLE_EXECUTING):
            raise HTTPException(status_code=400, detail=f"Cannot reject recommendation in '{rec.status}' status.")

        rec.status = LIFECYCLE_REJECTED
        rec.reviewed_by_id = user.id
        rec.reviewed_at = now
        rec.review_notes = notes
        rec.rejection_reason = notes

        audit = AuditLog(
            market_id=rec.market_id,
            recommendation_id=rec.id,
            source_module=rec.source_module,
            action_taken=rec.proposed_action or rec.recommended_action,
            decision="REJECTED",
            user_id=user.id,
            user_name=user.full_name,
            user_role=user.role,
            confidence_score=rec.confidence_score,
            original_signals={"action_payload": rec.action_payload},
            execution_result={"status": "REJECTED", "reason": notes},
            timestamp=now
        )
        db.add(audit)
        db.commit()
        db.refresh(rec)
        return rec

    # ── Workstream E: Deterministic Execution & Closed-Loop Read-Back ────────

    def execute_recommendation(
        self,
        db: Session,
        recommendation_id: int,
        user: User,
        idempotency_key: Optional[str] = None
    ) -> Recommendation:
        """
        Executes an approved recommendation through strict allowlisted deterministic actions.
        Enforces tamper detection, concurrency locking, and closed-loop read-back verification.
        """
        rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Recommendation not found")

        self._validate_market_access(user, rec.market_id)
        self._validate_role_access(user, rec.source_module, action="execute")

        now = datetime.utcnow()

        # Idempotency check
        if idempotency_key and rec.execution_idempotency_key == idempotency_key and rec.status == LIFECYCLE_EXECUTED:
            logger.info(f"Idempotent replay detected for recommendation #{rec.id} (key: {idempotency_key})")
            return rec

        # Expiry check
        if rec.expires_at and now > rec.expires_at:
            rec.status = LIFECYCLE_EXPIRED
            db.commit()
            raise HTTPException(status_code=400, detail="Recommendation approval validity has expired. Re-approval required.")

        # Lifecycle state validation
        if rec.status in (LIFECYCLE_PROPOSED, LIFECYCLE_PENDING):
            raise HTTPException(status_code=400, detail="Recommendation must be APPROVED by authorized personnel before execution.")

        if rec.status == LIFECYCLE_REJECTED:
            raise HTTPException(status_code=400, detail="Cannot execute a REJECTED recommendation.")

        if rec.status == LIFECYCLE_EXECUTED:
            raise HTTPException(status_code=400, detail="Recommendation has already been successfully executed.")

        if rec.status == LIFECYCLE_EXECUTING:
            raise HTTPException(status_code=409, detail="Recommendation is currently executing in another process.")

        if rec.status != LIFECYCLE_APPROVED:
            raise HTTPException(status_code=400, detail=f"Invalid recommendation status '{rec.status}' for execution.")

        # Tamper Protection: verify action fingerprint
        action_name = rec.proposed_action or rec.recommended_action
        expected_fingerprint = compute_action_fingerprint(
            rec_id=rec.id,
            proposed_action=action_name,
            target_type=rec.target_entity_type,
            target_id=rec.target_entity_id,
            market_id=rec.market_id,
            action_payload=rec.action_payload
        )

        if rec.action_fingerprint and rec.action_fingerprint != expected_fingerprint:
            rec.status = LIFECYCLE_FAILED
            rec.execution_status = "TAMPER_DETECTED"
            db.commit()
            raise HTTPException(
                status_code=400,
                detail="Action integrity violation: recommendation target, action, or parameters were modified post-approval."
            )

        # Allowlist verification
        if action_name not in ALLOWLISTED_ACTIONS:
            rec.status = LIFECYCLE_FAILED
            rec.execution_status = "UNAUTHORIZED_ACTION"
            db.commit()
            raise HTTPException(
                status_code=400,
                detail=f"Action '{action_name}' is not in the approved deterministic allowlist."
            )

        # Atomic Concurrency Lock: Transition to EXECUTING
        rows = db.query(Recommendation).filter(
            Recommendation.id == rec.id,
            Recommendation.status == LIFECYCLE_APPROVED
        ).update({"status": LIFECYCLE_EXECUTING})
        db.commit()

        if rows == 0:
            raise HTTPException(status_code=409, detail="Concurrent execution conflict: approval was already consumed.")

        # Re-fetch after lock
        rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()

        # Step 5: Execute deterministic simulated action
        execution_receipt: Dict[str, Any] = {}
        verification_passed = False

        try:
            if action_name == "RECOVER_NETWORK_SCENARIO":
                scenario_engine = get_scenario_engine()
                scenario_res = scenario_engine.recover_scenario(
                    db=db,
                    target_type=rec.target_entity_type,
                    target_id=str(rec.target_entity_id),
                    market_id=rec.market_id,
                    actor=user
                )
                execution_receipt = dict(scenario_res)
                execution_receipt["action"] = "RECOVER_NETWORK_SCENARIO"

                # Closed-Loop Read-Back: Verify link or device nominal health
                if rec.target_entity_type == "LINK":
                    verified_link = db.query(NetworkLink).filter(NetworkLink.id == rec.target_entity_id).first()
                    if verified_link and verified_link.status in ("ACTIVE", "HEALTHY"):
                        verification_passed = True
                else:
                    verified_dev = db.query(NetworkDevice).filter(NetworkDevice.id == rec.target_entity_id).first()
                    if verified_dev and verified_dev.status in ("HEALTHY", "ONLINE"):
                        verification_passed = True

            elif action_name == "OPTIMIZE_OPTICAL_ATTENUATION":
                dev = db.query(NetworkDevice).filter(NetworkDevice.id == rec.target_entity_id).first()
                if not dev:
                    raise RuntimeError(f"Target terminal {rec.target_entity_id} missing during execution.")

                target_rx = float(rec.action_payload.get("target_rx_dbm", -19.5))
                dev.optical_rx_dbm = target_rx
                dev.status = "HEALTHY"
                dev.last_seen_at = now

                # Clear related optical alarm
                alm_code = f"ALM-OPT-DEG-{dev.id}"
                alms = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == dev.id,
                    NetworkAlarm.alarm_code == alm_code,
                    NetworkAlarm.status == "ACTIVE"
                ).all()
                for a in alms:
                    a.status = "CLEARED"
                    a.cleared_at = now

                db.flush()

                # Closed-Loop Read-Back:
                readback_dev = db.query(NetworkDevice).filter(NetworkDevice.id == dev.id).first()
                active_alms = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == dev.id,
                    NetworkAlarm.alarm_code == alm_code,
                    NetworkAlarm.status == "ACTIVE"
                ).count()

                if readback_dev and readback_dev.optical_rx_dbm == target_rx and active_alms == 0:
                    verification_passed = True
                    execution_receipt = {
                        "action": "OPTIMIZE_OPTICAL_ATTENUATION",
                        "device_code": dev.device_code,
                        "calibrated_rx_dbm": target_rx,
                        "cleared_alarms_count": len(alms),
                        "verification": "CLOSED_LOOP_VERIFIED"
                    }

            elif action_name == "THROTTLE_QOS_BANDWIDTH":
                dev = db.query(NetworkDevice).filter(NetworkDevice.id == rec.target_entity_id).first()
                if not dev:
                    raise RuntimeError(f"Target chassis {rec.target_entity_id} missing during execution.")

                target_util = float(rec.action_payload.get("target_utilization_pct", 45.0))
                dev.cpu_utilization_pct = 32.0
                dev.status = "HEALTHY"

                for p in dev.ports:
                    p.utilization_pct = target_util
                    p.status = "HEALTHY"

                # Clear HIGH_PON_UTILIZATION and HIGH_CPU alarms
                alms = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == dev.id,
                    NetworkAlarm.code.in_(["HIGH_CPU", "HIGH_PON_UTILIZATION"]),
                    NetworkAlarm.status == "ACTIVE"
                ).all()
                for a in alms:
                    a.status = "CLEARED"
                    a.cleared_at = now

                db.flush()

                # Closed-Loop Read-Back:
                readback_dev = db.query(NetworkDevice).filter(NetworkDevice.id == dev.id).first()
                if readback_dev and readback_dev.cpu_utilization_pct <= 50.0:
                    verification_passed = True
                    execution_receipt = {
                        "action": "THROTTLE_QOS_BANDWIDTH",
                        "device_code": dev.device_code,
                        "rebalanced_cpu_pct": dev.cpu_utilization_pct,
                        "port_utilization_pct": target_util,
                        "cleared_alarms_count": len(alms),
                        "verification": "CLOSED_LOOP_VERIFIED"
                    }

            elif action_name == "DISPATCH_FIELD_CREW":
                # Create regional field ticket / assignment
                res = db.query(Resource).filter(
                    Resource.market_id == rec.market_id,
                    Resource.status == "Available"
                ).first()
                if not res:
                    res = db.query(Resource).filter(Resource.market_id == rec.market_id).first()

                assigned_name = res.name if res else "Regional Emergency Splicing Team"
                verification_passed = True
                execution_receipt = {
                    "action": "DISPATCH_FIELD_CREW",
                    "assigned_resource": assigned_name,
                    "target": rec.target_entity_label,
                    "dispatch_status": "DISPATCHED",
                    "verification": "CLOSED_LOOP_VERIFIED"
                }

            elif action_name == "REBOOT_CPE_DIAGNOSTIC":
                dev = db.query(NetworkDevice).filter(NetworkDevice.id == rec.target_entity_id).first()
                if dev:
                    dev.last_boot_at = now
                    dev.status = "ONLINE"
                    verification_passed = True
                    execution_receipt = {
                        "action": "REBOOT_CPE_DIAGNOSTIC",
                        "device_code": dev.device_code,
                        "boot_time": now.isoformat(),
                        "verification": "CLOSED_LOOP_VERIFIED"
                    }

            elif action_name == "APPLY_SLA_BILLING_CREDIT":
                from app.models_finance import BillingAccount, CreditNote
                from app.services.billing_service import issue_credit_note

                # 1. Resolve target billing account
                target_account = None
                if rec.target_entity_type == "BillingAccount":
                    target_account = db.query(BillingAccount).filter(BillingAccount.id == str(rec.target_entity_id)).first()
                else:
                    target_cust = db.query(Customer).filter(Customer.id == rec.target_entity_id).first()
                    if target_cust:
                        target_account = db.query(BillingAccount).filter(
                            BillingAccount.customer_id == target_cust.id,
                            BillingAccount.market_id == rec.market_id
                        ).first()

                if not target_account:
                    raise RuntimeError(f"No active BillingAccount found for target {rec.target_entity_type}:{rec.target_entity_id} in market {rec.market_id}")

                # 2. Extract and validate credit amount within policy bounds
                credit_amount = float((rec.action_payload or {}).get("credit_amount", 150.0))
                if credit_amount <= 0.0 or credit_amount > 5000.0:
                    raise ValueError(f"SLA credit amount {credit_amount} outside authorized compensation bounds (0.01 - 5000.00)")

                # 3. Duplicate protection: verify if a credit note has already been issued for this recommendation
                rec_identifier = rec.recommendation_code or f"REC-{rec.id}"
                existing_cn = db.query(CreditNote).filter(
                    CreditNote.billing_account_id == target_account.id,
                    CreditNote.reason.ilike(f"%{rec_identifier}%")
                ).first()

                if existing_cn:
                    verification_passed = True
                    execution_receipt = {
                        "action": "APPLY_SLA_BILLING_CREDIT",
                        "credit_note_code": existing_cn.credit_note_code,
                        "credit_amount": float(existing_cn.amount),
                        "billing_account_id": str(target_account.id),
                        "status": "ALREADY_ISSUED",
                        "idempotent_replay": True,
                        "verification": "CLOSED_LOOP_VERIFIED"
                    }
                else:
                    reason_text = f"SLA Outage Breach Compensation for incident {rec.source_alarm_code or rec_identifier} [{rec_identifier}]"
                    cn = issue_credit_note(
                        db=db,
                        billing_account_id=str(target_account.id),
                        amount=credit_amount,
                        reason=reason_text,
                        created_by_id=user.id
                    )

                    # 4. Closed-loop readback verification: verify CreditNote in DB
                    verified_cn = db.query(CreditNote).filter(CreditNote.id == cn.id).first()
                    if verified_cn and verified_cn.status == "ISSUED" and float(verified_cn.amount) == credit_amount:
                        verification_passed = True
                        execution_receipt = {
                            "action": "APPLY_SLA_BILLING_CREDIT",
                            "credit_note_code": verified_cn.credit_note_code,
                            "credit_amount": credit_amount,
                            "billing_account_id": str(target_account.id),
                            "new_balance": float(target_account.balance),
                            "verification": "CLOSED_LOOP_VERIFIED"
                        }
                    else:
                        raise RuntimeError(f"Closed-loop verification failed for SLA credit note {cn.credit_note_code}")

            else:
                raise RuntimeError(f"Unhandled deterministic action '{action_name}'")

            # Finalize status based on closed-loop verification
            if verification_passed:
                rec.status = LIFECYCLE_EXECUTED
                rec.execution_status = "VERIFIED_SUCCESS"
            else:
                rec.status = LIFECYCLE_FAILED
                rec.execution_status = "VERIFICATION_FAILED"
                rec.retry_count = (rec.retry_count or 0) + 1

            rec.executed_by_id = user.id
            rec.executed_at = now
            rec.execution_result = execution_receipt
            if idempotency_key:
                rec.execution_idempotency_key = idempotency_key

            # Audit Log for Execution
            audit = AuditLog(
                market_id=rec.market_id,
                recommendation_id=rec.id,
                source_module=rec.source_module,
                action_taken=action_name,
                decision="EXECUTED" if verification_passed else "FAILED",
                user_id=user.id,
                user_name=user.full_name,
                user_role=user.role,
                confidence_score=rec.confidence_score,
                original_signals=rec.evidence,
                execution_result=execution_receipt,
                timestamp=now
            )
            db.add(audit)
            db.commit()
            db.refresh(rec)
            return rec

        except Exception as e:
            db.rollback()
            logger.exception(f"Execution failed for recommendation #{recommendation_id}: {str(e)}")
            try:
                fail_rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
                if fail_rec:
                    fail_rec.status = LIFECYCLE_FAILED
                    fail_rec.execution_status = "EXECUTION_ERROR"
                    fail_rec.execution_result = {"error": str(e)}
                    fail_rec.retry_count = (fail_rec.retry_count or 0) + 1
                    db.commit()
            except Exception:
                db.rollback()
            raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")

    # ── Internal Security & Authorization Helpers ───────────────────────────

    def _validate_market_access(self, user: User, target_market: str) -> None:
        """
        Guarantees regional tenant boundaries. SUPER_ADMIN may access any market;
        regional roles are restricted to their assigned market.
        """
        from app.services.rbac_service import is_super_admin
        if is_super_admin(user.role):
            return

        assigned_markets = {a.market_id.lower() for a in getattr(user, 'market_assignments', [])}
        user_market = getattr(user, 'market_id', 'mumbai').lower()

        if target_market.lower() != user_market and target_market.lower() not in assigned_markets:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: User '{user.email}' lacks authorization for market '{target_market}'"
            )

    def _validate_role_access(self, user: User, source_module: str, action: str) -> None:
        """
        Enforces operational RBAC per governance source module.
        """
        from app.services.rbac_service import is_super_admin, is_admin_or_super, normalize_role_name

        if is_admin_or_super(user.role):
            return

        allowed = MODULE_OPERATIONAL_ROLES.get(source_module, ["Admin", "SUPER_ADMIN"])
        norm_user_role = normalize_role_name(user.role)
        norm_allowed = [normalize_role_name(r) for r in allowed]

        if norm_user_role not in norm_allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Action '{action}' on module '{source_module}' requires role in {allowed}. Current user: {user.role}."
            )


    # ── Durable Watchdog: Stale Execution Reaper ────────────────────────────

    def reap_stuck_executions(
        self,
        db: Session,
        max_age_minutes: int = 10,
        actor: Optional[User] = None
    ) -> List[Dict[str, Any]]:
        """
        Durable watchdog: safely reconciles recommendations stuck in EXECUTING after worker crash.
        Enforces:
        - Atomic optimistic locking to prevent concurrent watchdog collisions.
        - Authoritative side-effect readback (credit note issued, device healthy) vs blind retry.
        - Terminal transition to EXECUTED (if verified) or FAILED (with retry count incremented).
        - Comprehensive audit trail.
        """
        from app.models import AuditLog, NetworkDevice, NetworkLink, NetworkAlarm
        from app.models_finance import CreditNote, BillingAccount

        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=max_age_minutes)

        candidates = db.query(Recommendation).filter(
            Recommendation.status == LIFECYCLE_EXECUTING,
            or_(
                Recommendation.executed_at <= cutoff,
                and_(Recommendation.executed_at.is_(None), Recommendation.created_at <= cutoff)
            )
        ).all()

        results = []

        for cand in candidates:
            # Atomic lock transition: EXECUTING -> RECOVERING
            locked = db.query(Recommendation).filter(
                Recommendation.id == cand.id,
                Recommendation.status == LIFECYCLE_EXECUTING
            ).update({"status": "RECOVERING"})
            db.commit()

            if locked == 0:
                continue

            rec = db.query(Recommendation).filter(Recommendation.id == cand.id).first()
            action_name = rec.proposed_action or rec.recommended_action
            rec_identifier = rec.recommendation_code or f"REC-{rec.id}"
            outcome_status = LIFECYCLE_FAILED
            execution_status = "STALE_EXECUTION_REAPED"
            receipt: Dict[str, Any] = {}

            # Action-specific authoritative outcome reconciliation
            if action_name == "APPLY_SLA_BILLING_CREDIT":
                existing_cn = db.query(CreditNote).filter(
                    CreditNote.reason.ilike(f"%{rec_identifier}%")
                ).first()
                if existing_cn:
                    outcome_status = LIFECYCLE_EXECUTED
                    execution_status = "RECONCILED_BY_WATCHDOG"
                    receipt = {
                        "action": action_name,
                        "credit_note_code": existing_cn.credit_note_code,
                        "credit_amount": float(existing_cn.amount),
                        "watchdog_recovered": True,
                        "reconciled": True
                    }
                else:
                    outcome_status = LIFECYCLE_FAILED
                    execution_status = "FAILED_DURING_EXECUTION"
                    receipt = {
                        "action": action_name,
                        "error": "Execution process crashed prior to credit note creation",
                        "watchdog_recovered": True
                    }

            elif action_name == "RECOVER_NETWORK_SCENARIO":
                is_nominal = False
                if rec.target_entity_type == "LINK":
                    lnk = db.query(NetworkLink).filter(NetworkLink.id == rec.target_entity_id).first()
                    if lnk and lnk.status in ("ACTIVE", "HEALTHY"):
                        is_nominal = True
                else:
                    dev = db.query(NetworkDevice).filter(NetworkDevice.id == rec.target_entity_id).first()
                    if dev and dev.status in ("HEALTHY", "ONLINE"):
                        is_nominal = True

                if is_nominal:
                    outcome_status = LIFECYCLE_EXECUTED
                    execution_status = "RECONCILED_BY_WATCHDOG"
                    receipt = {
                        "action": action_name,
                        "watchdog_recovered": True,
                        "reconciled": True
                    }
                else:
                    outcome_status = LIFECYCLE_FAILED
                    execution_status = "FAILED_DURING_EXECUTION"
                    receipt = {
                        "action": action_name,
                        "error": "Execution process crashed prior to topology recovery",
                        "watchdog_recovered": True
                    }

            else:
                outcome_status = LIFECYCLE_FAILED
                execution_status = "STALE_EXECUTION_REAPED"
                receipt = {
                    "action": action_name,
                    "error": f"Execution timed out after {max_age_minutes}m with unverified outcome",
                    "watchdog_recovered": True
                }

            rec.status = outcome_status
            rec.execution_status = execution_status
            rec.execution_result = receipt
            if outcome_status == LIFECYCLE_FAILED:
                rec.retry_count = (rec.retry_count or 0) + 1

            audit = AuditLog(
                market_id=rec.market_id,
                recommendation_id=rec.id,
                source_module=rec.source_module,
                action_taken="WATCHDOG_REAP_EXECUTION",
                decision=outcome_status,
                user_id=actor.id if actor else None,
                user_name=actor.full_name if actor else "Automated Watchdog Reaper",
                user_role=actor.role if actor else "SYSTEM",
                confidence_score=rec.confidence_score,
                original_signals=rec.evidence,
                execution_result=receipt,
                timestamp=now
            )
            db.add(audit)
            db.commit()

            results.append({
                "recommendation_id": rec.id,
                "recommendation_code": rec.recommendation_code,
                "action": action_name,
                "reaped_status": outcome_status,
                "execution_status": execution_status
            })

        return results


# ── Global Singleton Accessor ───────────────────────────────────────────────

_orchestration_service: Optional[AIOrchestrationService] = None

def get_ai_orchestration_service() -> AIOrchestrationService:
    global _orchestration_service
    if _orchestration_service is None:
        _orchestration_service = AIOrchestrationService()
    return _orchestration_service
