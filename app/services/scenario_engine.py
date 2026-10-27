"""
Phase 9 / Gate 3: Deterministic Synthetic Network Fault Scenario Engine

Architecture:
- Provides deterministic multi-hop fault simulation across access network topologies:
  1. FIBER_CUT: Sever links, cascade downstream outage/LOS to subtending PON ports and ONTs.
  2. OPTICAL_DEGRADE: Introduce optical power attenuation across ports or device branches.
  3. PEAK_TRAFFIC: Inject traffic surges, PON capacity saturation, and thermal/CPU load.
  4. RECOVERY: Restore affected links, devices, and ports to baseline nominal states and auto-clear alarms.
- Strict market/tenant isolation: cross-market targets are rejected.
- RBAC enforcement: restricted to authorized operational personas (SUPER_ADMIN, Admin, NOC).
- Deterministic behavior: repeatable outcomes given identical parameters/seed.
- Explicit simulation provenance: responses clearly labeled 'Synthetic / Demo Telemetry'.
- Auditability: records execution, parameters, outcomes, and actors in audit_logs.
- Non-destructive: preserves historical alarm records (status -> CLEARED, never deleted).
"""

import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Set, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.models import (
    NetworkDevice, OLTPort, NetworkLink, NetworkAlarm,
    Customer, User, AuditLog, NetworkScenario
)
from app.services.network_provider import get_network_provider
from app.services.monitoring_provider import get_monitoring_provider


# ── Canonical Constants ────────────────────────────────────────────────────────

SCENARIO_FIBER_CUT = "FIBER_CUT"
SCENARIO_OPTICAL_DEGRADE = "OPTICAL_DEGRADE"
SCENARIO_PEAK_TRAFFIC = "PEAK_TRAFFIC"
SCENARIO_RECOVERY = "RECOVERY"

VALID_SCENARIOS = {
    SCENARIO_FIBER_CUT,
    SCENARIO_OPTICAL_DEGRADE,
    SCENARIO_PEAK_TRAFFIC,
    SCENARIO_RECOVERY,
}

TARGET_TYPE_LINK = "LINK"
TARGET_TYPE_DEVICE = "DEVICE"
TARGET_TYPE_PORT = "PORT"

VALID_TARGET_TYPES = {
    TARGET_TYPE_LINK,
    TARGET_TYPE_DEVICE,
    TARGET_TYPE_PORT,
}


def _sanitize_for_json(val: Any) -> Any:
    """Recursively convert datetimes and non-string keys to JSON-serializable primitives."""
    if isinstance(val, dict):
        return {str(k): _sanitize_for_json(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple, set)):
        return [_sanitize_for_json(x) for x in val]
    elif isinstance(val, datetime):
        return val.isoformat()
    return val


class NetworkScenarioEngine:
    """
    Authoritative scenario execution engine for deterministic synthetic network faults.
    """

    TELEMETRY_SOURCE = "Synthetic / Demo Telemetry"

    def __init__(self):
        # In-memory registry of active scenarios for quick lookup & idempotency
        self._active_scenarios: Dict[str, Dict[str, Any]] = {}
        self._idempotency_registry: Dict[str, str] = {}

    def _scenario_to_dict(self, scen: NetworkScenario) -> Dict[str, Any]:
        """Convert a persisted NetworkScenario record to clean client response format."""
        return {
            "scenario_id": scen.scenario_id,
            "scenario_type": scen.scenario_type,
            "target_type": scen.target_type,
            "target_id": str(scen.target_id),
            "market_id": scen.market_id,
            "severity": scen.severity,
            "status": scen.status,
            "created_at": scen.created_at.isoformat() if scen.created_at else None,
            "recovered_at": scen.recovered_at.isoformat() if scen.recovered_at else None,
            "affected_links_count": scen.affected_links_count,
            "affected_devices_count": scen.affected_devices_count,
            "affected_ports_count": scen.affected_ports_count,
            "affected_customers_count": scen.affected_customers_count,
            "alarms_generated_count": scen.alarms_generated_count,
            "alarms_generated": scen.alarms_generated or [],
            "summary": scen.summary or "",
            "simulation": True,
            "telemetry_source": self.TELEMETRY_SOURCE
        }


    # ── Helper: Resolve & Validate Target ──────────────────────────────────────

    def _resolve_target(
        self,
        db: Session,
        target_type: str,
        target_id: str,
        market_id: str
    ) -> Tuple[Any, str]:
        """
        Locates target entity, verifies existence and enforces market isolation.
        Returns: (entity_instance, canonical_market_id)
        Raises:
            ValueError: if target does not exist or target_type is invalid.
            PermissionError: if target belongs to a different regional market.
        """
        tt = target_type.upper().strip()
        if tt not in VALID_TARGET_TYPES:
            raise ValueError(f"Invalid target_type '{target_type}'. Valid types: {sorted(VALID_TARGET_TYPES)}")

        if tt == TARGET_TYPE_LINK:
            query = db.query(NetworkLink)
            if target_id.isdigit():
                link = query.filter(NetworkLink.id == int(target_id)).first()
            else:
                # Resolve link by source or target device code
                dev = db.query(NetworkDevice).filter(NetworkDevice.device_code == target_id).first()
                if dev:
                    link = query.filter(
                        or_(NetworkLink.source_device_id == dev.id, NetworkLink.target_device_id == dev.id)
                    ).first()
                else:
                    link = None

            if not link:
                raise ValueError(f"NetworkLink '{target_id}' not found.")
            if link.market_id != market_id:
                raise PermissionError(f"Forbidden: Target link #{link.id} belongs to another regional market ('{link.market_id}').")
            return link, link.market_id

        elif tt == TARGET_TYPE_DEVICE:
            query = db.query(NetworkDevice)
            if target_id.isdigit():
                dev = query.filter(NetworkDevice.id == int(target_id)).first()
            else:
                dev = query.filter(NetworkDevice.device_code == target_id).first()

            if not dev:
                raise ValueError(f"NetworkDevice '{target_id}' not found.")
            if dev.market_id != market_id:
                raise PermissionError(f"Forbidden: Target device '{dev.device_code}' belongs to another regional market ('{dev.market_id}').")
            return dev, dev.market_id

        elif tt == TARGET_TYPE_PORT:
            query = db.query(OLTPort)
            if target_id.isdigit():
                port = query.filter(OLTPort.id == int(target_id)).first()
            elif ":" in target_id or "/" in target_id:
                parts = target_id.split(":", 1)
                if len(parts) == 2:
                    dev_code, p_num = parts
                    dev = db.query(NetworkDevice).filter(NetworkDevice.device_code == dev_code).first()
                    port = query.filter(OLTPort.device_id == dev.id, OLTPort.port_number == p_num).first() if dev else None
                else:
                    port = query.filter(OLTPort.port_number == target_id).first()
            else:
                port = query.filter(OLTPort.port_number == target_id).first()

            if not port:
                raise ValueError(f"OLTPort '{target_id}' not found.")
            port_market = port.device.market_id if port.device else None
            if port_market and port_market != market_id:
                raise PermissionError(f"Forbidden: Target port #{port.id} belongs to another regional market ('{port_market}').")
            return port, port_market

        raise ValueError(f"Unsupported target type: {target_type}")

    # ── Helper: Downstream Topology Traversal ──────────────────────────────────

    def _get_downstream_subtree(
        self,
        db: Session,
        root_device_id: int,
        market_id: str
    ) -> List[NetworkDevice]:
        """
        Breadth-first traversal down parent_id hierarchy.
        Returns all strictly subordinate devices (Splitters, ONTs, ONUs).
        """
        visited_ids: Set[int] = {root_device_id}
        queue: List[int] = [root_device_id]
        descendants: List[NetworkDevice] = []

        while queue:
            curr_parent = queue.pop(0)
            children = db.query(NetworkDevice).filter(
                NetworkDevice.parent_id == curr_parent,
                NetworkDevice.market_id == market_id
            ).all()
            for ch in children:
                if ch.id not in visited_ids:
                    visited_ids.add(ch.id)
                    queue.append(ch.id)
                    descendants.append(ch)

        return descendants

    def _get_downstream_links(
        self,
        db: Session,
        device_ids: Set[int],
        market_id: str
    ) -> List[NetworkLink]:
        """Collect all links where target or source is within the device_ids set."""
        if not device_ids:
            return []
        return db.query(NetworkLink).filter(
            NetworkLink.market_id == market_id,
            or_(
                NetworkLink.source_device_id.in_(device_ids),
                NetworkLink.target_device_id.in_(device_ids)
            )
        ).all()

    def _get_affected_customers(
        self,
        db: Session,
        devices: List[NetworkDevice]
    ) -> List[Dict[str, Any]]:
        """Collect active customers linked directly to any affected terminal."""
        customer_ids = [d.customer_id for d in devices if d.customer_id]
        if not customer_ids:
            return []
        custs = db.query(Customer).filter(Customer.id.in_(customer_ids)).all()
        return [
            {
                "id": c.id,
                "customer_code": c.customer_code,
                "name": c.name,
                "locality": c.locality,
                "status": c.status,
                "market_id": c.market_id
            }
            for c in custs
        ]

    # ── Helper: Generate Unique Scenario ID ───────────────────────────────────

    def _generate_scenario_id(self, market_id: str, scenario_type: str, seed: Optional[int] = None) -> str:
        """Deterministic or timestamp-seeded traceable scenario ID."""
        ts = int(time.time())
        if seed is not None:
            token = f"{seed:06x}"[:6]
        else:
            token = uuid.uuid4().hex[:6]
        return f"SCEN-{market_id[:3].upper()}-{scenario_type[:4]}-{ts}-{token}"

    # ── Scenario 1: FIBER_CUT ─────────────────────────────────────────────────

    def _execute_fiber_cut(
        self,
        db: Session,
        target_entity: Any,
        target_type: str,
        market_id: str,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Simulate physical fiber cut:
        - Target link marked DOWN.
        - Downstream devices transition to DOWN.
        - Downstream ONTs experience complete optical loss (Rx = -40.0 dBm).
        - LOS and FIBER_CUT alarms generated.
        - Unrelated branches remain untouched.
        """
        snapshot = {"links": {}, "devices": {}, "ports": {}}

        if target_type == TARGET_TYPE_LINK:
            cut_link = target_entity
            affected_links = [cut_link]
            root_device = db.query(NetworkDevice).filter(NetworkDevice.id == cut_link.target_device_id).first()
            descendants = self._get_downstream_subtree(db, root_device.id, market_id) if root_device else []
            affected_devices = ([root_device] if root_device else []) + descendants
        elif target_type == TARGET_TYPE_DEVICE:
            root_device = target_entity
            descendants = self._get_downstream_subtree(db, root_device.id, market_id)
            affected_devices = [root_device] + descendants
            dev_ids = {d.id for d in affected_devices}
            affected_links = self._get_downstream_links(db, dev_ids, market_id)
        else:
            port = target_entity
            olt = port.device
            descendants = self._get_downstream_subtree(db, olt.id, market_id)
            affected_devices = descendants
            affected_links = self._get_downstream_links(db, {d.id for d in affected_devices}, market_id)

        # 1. Update links & snapshot
        for l in affected_links:
            snapshot["links"][l.id] = {"status": l.status}
            l.status = "DOWN"

        # 2. Update devices & snapshot
        generated_alarms = []
        now = datetime.utcnow()

        for d in affected_devices:
            snapshot["devices"][d.id] = {
                "status": d.status,
                "optical_rx_dbm": d.optical_rx_dbm,
                "cpu_utilization_pct": d.cpu_utilization_pct,
                "last_seen_at": d.last_seen_at
            }
            d.status = "DOWN"

            if d.device_type in ("ONT", "ONU"):
                d.optical_rx_dbm = -40.0
                d.last_seen_at = now - timedelta(minutes=15)

                alm_code = f"ALM-LOS-{d.id}"
                existing_alm = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == d.id,
                    NetworkAlarm.alarm_code == alm_code
                ).first()
                if existing_alm:
                    existing_alm.status = "ACTIVE"
                    existing_alm.severity = "CRITICAL"
                    existing_alm.last_seen_at = now
                    existing_alm.cleared_at = None
                    existing_alm.message = f"Loss of optical signal (LOS) on {d.device_code}: upstream fiber cut"
                    generated_alarms.append(existing_alm)
                else:
                    new_alm = NetworkAlarm(
                        alarm_code=alm_code,
                        market_id=market_id,
                        device_id=d.id,
                        severity="CRITICAL",
                        code="LOSS_OF_SIGNAL",
                        message=f"Loss of optical signal (LOS) on {d.device_code}: upstream fiber cut",
                        status="ACTIVE",
                        first_seen_at=now,
                        last_seen_at=now
                    )
                    db.add(new_alm)
                    generated_alarms.append(new_alm)
            else:
                alm_code = f"ALM-FIBER-CUT-{d.id}"
                existing_alm = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == d.id,
                    NetworkAlarm.alarm_code == alm_code
                ).first()
                if existing_alm:
                    existing_alm.status = "ACTIVE"
                    existing_alm.severity = "CRITICAL"
                    existing_alm.last_seen_at = now
                    existing_alm.cleared_at = None
                    existing_alm.message = f"Fiber link severed: Optical distribution path down at {d.device_code} ({d.device_type})"
                    generated_alarms.append(existing_alm)
                else:
                    new_alm = NetworkAlarm(
                        alarm_code=alm_code,
                        market_id=market_id,
                        device_id=d.id,
                        severity="CRITICAL",
                        code="FIBER_CUT",
                        message=f"Fiber link severed: Optical distribution path down at {d.device_code} ({d.device_type})",
                        status="ACTIVE",
                        first_seen_at=now,
                        last_seen_at=now
                    )
                    db.add(new_alm)
                    generated_alarms.append(new_alm)

        affected_customers = self._get_affected_customers(db, affected_devices)

        return {
            "snapshot": snapshot,
            "affected_links": [l.id for l in affected_links],
            "affected_devices": [d.id for d in affected_devices],
            "affected_customers": affected_customers,
            "generated_alarms": [a.alarm_code for a in generated_alarms],
            "summary": f"FIBER_CUT propagated: {len(affected_links)} link(s) DOWN, {len(affected_devices)} device(s) DOWN, {len(affected_customers)} customer(s) impacted."
        }

    # ── Scenario 2: OPTICAL_DEGRADE ───────────────────────────────────────────

    def _execute_optical_degrade(
        self,
        db: Session,
        target_entity: Any,
        target_type: str,
        market_id: str,
        severity: str,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Simulate optical degradation (attenuation / macro-bending / dirty fiber):
        - Applies repository's optical thresholds (-26 dBm Warning, -28 dBm Critical).
        - Propagates degraded Rx power to subtending ONTs.
        - Generates deduplicated OPTICAL_DEGRADATION alarms.
        """
        snapshot = {"links": {}, "devices": {}, "ports": {}}

        custom_rx = parameters.get("rx_dbm")
        if custom_rx is not None:
            target_rx = float(custom_rx)
        elif severity.upper() == "WARNING":
            target_rx = -27.0
        else:
            target_rx = -29.5

        if target_type == TARGET_TYPE_DEVICE:
            dev = target_entity
            if dev.device_type in ("ONT", "ONU"):
                affected_onts = [dev]
            else:
                descendants = self._get_downstream_subtree(db, dev.id, market_id)
                affected_onts = [d for d in descendants if d.device_type in ("ONT", "ONU")]
        elif target_type == TARGET_TYPE_PORT:
            port = target_entity
            olt = port.device
            descendants = self._get_downstream_subtree(db, olt.id, market_id)
            affected_onts = [d for d in descendants if d.device_type in ("ONT", "ONU")]
        else:
            link = target_entity
            root_dev = db.query(NetworkDevice).filter(NetworkDevice.id == link.target_device_id).first()
            if root_dev:
                descendants = self._get_downstream_subtree(db, root_dev.id, market_id)
                affected_onts = [d for d in [root_dev] + descendants if d.device_type in ("ONT", "ONU")]
            else:
                affected_onts = []

        now = datetime.utcnow()
        generated_alarms = []
        sev_label = "CRITICAL" if target_rx <= -28.0 else "WARNING"
        status_label = "DEGRADED" if target_rx <= -28.0 else "WARNING"

        for ont in affected_onts:
            snapshot["devices"][ont.id] = {
                "status": ont.status,
                "optical_rx_dbm": ont.optical_rx_dbm,
                "last_seen_at": ont.last_seen_at
            }
            ont.optical_rx_dbm = target_rx
            ont.status = status_label
            ont.last_seen_at = now

            alm_code = f"ALM-OPT-DEG-{ont.id}"
            existing_alm = db.query(NetworkAlarm).filter(
                NetworkAlarm.device_id == ont.id,
                NetworkAlarm.alarm_code == alm_code
            ).first()

            msg = f"Optical power degraded to {target_rx} dBm on {ont.device_code} (excess attenuation)"
            if existing_alm:
                existing_alm.status = "ACTIVE"
                existing_alm.severity = sev_label
                existing_alm.last_seen_at = now
                existing_alm.cleared_at = None
                existing_alm.message = msg
                generated_alarms.append(existing_alm)
            else:
                new_alm = NetworkAlarm(
                    alarm_code=alm_code,
                    market_id=market_id,
                    device_id=ont.id,
                    severity=sev_label,
                    code="OPTICAL_DEGRADATION",
                    message=msg,
                    status="ACTIVE",
                    first_seen_at=now,
                    last_seen_at=now
                )
                db.add(new_alm)
                generated_alarms.append(new_alm)

        affected_customers = self._get_affected_customers(db, affected_onts)

        return {
            "snapshot": snapshot,
            "target_rx_dbm": target_rx,
            "affected_links": [],
            "affected_devices": [d.id for d in affected_onts],
            "affected_customers": affected_customers,
            "generated_alarms": [a.alarm_code for a in generated_alarms],
            "summary": f"OPTICAL_DEGRADE applied: {len(affected_onts)} ONT(s) degraded to {target_rx} dBm ({sev_label}), {len(affected_customers)} customer(s) impacted."
        }

    # ── Scenario 3: PEAK_TRAFFIC ──────────────────────────────────────────────

    def _execute_peak_traffic(
        self,
        db: Session,
        target_entity: Any,
        target_type: str,
        market_id: str,
        severity: str,
        parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Simulate traffic surge / saturation:
        - Increases PON port utilization and chassis CPU/temperature.
        - Evaluates configured thresholds (75% warning, 90% critical).
        - Generates HIGH_PON_UTILIZATION / HIGH_CPU alarms.
        """
        snapshot = {"links": {}, "devices": {}, "ports": {}}

        custom_util = parameters.get("utilization_pct")
        if custom_util is not None:
            target_util = float(custom_util)
        elif severity.upper() == "WARNING":
            target_util = 82.0
        else:
            target_util = 94.0

        now = datetime.utcnow()
        affected_ports = []
        affected_devices = []
        generated_alarms = []

        if target_type == TARGET_TYPE_PORT:
            port = target_entity
            olt = port.device
            affected_ports.append(port)
            if olt:
                affected_devices.append(olt)
        elif target_type == TARGET_TYPE_DEVICE:
            dev = target_entity
            affected_devices.append(dev)
            if dev.ports:
                affected_ports.extend(dev.ports)
        else:
            link = target_entity
            s_dev = db.query(NetworkDevice).filter(NetworkDevice.id == link.source_device_id).first()
            if s_dev and s_dev.ports:
                affected_ports.extend(s_dev.ports)
                affected_devices.append(s_dev)

        sev_label = "CRITICAL" if target_util >= 90.0 else "WARNING"
        status_label = "CRITICAL" if target_util >= 90.0 else "WARNING"

        # Update ports
        for p in affected_ports:
            snapshot["ports"][p.id] = {
                "status": p.status,
                "utilization_pct": p.utilization_pct,
                "last_updated_at": p.last_updated_at
            }
            p.utilization_pct = target_util
            p.status = status_label
            p.last_updated_at = now

            if p.device:
                alm_code = f"ALM-PON-UTIL-{p.id}"
                existing_alm = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == p.device.id,
                    NetworkAlarm.alarm_code == alm_code
                ).first()
                msg = f"PON port {p.port_number} throughput reached {target_util}% ({sev_label} threshold)"
                if existing_alm:
                    existing_alm.status = "ACTIVE"
                    existing_alm.severity = sev_label
                    existing_alm.last_seen_at = now
                    existing_alm.cleared_at = None
                    existing_alm.message = msg
                    generated_alarms.append(existing_alm)
                else:
                    new_alm = NetworkAlarm(
                        alarm_code=alm_code,
                        market_id=market_id,
                        device_id=p.device.id,
                        severity=sev_label,
                        code="HIGH_PON_UTILIZATION",
                        message=msg,
                        status="ACTIVE",
                        first_seen_at=now,
                        last_seen_at=now
                    )
                    db.add(new_alm)
                    generated_alarms.append(new_alm)

        # Update OLT devices
        for d in affected_devices:
            snapshot["devices"][d.id] = {
                "status": d.status,
                "cpu_utilization_pct": d.cpu_utilization_pct,
                "temperature_c": d.temperature_c,
                "last_seen_at": d.last_seen_at
            }
            d.cpu_utilization_pct = min(98.0, round(target_util * 0.95, 1))
            d.temperature_c = round((d.temperature_c or 40.0) + (8.0 if sev_label == "CRITICAL" else 4.0), 1)
            d.status = status_label
            d.last_seen_at = now

            alm_code = f"ALM-CPU-SPIKE-{d.id}"
            existing_alm = db.query(NetworkAlarm).filter(
                NetworkAlarm.device_id == d.id,
                NetworkAlarm.alarm_code == alm_code
            ).first()
            msg = f"High CPU utilization ({d.cpu_utilization_pct}%) on OLT {d.device_code} during peak traffic surge"
            if existing_alm:
                existing_alm.status = "ACTIVE"
                existing_alm.severity = sev_label
                existing_alm.last_seen_at = now
                existing_alm.cleared_at = None
                existing_alm.message = msg
                generated_alarms.append(existing_alm)
            else:
                new_alm = NetworkAlarm(
                    alarm_code=alm_code,
                    market_id=market_id,
                    device_id=d.id,
                    severity=sev_label,
                    code="HIGH_CPU",
                    message=msg,
                    status="ACTIVE",
                    first_seen_at=now,
                    last_seen_at=now
                )
                db.add(new_alm)
                generated_alarms.append(new_alm)

        return {
            "snapshot": snapshot,
            "target_utilization_pct": target_util,
            "affected_links": [],
            "affected_ports": [p.id for p in affected_ports],
            "affected_devices": [d.id for d in affected_devices],
            "affected_customers": [],
            "generated_alarms": [a.alarm_code for a in generated_alarms],
            "summary": f"PEAK_TRAFFIC applied: {len(affected_ports)} port(s) set to {target_util}%, {len(affected_devices)} device(s) under {sev_label} load."
        }

    # ── Scenario 4: RECOVERY ──────────────────────────────────────────────────

    def _execute_recovery(
        self,
        db: Session,
        scenario_id: Optional[str],
        target_entity: Optional[Any],
        target_type: Optional[str],
        market_id: str,
        target_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Recover synthetic fault states:
        - Restores links, devices, and ports to snapshot states or nominal baseline.
        - Resolves active alarms (status -> CLEARED, cleared_at -> now).
        - Preserves historical alarm records.
        """
        now = datetime.utcnow()
        recovered_links = 0
        recovered_devices = 0
        recovered_ports = 0
        cleared_alarms = 0

        # Case A: Recover specific scenario from persistent DB snapshot or memory
        db_scen = None
        if scenario_id:
            db_scen = db.query(NetworkScenario).filter(NetworkScenario.scenario_id == scenario_id).first()

        if db_scen or (scenario_id and scenario_id in self._active_scenarios):
            scen_info = self._active_scenarios.get(scenario_id, {})
            snapshot = (db_scen.snapshot if db_scen else scen_info.get("snapshot", {})) or {}
            target_market = db_scen.market_id if db_scen else scen_info.get("market_id", market_id)

            if target_market != market_id:
                raise PermissionError(f"Cross-market recovery denied: scenario '{scenario_id}' belongs to market '{target_market}', caller is in '{market_id}'.")

            if db_scen and db_scen.status == "RECOVERED":
                return {
                    "scenario_type": SCENARIO_RECOVERY,
                    "status": "RECOVERED",
                    "scenario_id": db_scen.scenario_id,
                    "idempotent_replay": True,
                    "recovered_links_count": 0,
                    "recovered_devices_count": 0,
                    "recovered_ports_count": 0,
                    "cleared_alarms_count": 0,
                    "summary": f"Scenario {scenario_id} already recovered."
                }

            # Optimistic concurrency lock: transition ACTIVE -> RECOVERING
            if db_scen:
                locked = db.query(NetworkScenario).filter(
                    NetworkScenario.id == db_scen.id,
                    NetworkScenario.status == "ACTIVE"
                ).update({"status": "RECOVERING"})
                db.flush()
                if locked == 0 and db_scen.status != "ACTIVE":
                    return {
                        "scenario_type": SCENARIO_RECOVERY,
                        "status": db_scen.status,
                        "scenario_id": db_scen.scenario_id,
                        "idempotent_replay": True,
                        "summary": f"Scenario {scenario_id} is already in state {db_scen.status}."
                    }

            # Safeguard: Check other ACTIVE scenarios in this market to avoid clearing overlapping faults
            other_active = db.query(NetworkScenario).filter(
                NetworkScenario.status.in_(["ACTIVE", "RECOVERING"]),
                NetworkScenario.scenario_id != scenario_id,
                NetworkScenario.market_id == target_market
            ).all()

            other_links = {int(lid) for o in other_active for lid in ((o.snapshot or {}).get("links", {})).keys() if str(lid).isdigit()}
            other_devs = {int(did) for o in other_active for did in ((o.snapshot or {}).get("devices", {})).keys() if str(did).isdigit()}
            other_ports = {int(pid) for o in other_active for pid in ((o.snapshot or {}).get("ports", {})).keys() if str(pid).isdigit()}
            other_alarms = {ac for o in other_active for ac in (o.alarms_generated or [])}

            # Restore links (preserving still-active faults)
            for l_id, s_data in snapshot.get("links", {}).items():
                if int(l_id) not in other_links:
                    l = db.query(NetworkLink).filter(NetworkLink.id == int(l_id)).first()
                    if l:
                        l.status = s_data.get("status", "ACTIVE")
                        recovered_links += 1

            # Restore devices (preserving still-active faults)
            for d_id, s_data in snapshot.get("devices", {}).items():
                if int(d_id) not in other_devs:
                    d = db.query(NetworkDevice).filter(NetworkDevice.id == int(d_id)).first()
                    if d:
                        d.status = s_data.get("status", "HEALTHY")
                        d.optical_rx_dbm = s_data.get("optical_rx_dbm", -19.5 if d.device_type in ("ONT", "ONU") else None)
                        d.cpu_utilization_pct = s_data.get("cpu_utilization_pct", 42.0 if d.device_type == "OLT" else None)
                        d.temperature_c = s_data.get("temperature_c", 40.0)
                        d.last_seen_at = now
                        recovered_devices += 1

            # Restore ports (preserving still-active faults)
            for p_id, s_data in snapshot.get("ports", {}).items():
                if int(p_id) not in other_ports:
                    p = db.query(OLTPort).filter(OLTPort.id == int(p_id)).first()
                    if p:
                        p.status = s_data.get("status", "HEALTHY")
                        p.utilization_pct = s_data.get("utilization_pct", 45.0)
                        p.last_updated_at = now
                        recovered_ports += 1

            # Clear generated alarms (preserving still-active incident alarms)
            alarm_codes_to_clear = (db_scen.alarms_generated if db_scen else None) or scen_info.get("generated_alarms", []) or scen_info.get("alarms_generated", [])
            for alm_code in alarm_codes_to_clear:
                if alm_code not in other_alarms:
                    alms = db.query(NetworkAlarm).filter(
                        NetworkAlarm.alarm_code == alm_code,
                        NetworkAlarm.status == "ACTIVE"
                    ).all()
                    for a in alms:
                        a.status = "CLEARED"
                        a.cleared_at = now
                        cleared_alarms += 1

            # Update DB scenario record to RECOVERED
            if db_scen:
                db_scen.status = "RECOVERED"
                db_scen.recovered_at = now

            if scenario_id in self._active_scenarios:
                self._active_scenarios[scenario_id]["status"] = "RECOVERED"
                self._active_scenarios[scenario_id]["recovered_at"] = now.isoformat()

        else:
            # Case B: Target-scoped recovery or explicit/implicit blanket market recovery
            is_blanket = (target_id == "ALL") or (not scenario_id and not target_type and not target_id)

            q_links = db.query(NetworkLink).filter(NetworkLink.market_id == market_id)
            q_devs = db.query(NetworkDevice).filter(NetworkDevice.market_id == market_id)
            q_alms = db.query(NetworkAlarm).filter(NetworkAlarm.market_id == market_id, NetworkAlarm.status == "ACTIVE")

            target_affected_device_ids = set()
            target_affected_link_ids = set()

            if not is_blanket:
                if target_entity and target_type == TARGET_TYPE_DEVICE:
                    dev = target_entity
                    descendants = self._get_downstream_subtree(db, dev.id, market_id)
                    all_devs = [dev] + descendants
                    target_affected_device_ids = {d.id for d in all_devs}
                    q_devs = q_devs.filter(NetworkDevice.id.in_(target_affected_device_ids))
                    q_links = q_links.filter(or_(NetworkLink.source_device_id.in_(target_affected_device_ids), NetworkLink.target_device_id.in_(target_affected_device_ids)))
                    q_alms = q_alms.filter(NetworkAlarm.device_id.in_(target_affected_device_ids))
                    target_affected_link_ids = {l.id for l in q_links.all()}

                elif target_entity and target_type == TARGET_TYPE_LINK:
                    target_affected_link_ids = {target_entity.id}
                    q_links = q_links.filter(NetworkLink.id == target_entity.id)
                    root = db.query(NetworkDevice).filter(NetworkDevice.id == target_entity.target_device_id).first()
                    if root:
                        all_devs = [root] + self._get_downstream_subtree(db, root.id, market_id)
                        target_affected_device_ids = {d.id for d in all_devs}
                        q_devs = q_devs.filter(NetworkDevice.id.in_(target_affected_device_ids))
                        q_alms = q_alms.filter(NetworkAlarm.device_id.in_(target_affected_device_ids))

            # Selectively mark matching active scenarios as RECOVERED
            if is_blanket:
                matching_scens = db.query(NetworkScenario).filter(
                    NetworkScenario.market_id == market_id,
                    NetworkScenario.status == "ACTIVE"
                ).all()
                for sid, sinfo in list(self._active_scenarios.items()):
                    if sinfo.get("market_id") == market_id:
                        sinfo["status"] = "RECOVERED"
                        sinfo["recovered_at"] = now.isoformat()
            else:
                matching_scen_targets = set()
                if target_id:
                    matching_scen_targets.add(str(target_id))
                if target_entity:
                    matching_scen_targets.add(str(target_entity.id))
                    if hasattr(target_entity, "device_code") and target_entity.device_code:
                        matching_scen_targets.add(str(target_entity.device_code))

                scen_query = db.query(NetworkScenario).filter(
                    NetworkScenario.market_id == market_id,
                    NetworkScenario.status == "ACTIVE",
                    NetworkScenario.target_id.in_(list(matching_scen_targets))
                )
                if target_type:
                    scen_query = scen_query.filter(NetworkScenario.target_type == target_type.upper())
                matching_scens = scen_query.all()

                for sid, sinfo in list(self._active_scenarios.items()):
                    if (
                        sinfo.get("market_id") == market_id
                        and str(sinfo.get("target_id")) in matching_scen_targets
                        and (not target_type or sinfo.get("target_type") == target_type.upper())
                    ):
                        sinfo["status"] = "RECOVERED"
                        sinfo["recovered_at"] = now.isoformat()

            for s_rec in matching_scens:
                s_rec.status = "RECOVERED"
                s_rec.recovered_at = now
                if s_rec.scenario_id in self._active_scenarios:
                    self._active_scenarios[s_rec.scenario_id]["status"] = "RECOVERED"
                    self._active_scenarios[s_rec.scenario_id]["recovered_at"] = now.isoformat()

            db.flush()

            # Identify devices and links that are STILL targeted by remaining active scenarios
            remaining_active_scens = db.query(NetworkScenario).filter(
                NetworkScenario.market_id == market_id,
                NetworkScenario.status == "ACTIVE"
            ).all()

            still_faulted_device_ids = set()
            still_faulted_link_ids = set()
            for s in remaining_active_scens:
                if s.target_type == TARGET_TYPE_LINK:
                    try:
                        still_faulted_link_ids.add(int(s.target_id))
                    except ValueError:
                        pass
                elif s.target_type == TARGET_TYPE_DEVICE:
                    matched_dev = None
                    if str(s.target_id).isdigit():
                        matched_dev = db.query(NetworkDevice).filter(NetworkDevice.id == int(s.target_id)).first()
                    else:
                        matched_dev = db.query(NetworkDevice).filter(NetworkDevice.device_code == str(s.target_id)).first()

                    if matched_dev:
                        still_faulted_device_ids.add(matched_dev.id)
                        for desc in self._get_downstream_subtree(db, matched_dev.id, market_id):
                            still_faulted_device_ids.add(desc.id)

            # Restore links that are not still faulted
            for l in q_links.all():
                if l.id not in still_faulted_link_ids:
                    if l.status != "ACTIVE" and l.status != "HEALTHY":
                        l.status = "ACTIVE"
                        recovered_links += 1

            # Restore devices that are not still faulted
            for d in q_devs.all():
                if d.id in still_faulted_device_ids:
                    continue

                d.status = "HEALTHY"
                d.last_seen_at = now
                if d.device_type in ("ONT", "ONU"):
                    d.optical_rx_dbm = -19.5
                elif d.device_type == "OLT":
                    d.cpu_utilization_pct = 42.0
                    d.memory_utilization_pct = 48.0
                    d.temperature_c = 40.0
                    for p in d.ports:
                        p.status = "HEALTHY"
                        p.utilization_pct = 45.0
                        p.last_updated_at = now
                        recovered_ports += 1
                recovered_devices += 1

                dev_alms = db.query(NetworkAlarm).filter(
                    NetworkAlarm.device_id == d.id,
                    NetworkAlarm.status == "ACTIVE"
                ).all()
                for a in dev_alms:
                    a.status = "CLEARED"
                    a.cleared_at = now
                    cleared_alarms += 1

        return {
            "scenario_type": SCENARIO_RECOVERY,
            "status": "RECOVERED",
            "recovered_links_count": recovered_links,
            "recovered_devices_count": recovered_devices,
            "recovered_ports_count": recovered_ports,
            "cleared_alarms_count": cleared_alarms,
            "summary": f"RECOVERY completed: {recovered_links} link(s), {recovered_devices} device(s), and {recovered_ports} port(s) returned to nominal health; {cleared_alarms} alarm(s) cleared."
        }

    # ── Public API: Trigger Scenario ──────────────────────────────────────────

    def trigger_scenario(
        self,
        db: Session,
        scenario_type: str,
        target_type: str,
        target_id: str,
        market_id: str,
        severity: str = "CRITICAL",
        parameters: Optional[Dict[str, Any]] = None,
        actor: Optional[User] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute deterministic synthetic fault scenario.
        Enforces:
        - Scenario type validation.
        - Target resolution & tenant/market isolation.
        - Idempotency & deduplication.
        - Atomic database commit with rollback on exception.
        - Audit trail generation.
        """
        st = scenario_type.upper().strip()
        if st not in VALID_SCENARIOS:
            raise ValueError(f"Invalid scenario_type '{scenario_type}'. Supported scenarios: {sorted(VALID_SCENARIOS)}")

        params = parameters or {}

        # 1. Idempotency Check (Durable DB lookup + in-memory cache)
        if idempotency_key:
            db_existing_idemp = db.query(NetworkScenario).filter(NetworkScenario.idempotency_key == idempotency_key).first()
            if db_existing_idemp:
                cached = self._scenario_to_dict(db_existing_idemp)
                cached["idempotent_replay"] = True
                return cached
            if idempotency_key in self._idempotency_registry:
                existing_sid = self._idempotency_registry[idempotency_key]
                if existing_sid in self._active_scenarios:
                    cached = dict(self._active_scenarios[existing_sid])
                    cached["idempotent_replay"] = True
                    return cached

        # Check if an identical scenario is already active on the same target with the same severity
        db_active_duplicate = db.query(NetworkScenario).filter(
            NetworkScenario.status == "ACTIVE",
            NetworkScenario.scenario_type == st,
            NetworkScenario.target_id == str(target_id),
            NetworkScenario.market_id == market_id,
            NetworkScenario.severity == severity.upper()
        ).first()
        if db_active_duplicate:
            cached = self._scenario_to_dict(db_active_duplicate)
            cached["idempotent_replay"] = True
            return cached

        for sid, s_info in self._active_scenarios.items():
            if (
                s_info.get("status") == "ACTIVE"
                and s_info.get("scenario_type") == st
                and str(s_info.get("target_id")) == str(target_id)
                and s_info.get("market_id") == market_id
                and s_info.get("severity") == severity.upper()
            ):
                cached = dict(s_info)
                cached["idempotent_replay"] = True
                return cached

        # 2. Handle RECOVERY directly
        if st == SCENARIO_RECOVERY:
            return self.recover_scenario(
                db=db,
                scenario_id=target_id if str(target_id).startswith("SCEN-") else None,
                target_type=target_type if target_id != "ALL" and not str(target_id).startswith("SCEN-") else None,
                target_id=target_id if target_id != "ALL" and not str(target_id).startswith("SCEN-") else None,
                market_id=market_id,
                actor=actor
            )

        # 3. Resolve Target (Validates existence & market isolation)
        target_entity, resolved_market = self._resolve_target(db, target_type, target_id, market_id)

        # 4. Atomic Execution of Fault Scenarios
        scenario_id = self._generate_scenario_id(market_id, st, params.get("seed"))

        try:
            if st == SCENARIO_FIBER_CUT:
                exec_data = self._execute_fiber_cut(db, target_entity, target_type, market_id, params)
            elif st == SCENARIO_OPTICAL_DEGRADE:
                exec_data = self._execute_optical_degrade(db, target_entity, target_type, market_id, severity, params)
            elif st == SCENARIO_PEAK_TRAFFIC:
                exec_data = self._execute_peak_traffic(db, target_entity, target_type, market_id, severity, params)
            else:
                raise ValueError(f"Unhandled scenario type: {st}")

            response_payload = {
                "scenario_id": scenario_id,
                "scenario_type": st,
                "target_type": target_type.upper(),
                "target_id": str(target_id),
                "market_id": market_id,
                "severity": severity.upper(),
                "status": "ACTIVE",
                "created_at": datetime.utcnow().isoformat(),
                "affected_links_count": len(exec_data.get("affected_links", [])),
                "affected_devices_count": len(exec_data.get("affected_devices", [])),
                "affected_ports_count": len(exec_data.get("affected_ports", [])),
                "affected_customers_count": len(exec_data.get("affected_customers", [])),
                "affected_customers": exec_data.get("affected_customers", []),
                "alarms_generated_count": len(exec_data.get("generated_alarms", [])),
                "alarms_generated": exec_data.get("generated_alarms", []),
                "target_rx_dbm": exec_data.get("target_rx_dbm"),
                "target_utilization_pct": exec_data.get("target_utilization_pct"),
                "summary": exec_data.get("summary", ""),
                "simulation": True,
                "telemetry_source": self.TELEMETRY_SOURCE
            }

            # Persist authoritative NetworkScenario in database
            db_scen_record = NetworkScenario(
                scenario_id=scenario_id,
                scenario_type=st,
                target_type=target_type.upper(),
                target_id=str(target_id),
                market_id=market_id,
                severity=severity.upper(),
                status="ACTIVE",
                parameters=_sanitize_for_json(params),
                snapshot=_sanitize_for_json(exec_data.get("snapshot", {})),
                affected_links_count=len(exec_data.get("affected_links", [])),
                affected_devices_count=len(exec_data.get("affected_devices", [])),
                affected_ports_count=len(exec_data.get("affected_ports", [])),
                affected_customers_count=len(exec_data.get("affected_customers", [])),
                alarms_generated_count=len(exec_data.get("generated_alarms", [])),
                alarms_generated=_sanitize_for_json(exec_data.get("generated_alarms", [])),
                summary=exec_data.get("summary", ""),
                idempotency_key=idempotency_key,
                created_at=datetime.utcnow()
            )
            db.add(db_scen_record)
            db.flush()

            # Cache in registry
            scen_record = dict(response_payload)
            scen_record["snapshot"] = exec_data.get("snapshot", {})
            scen_record["generated_alarms"] = exec_data.get("generated_alarms", [])
            self._active_scenarios[scenario_id] = scen_record

            if idempotency_key:
                self._idempotency_registry[idempotency_key] = scenario_id

            # Write AuditLog
            self._log_audit_event(
                db=db,
                market_id=market_id,
                action=f"TRIGGER_SCENARIO_{st}_{target_type}_{target_id}",
                actor=actor,
                signals={"scenario_type": st, "target_type": target_type, "target_id": target_id, "params": params},
                result=response_payload
            )

            # Atomic commit
            db.commit()
            return response_payload

        except Exception as e:
            db.rollback()
            raise e

    # ── Public API: Recover Scenario ──────────────────────────────────────────

    def recover_scenario(
        self,
        db: Session,
        scenario_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        market_id: str = "mumbai",
        actor: Optional[User] = None
    ) -> Dict[str, Any]:
        """
        Recover active synthetic scenarios and restore nominal health.
        """
        if (target_type and not target_id) or (target_id and target_id != "ALL" and not target_type and not scenario_id):
            raise ValueError("Ambiguous recovery request: specify both target_type and target_id, scenario_id, or omit both for blanket recovery.")

        target_entity = None
        if target_type and target_id and target_id != "ALL" and not str(target_id).startswith("SCEN-"):
            try:
                target_entity, _ = self._resolve_target(db, target_type, target_id, market_id)
            except Exception:
                target_entity = None

        result = self._execute_recovery(
            db=db,
            scenario_id=scenario_id,
            target_entity=target_entity,
            target_type=target_type,
            market_id=market_id,
            target_id=target_id
        )
        result["scenario_id"] = scenario_id or self._generate_scenario_id(market_id, "RECV")
        result["simulation"] = True
        result["telemetry_source"] = self.TELEMETRY_SOURCE

        self._log_audit_event(
            db=db,
            market_id=market_id,
            action=f"RECOVER_SCENARIO_{scenario_id or target_id or 'ALL'}",
            actor=actor,
            signals={"scenario_id": scenario_id, "target_type": target_type, "target_id": target_id},
            result=result
        )
        db.commit()
        return result

    # ── Helper: Audit Logging ─────────────────────────────────────────────────

    def _log_audit_event(
        self,
        db: Session,
        market_id: str,
        action: str,
        actor: Optional[User],
        signals: Dict[str, Any],
        result: Dict[str, Any]
    ) -> None:
        """Persist structured scenario action in audit_logs."""
        try:
            audit = AuditLog(
                market_id=market_id,
                source_module="NETWORK_SCENARIO_ENGINE",
                action_taken=action[:200],
                decision="EXECUTED",
                user_id=actor.id if actor else None,
                user_name=actor.full_name if actor else "System Automated Simulation",
                user_role=actor.role if actor else "Admin",
                confidence_score=1.0,
                original_signals=signals,
                execution_result=result,
                timestamp=datetime.utcnow()
            )
            db.add(audit)
        except Exception:
            pass

    # ── Public API: Get Active Scenarios ──────────────────────────────────────

    def get_active_scenarios(self, db: Session, market_id: str) -> List[Dict[str, Any]]:
        """List currently active synthetic fault scenarios in the market (authoritative from DB)."""
        db_scens = db.query(NetworkScenario).filter(
            NetworkScenario.status == "ACTIVE",
            NetworkScenario.market_id == market_id
        ).order_by(NetworkScenario.created_at.desc()).all()

        active = [self._scenario_to_dict(s) for s in db_scens]
        # Also sync any in-memory scenarios not yet flushed
        seen_ids = {s["scenario_id"] for s in active}
        for sid, s in self._active_scenarios.items():
            if s.get("status") == "ACTIVE" and s.get("market_id") == market_id and sid not in seen_ids:
                clean = dict(s)
                clean.pop("snapshot", None)
                active.append(clean)
        return active


# ── Global Singleton Factory ──────────────────────────────────────────────────

_scenario_engine_instance: Optional[NetworkScenarioEngine] = None

def get_scenario_engine() -> NetworkScenarioEngine:
    """Singleton accessor for NetworkScenarioEngine."""
    global _scenario_engine_instance
    if _scenario_engine_instance is None:
        _scenario_engine_instance = NetworkScenarioEngine()
    return _scenario_engine_instance
