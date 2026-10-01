"""
Phase 8: Monitoring Provider Abstraction & Synthetic Implementation

Architecture:
- Defines MonitoringProvider ABC decoupling health & metric evaluation from hardware collectors.
- Implements SyntheticMonitoringProvider delivering deterministic metrics, historical time-series,
  backend-authoritative health classification, configurable thresholds, and alarm lifecycle management.
- All telemetry explicitly marked: "Source: Synthetic / Demo Telemetry".
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import math
from sqlalchemy.orm import Session
from app.models import (
    NetworkDevice, OLTPort, NetworkAlarm, DeviceMetricThreshold,
    DeviceMetricRecord, User
)

# ─── Canonical Constants ──────────────────────────────────────────────────────

# Metric Types
METRIC_CPU_UTILIZATION = "CPU_UTILIZATION"
METRIC_MEMORY_UTILIZATION = "MEMORY_UTILIZATION"
METRIC_MEMORY_USED = "MEMORY_USED"
METRIC_MEMORY_AVAILABLE = "MEMORY_AVAILABLE"
METRIC_TEMPERATURE = "TEMPERATURE"
METRIC_UPTIME = "UPTIME"
METRIC_INTERFACE_UTILIZATION = "INTERFACE_UTILIZATION"
METRIC_PON_UTILIZATION = "PON_UTILIZATION"
METRIC_CONNECTED_CLIENTS = "CONNECTED_CLIENTS"
METRIC_BANDWIDTH_IN = "BANDWIDTH_IN"
METRIC_BANDWIDTH_OUT = "BANDWIDTH_OUT"
METRIC_OPTICAL_RX_POWER = "OPTICAL_RX_POWER"
METRIC_OPTICAL_TX_POWER = "OPTICAL_TX_POWER"
METRIC_OPTICAL_SIGNAL_QUALITY = "OPTICAL_SIGNAL_QUALITY"
METRIC_DEVICE_UP = "DEVICE_UP"
METRIC_DEVICE_DOWN = "DEVICE_DOWN"
METRIC_LAST_SEEN = "LAST_SEEN"

# Health Classifications
HEALTH_DOWN = "DOWN"
HEALTH_CRITICAL = "CRITICAL"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_WARNING = "WARNING"
HEALTH_HEALTHY = "HEALTHY"
HEALTH_UNKNOWN = "UNKNOWN"

# Deterministic Health Priority Order: Highest priority dominates overall status
HEALTH_PRIORITY = {
    HEALTH_DOWN: 6,
    HEALTH_CRITICAL: 5,
    HEALTH_DEGRADED: 4,
    HEALTH_WARNING: 3,
    HEALTH_HEALTHY: 2,
    HEALTH_UNKNOWN: 1,
}

# Telemetry Freshness Statuses
FRESHNESS_LIVE = "LIVE"        # < 60s
FRESHNESS_STALE = "STALE"      # 60s - 300s
FRESHNESS_OFFLINE = "OFFLINE"  # > 300s

# Alarm Codes
ALARM_HIGH_CPU = "HIGH_CPU"
ALARM_HIGH_MEMORY = "HIGH_MEMORY"
ALARM_HIGH_TEMPERATURE = "HIGH_TEMPERATURE"
ALARM_DEVICE_DOWN = "DEVICE_DOWN"
ALARM_HIGH_PON_UTILIZATION = "HIGH_PON_UTILIZATION"
ALARM_OPTICAL_DEGRADATION = "OPTICAL_DEGRADATION"

# Default Threshold Fixtures
DEFAULT_THRESHOLDS = [
    # OLT Defaults
    {"device_type": "OLT", "metric_type": METRIC_CPU_UTILIZATION, "warning": 70.0, "critical": 85.0, "unit": "%"},
    {"device_type": "OLT", "metric_type": METRIC_MEMORY_UTILIZATION, "warning": 75.0, "critical": 90.0, "unit": "%"},
    {"device_type": "OLT", "metric_type": METRIC_TEMPERATURE, "warning": 65.0, "critical": 80.0, "unit": "°C"},
    {"device_type": "OLT", "metric_type": METRIC_PON_UTILIZATION, "warning": 75.0, "critical": 90.0, "unit": "%"},
    # ONT / ONU Defaults
    {"device_type": "ONT", "metric_type": METRIC_CPU_UTILIZATION, "warning": 75.0, "critical": 90.0, "unit": "%"},
    {"device_type": "ONT", "metric_type": METRIC_MEMORY_UTILIZATION, "warning": 80.0, "critical": 95.0, "unit": "%"},
    {"device_type": "ONT", "metric_type": METRIC_TEMPERATURE, "warning": 60.0, "critical": 75.0, "unit": "°C"},
    {"device_type": "ONU", "metric_type": METRIC_CPU_UTILIZATION, "warning": 75.0, "critical": 90.0, "unit": "%"},
    {"device_type": "ONU", "metric_type": METRIC_MEMORY_UTILIZATION, "warning": 80.0, "critical": 95.0, "unit": "%"},
    {"device_type": "ONU", "metric_type": METRIC_TEMPERATURE, "warning": 60.0, "critical": 75.0, "unit": "°C"},
    # Fiber Cabinet Defaults (Passive chassis temperature)
    {"device_type": "FIBER_CABINET", "metric_type": METRIC_TEMPERATURE, "warning": 55.0, "critical": 70.0, "unit": "°C"},
]


class MonitoringProvider(ABC):
    """
    Abstract Base Class for carrier-grade network health and metrics telemetry.
    Future real implementations (SNMP poller, telemetry collector, OMCI) will implement this interface.
    """

    @abstractmethod
    def get_monitoring_overview(self, db: Session, market_id: str) -> Dict[str, Any]:
        """Aggregate health status metrics across all network tiers for the given market."""
        pass

    @abstractmethod
    def get_device_metrics(
        self,
        db: Session,
        device_id_or_code: str,
        market_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Retrieve current CPU, memory, temperature, uptime, PON, optical metrics, and overall health."""
        pass

    @abstractmethod
    def get_metric_history(
        self,
        db: Session,
        device_id_or_code: str,
        time_range: str = "1h",
        metric_type: Optional[str] = None,
        market_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Retrieve time-series historical metric data points for charts."""
        pass

    @abstractmethod
    def get_thresholds(
        self,
        db: Session,
        market_id: Optional[str] = None,
        device_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve configurable metric thresholds."""
        pass

    @abstractmethod
    def update_threshold(
        self,
        db: Session,
        threshold_id: int,
        warning_threshold: float,
        critical_threshold: float,
        user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Update warning and critical thresholds for a metric."""
        pass

    @abstractmethod
    def get_monitoring_alarms(
        self,
        db: Session,
        market_id: str,
        severity: Optional[str] = None,
        status: str = "ACTIVE"
    ) -> List[Dict[str, Any]]:
        """Retrieve active monitoring alarms."""
        pass

    @abstractmethod
    def simulate_device_scenario(
        self,
        db: Session,
        device_id_or_code: str,
        scenario: str,
        market_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Simulate a deterministic operational scenario (HEALTHY, WARNING, CRITICAL, DOWN)."""
        pass


class SyntheticMonitoringProvider(MonitoringProvider):
    """
    Deterministic Synthetic Monitoring Provider.
    Implements carrier-grade health classification, telemetry freshness, configurable thresholds,
    and alarm lifecycles without physical hardware collectors.
    """

    TELEMETRY_SOURCE = "Synthetic / Demo Telemetry"

    # ─── Helper: Threshold Lookups ──────────────────────────────────────────

    def _get_threshold(
        self,
        db: Session,
        device_type: str,
        metric_type: str,
        market_id: Optional[str] = None
    ) -> Dict[str, float]:
        """Query persistent threshold or fall back to defaults."""
        query = db.query(DeviceMetricThreshold).filter(
            DeviceMetricThreshold.device_type == device_type,
            DeviceMetricThreshold.metric_type == metric_type,
            DeviceMetricThreshold.enabled == True
        )
        if market_id:
            th = query.filter(DeviceMetricThreshold.market_id.in_([market_id, "all"])).first()
        else:
            th = query.first()

        if th:
            return {"warning": th.warning_threshold, "critical": th.critical_threshold}

        # Fallback to in-memory defaults
        for def_th in DEFAULT_THRESHOLDS:
            if def_th["device_type"] == device_type and def_th["metric_type"] == metric_type:
                return {"warning": def_th["warning"], "critical": def_th["critical"]}

        # General generic fallback
        return {"warning": 75.0, "critical": 90.0}

    # ─── Helper: Telemetry Freshness ────────────────────────────────────────

    def _calculate_freshness(self, last_seen_at: Optional[datetime]) -> Dict[str, Any]:
        """Backend-authoritative telemetry freshness calculation."""
        if not last_seen_at:
            return {"status": FRESHNESS_OFFLINE, "age_seconds": None, "label": "No telemetry"}

        age_seconds = int((datetime.utcnow() - last_seen_at).total_seconds())
        if age_seconds < 0:
            age_seconds = 0

        if age_seconds < 60:
            status = FRESHNESS_LIVE
            label = f"{age_seconds}s ago"
        elif age_seconds <= 300:
            status = FRESHNESS_STALE
            label = f"{age_seconds // 60}m ago"
        else:
            status = FRESHNESS_OFFLINE
            label = f"{age_seconds // 60}m ago (offline)"

        return {"status": status, "age_seconds": age_seconds, "label": label}

    # ─── Helper: Deterministic Baseline Metrics ─────────────────────────────

    def _compute_baseline_metrics(self, dev: NetworkDevice) -> Dict[str, Any]:
        """
        Derive stable, realistic baseline metrics for a device based on its code/type.
        Values are deterministic (same device always returns same metrics unless simulated).
        """
        dev_hash = sum(ord(c) for c in dev.device_code)

        if dev.device_type == "OLT":
            # Realistic OLT metrics
            cpu = dev.cpu_utilization_pct if dev.cpu_utilization_pct is not None else round(42.0 + (dev_hash % 28) + 0.4, 1)
            mem_pct = dev.memory_utilization_pct if dev.memory_utilization_pct is not None else round(55.0 + (dev_hash % 22) + 0.2, 1)
            total_mem = dev.memory_total_gb or 16.0
            used_mem = round((mem_pct / 100.0) * total_mem, 2)
            avail_mem = round(total_mem - used_mem, 2)
            temp = dev.temperature_c if dev.temperature_c is not None else round(41.0 + (dev_hash % 8) + 0.5, 1)
            uptime = dev.uptime_seconds if dev.uptime_seconds else (345600 + (dev_hash * 3600) % 2000000)

            return {
                "cpu": {"value": cpu, "unit": "%"},
                "memory": {
                    "utilization_pct": mem_pct,
                    "used_gb": used_mem,
                    "available_gb": avail_mem,
                    "total_gb": total_mem,
                    "unit": "%"
                },
                "temperature": {"value": temp, "unit": "°C"},
                "uptime": {"seconds": uptime},
            }
        elif dev.device_type in ("ONT", "ONU"):
            # Realistic ONT/ONU metrics
            cpu = dev.cpu_utilization_pct if dev.cpu_utilization_pct is not None else round(22.0 + (dev_hash % 35) + 0.1, 1)
            mem_pct = dev.memory_utilization_pct if dev.memory_utilization_pct is not None else round(38.0 + (dev_hash % 30) + 0.5, 1)
            total_mem = 0.512  # 512 MB
            used_mem = round((mem_pct / 100.0) * total_mem, 3)
            avail_mem = round(total_mem - used_mem, 3)
            temp = dev.temperature_c if dev.temperature_c is not None else round(36.0 + (dev_hash % 10) + 0.2, 1)
            uptime = dev.uptime_seconds if dev.uptime_seconds else (86400 + (dev_hash * 1800) % 500000)

            return {
                "cpu": {"value": cpu, "unit": "%"},
                "memory": {
                    "utilization_pct": mem_pct,
                    "used_gb": used_mem,
                    "available_gb": avail_mem,
                    "total_gb": total_mem,
                    "unit": "%"
                },
                "temperature": {"value": temp, "unit": "°C"},
                "uptime": {"seconds": uptime},
            }
        else:
            # Passive / Distribution nodes (Cabinets, Splitters)
            temp = dev.temperature_c if dev.temperature_c is not None else round(32.0 + (dev_hash % 12) + 0.3, 1)
            uptime = dev.uptime_seconds if dev.uptime_seconds else 864000
            return {
                "cpu": None,
                "memory": None,
                "temperature": {"value": temp, "unit": "°C"},
                "uptime": {"seconds": uptime},
            }

    # ─── Helper: Alarm Lifecycle Synchronizer ──────────────────────────────

    def _sync_alarm_lifecycle(
        self,
        db: Session,
        dev: NetworkDevice,
        alarm_code_base: str,
        code: str,
        message: str,
        severity: Optional[str]
    ) -> None:
        """
        Manages alarm lifecycle stably:
        - If severity is set (CRITICAL / WARNING): creates or updates active alarm.
        - If severity is None: recovers/resolves existing active alarm (status -> CLEARED).
        Does NOT flood duplicate alarms on repeated calls.
        """
        full_code = f"ALM-MON-{dev.id}-{alarm_code_base}"
        existing = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == dev.id,
            NetworkAlarm.alarm_code == full_code
        ).first()

        if severity:
            if existing:
                # Reactivate / update severity and last_seen without creating duplicate
                existing.status = "ACTIVE"
                existing.severity = severity
                existing.last_seen_at = datetime.utcnow()
                existing.cleared_at = None
                existing.message = message
            else:
                new_alarm = NetworkAlarm(
                    alarm_code=full_code,
                    market_id=dev.market_id,
                    device_id=dev.id,
                    severity=severity,
                    code=code,
                    message=message,
                    status="ACTIVE",
                    first_seen_at=datetime.utcnow(),
                    last_seen_at=datetime.utcnow()
                )
                db.add(new_alarm)
            db.commit()
        else:
            # Recovery: metric back to normal
            if existing and existing.status == "ACTIVE":
                existing.status = "CLEARED"
                existing.cleared_at = datetime.utcnow()
                db.commit()

    # ─── Health Classification Engine ───────────────────────────────────────

    def _evaluate_device_health(
        self,
        db: Session,
        dev: NetworkDevice,
        metrics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Evaluates device health deterministically:
        Priority: DOWN > CRITICAL > DEGRADED > WARNING > HEALTHY > UNKNOWN.
        Manages monitoring alarm lifecycles and compiles health_reasons.
        """
        reasons = []
        statuses = []

        # 1. Connectivity Check
        if dev.status == HEALTH_DOWN:
            statuses.append(HEALTH_DOWN)
            reasons.append("Device is administratively or operationally DOWN")
            self._sync_alarm_lifecycle(
                db, dev, ALARM_DEVICE_DOWN, "DEVICE_UNAVAILABLE",
                f"Device {dev.device_code} is DOWN / unreachable", "CRITICAL"
            )
        else:
            self._sync_alarm_lifecycle(db, dev, ALARM_DEVICE_DOWN, "DEVICE_UNAVAILABLE", "", None)

        # 2. CPU Utilization Check
        cpu_data = metrics.get("cpu")
        if cpu_data and cpu_data["value"] is not None:
            val = cpu_data["value"]
            th = self._get_threshold(db, dev.device_type, METRIC_CPU_UTILIZATION, dev.market_id)
            if val >= th["critical"]:
                statuses.append(HEALTH_CRITICAL)
                reasons.append(f"CPU utilization ({val}%) reached CRITICAL threshold (≥ {th['critical']}%)")
                cpu_data["status"] = "CRITICAL"
                self._sync_alarm_lifecycle(
                    db, dev, ALARM_HIGH_CPU, "HIGH_CPU_LOAD",
                    f"High CPU utilization ({val}%) on {dev.device_code}", "CRITICAL"
                )
            elif val >= th["warning"]:
                statuses.append(HEALTH_WARNING)
                reasons.append(f"CPU utilization ({val}%) reached WARNING threshold (≥ {th['warning']}%)")
                cpu_data["status"] = "WARNING"
                self._sync_alarm_lifecycle(
                    db, dev, ALARM_HIGH_CPU, "HIGH_CPU_LOAD",
                    f"Elevated CPU utilization ({val}%) on {dev.device_code}", "WARNING"
                )
            else:
                cpu_data["status"] = "NORMAL"
                self._sync_alarm_lifecycle(db, dev, ALARM_HIGH_CPU, "HIGH_CPU_LOAD", "", None)

        # 3. Memory Utilization Check
        mem_data = metrics.get("memory")
        if mem_data and mem_data.get("utilization_pct") is not None:
            val = mem_data["utilization_pct"]
            th = self._get_threshold(db, dev.device_type, METRIC_MEMORY_UTILIZATION, dev.market_id)
            if val >= th["critical"]:
                statuses.append(HEALTH_CRITICAL)
                reasons.append(f"Memory utilization ({val}%) reached CRITICAL threshold (≥ {th['critical']}%)")
                mem_data["status"] = "CRITICAL"
                self._sync_alarm_lifecycle(
                    db, dev, ALARM_HIGH_MEMORY, "HIGH_MEMORY_PRESSURE",
                    f"Critical memory pressure ({val}%) on {dev.device_code}", "CRITICAL"
                )
            elif val >= th["warning"]:
                statuses.append(HEALTH_WARNING)
                reasons.append(f"Memory utilization ({val}%) reached WARNING threshold (≥ {th['warning']}%)")
                mem_data["status"] = "WARNING"
                self._sync_alarm_lifecycle(
                    db, dev, ALARM_HIGH_MEMORY, "HIGH_MEMORY_PRESSURE",
                    f"Elevated memory usage ({val}%) on {dev.device_code}", "WARNING"
                )
            else:
                mem_data["status"] = "NORMAL"
                self._sync_alarm_lifecycle(db, dev, ALARM_HIGH_MEMORY, "HIGH_MEMORY_PRESSURE", "", None)

        # 4. Temperature Check
        temp_data = metrics.get("temperature")
        if temp_data and temp_data["value"] is not None:
            val = temp_data["value"]
            th = self._get_threshold(db, dev.device_type, METRIC_TEMPERATURE, dev.market_id)
            if val >= th["critical"]:
                statuses.append(HEALTH_CRITICAL)
                reasons.append(f"Chassis temperature ({val}°C) reached CRITICAL threshold (≥ {th['critical']}°C)")
                temp_data["status"] = "CRITICAL"
                self._sync_alarm_lifecycle(
                    db, dev, ALARM_HIGH_TEMPERATURE, "THERMAL_ALARM",
                    f"Chassis thermal alert ({val}°C) on {dev.device_code}", "CRITICAL"
                )
            elif val >= th["warning"]:
                statuses.append(HEALTH_WARNING)
                reasons.append(f"Chassis temperature ({val}°C) reached WARNING threshold (≥ {th['warning']}°C)")
                temp_data["status"] = "WARNING"
                self._sync_alarm_lifecycle(
                    db, dev, ALARM_HIGH_TEMPERATURE, "THERMAL_ALARM",
                    f"Chassis temperature elevated ({val}°C) on {dev.device_code}", "WARNING"
                )
            else:
                temp_data["status"] = "NORMAL"
                self._sync_alarm_lifecycle(db, dev, ALARM_HIGH_TEMPERATURE, "THERMAL_ALARM", "", None)

        # 5. PON Interfaces Utilization Check (for OLT)
        if dev.device_type == "OLT" and dev.ports:
            th = self._get_threshold(db, dev.device_type, METRIC_PON_UTILIZATION, dev.market_id)
            for p in dev.ports:
                if p.utilization_pct >= th["critical"]:
                    statuses.append(HEALTH_CRITICAL)
                    reasons.append(f"PON {p.port_number} capacity reached CRITICAL utilization ({p.utilization_pct}%)")
                elif p.utilization_pct >= th["warning"]:
                    statuses.append(HEALTH_WARNING)
                    reasons.append(f"PON {p.port_number} capacity reached WARNING utilization ({p.utilization_pct}%)")

        # 6. Optical Health Check
        if dev.optical_rx_dbm is not None:
            if dev.optical_rx_dbm <= -28.0:
                statuses.append(HEALTH_DEGRADED)
                reasons.append(f"High optical attenuation ({dev.optical_rx_dbm} dBm) on drop line")
            elif dev.optical_rx_dbm <= -26.0:
                statuses.append(HEALTH_WARNING)
                reasons.append(f"Marginal optical Rx power ({dev.optical_rx_dbm} dBm)")

        # 7. Active External Alarms Check
        active_alarms = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == dev.id,
            NetworkAlarm.status == "ACTIVE"
        ).all()
        for a in active_alarms:
            if a.severity == "CRITICAL":
                statuses.append(HEALTH_CRITICAL)
                if a.message not in reasons:
                    reasons.append(f"Active Critical Alarm: {a.message}")
            elif a.severity == "WARNING":
                statuses.append(HEALTH_WARNING)
                if a.message not in reasons:
                    reasons.append(f"Active Warning: {a.message}")

        # Deterministic Priority Selection
        if not statuses:
            overall = HEALTH_HEALTHY
            reasons.append("All system metrics, interfaces, and optical paths operating within nominal thresholds")
        else:
            overall = max(statuses, key=lambda s: HEALTH_PRIORITY.get(s, 0))

        return {
            "overall_status": overall,
            "health_reasons": reasons
        }

    # ─── Public API Implementations ─────────────────────────────────────────

    def get_monitoring_overview(self, db: Session, market_id: str) -> Dict[str, Any]:
        """Aggregate health overview across all infrastructure tiers."""
        devices = db.query(NetworkDevice).filter(NetworkDevice.market_id == market_id).all()

        olt_counts = {"healthy": 0, "warning": 0, "critical": 0, "down": 0, "total": 0}
        ont_counts = {"healthy": 0, "warning": 0, "critical": 0, "down": 0, "total": 0}
        pon_counts = {"normal": 0, "high_utilization": 0, "total": 0}

        for d in devices:
            if d.device_type == "OLT":
                olt_counts["total"] += 1
                st = (d.status or "HEALTHY").lower()
                if st in olt_counts:
                    olt_counts[st] += 1
                else:
                    olt_counts["healthy"] += 1

                for p in d.ports:
                    pon_counts["total"] += 1
                    if p.utilization_pct >= 75.0:
                        pon_counts["high_utilization"] += 1
                    else:
                        pon_counts["normal"] += 1

            elif d.device_type in ("ONT", "ONU"):
                ont_counts["total"] += 1
                st = (d.status or "HEALTHY").lower()
                if st in ont_counts:
                    ont_counts[st] += 1
                else:
                    ont_counts["healthy"] += 1

        alarms_q = db.query(NetworkAlarm).filter(
            NetworkAlarm.market_id == market_id,
            NetworkAlarm.status == "ACTIVE"
        ).all()
        alarm_counts = {
            "critical": sum(1 for a in alarms_q if a.severity == "CRITICAL"),
            "warning": sum(1 for a in alarms_q if a.severity == "WARNING"),
            "info": sum(1 for a in alarms_q if a.severity == "INFO"),
            "total": len(alarms_q)
        }

        return {
            "market_id": market_id,
            "olts": olt_counts,
            "onts": ont_counts,
            "pons": pon_counts,
            "alarms": alarm_counts,
            "devices_by_type": {
                "OLT": olt_counts["total"],
                "ONT": ont_counts["total"],
                "FIBER_CABINET": sum(1 for d in devices if d.device_type == "FIBER_CABINET"),
                "SPLITTER": sum(1 for d in devices if d.device_type == "SPLITTER")
            },
            "devices_by_health": {
                "HEALTHY": olt_counts["healthy"] + ont_counts["healthy"],
                "WARNING": olt_counts["warning"] + ont_counts["warning"],
                "CRITICAL": olt_counts["critical"] + ont_counts["critical"],
                "DOWN": olt_counts["down"] + ont_counts["down"]
            },
            "pon_utilization_summary": pon_counts,
            "active_monitoring_alarms": alarm_counts["total"],
            "telemetry_source": self.TELEMETRY_SOURCE
        }

    def get_device_metrics(
        self,
        db: Session,
        device_id_or_code: str,
        market_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Retrieve full current device telemetry metrics with backend health classification."""
        query = db.query(NetworkDevice)
        if device_id_or_code.isdigit():
            query = query.filter(NetworkDevice.id == int(device_id_or_code))
        else:
            query = query.filter(NetworkDevice.device_code == device_id_or_code)

        if market_id:
            query = query.filter(NetworkDevice.market_id == market_id)

        dev = query.first()
        if not dev:
            return None

        # Compute deterministic baseline metrics
        metrics = self._compute_baseline_metrics(dev)

        # Freshness evaluation
        freshness = self._calculate_freshness(dev.last_seen_at)

        # Health classification
        eval_result = self._evaluate_device_health(db, dev, metrics)

        # PON ports summary if OLT
        pon_summary = None
        if dev.device_type == "OLT" and dev.ports:
            pon_summary = [
                {
                    "id": p.id,
                    "port_number": p.port_number,
                    "technology": p.technology,
                    "status": p.status,
                    "connected_clients": p.connected_clients,
                    "capacity": p.capacity,
                    "utilization_pct": p.utilization_pct,
                    "tx_power_dbm": p.tx_power_dbm,
                    "rx_power_dbm": p.rx_power_dbm,
                    "status_classification": "CRITICAL" if p.utilization_pct >= 90.0 else ("WARNING" if p.utilization_pct >= 75.0 else "NORMAL")
                }
                for p in dev.ports
            ]

        # Active alarms
        alarms = db.query(NetworkAlarm).filter(
            NetworkAlarm.device_id == dev.id,
            NetworkAlarm.status == "ACTIVE"
        ).order_by(NetworkAlarm.first_seen_at.desc()).all()

        uptime_sec = metrics["uptime"]["seconds"] if metrics.get("uptime") else 0
        uptime_days = uptime_sec // 86400
        uptime_hrs = (uptime_sec % 86400) // 3600

        return {
            "device_id": dev.id,
            "device_code": dev.device_code,
            "device_name": dev.device_name,
            "device_type": dev.device_type,
            "market_id": dev.market_id,
            "status": dev.status,
            "overall_status": eval_result["overall_status"],
            "health_reasons": eval_result["health_reasons"],
            "telemetry_status": freshness["status"],
            "last_telemetry_label": freshness["label"],
            "last_seen_at": dev.last_seen_at.isoformat() if dev.last_seen_at else None,
            "metrics": metrics,
            "system_metrics": {
                "cpu_utilization_pct": metrics["cpu"]["value"] if metrics.get("cpu") else None,
                "memory_utilization_pct": metrics["memory"]["utilization_pct"] if metrics.get("memory") else None,
                "memory_total_gb": metrics["memory"]["total_gb"] if metrics.get("memory") else None,
                "memory_used_gb": metrics["memory"]["used_gb"] if metrics.get("memory") else None,
                "memory_available_gb": metrics["memory"]["available_gb"] if metrics.get("memory") else None,
                "temperature_celsius": metrics["temperature"]["value"] if metrics.get("temperature") else None,
                "uptime_seconds": uptime_sec,
                "uptime_formatted": f"{uptime_days}d {uptime_hrs}h",
            },
            "telemetry_freshness": {
                "freshness_status": freshness["status"],
                "seconds_ago": freshness["age_seconds"],
                "label": freshness["label"]
            },
            "optical": {
                "rx_power_dbm": dev.optical_rx_dbm,
                "tx_power_dbm": dev.optical_tx_dbm,
                "status": "CRITICAL" if dev.optical_rx_dbm and dev.optical_rx_dbm <= -28.0 else (
                    "WARNING" if dev.optical_rx_dbm and dev.optical_rx_dbm <= -26.0 else "NORMAL"
                )
            },
            "pon_ports": pon_summary,
            "active_alarms": [
                {
                    "id": a.id,
                    "alarm_code": a.alarm_code,
                    "severity": a.severity,
                    "code": a.code,
                    "message": a.message,
                    "first_seen_at": a.first_seen_at.isoformat() if a.first_seen_at else None,
                }
                for a in alarms
            ],
            "active_alarm_count": len(alarms),
            "telemetry_source": self.TELEMETRY_SOURCE
        }

    def get_metric_history(
        self,
        db: Session,
        device_id_or_code: str,
        time_range: str = "1h",
        metric_type: Optional[str] = None,
        market_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Generate deterministic historical time-series metric points (1h, 6h, 24h).
        Points are stable, realistic, and repeatable per device.
        """
        query = db.query(NetworkDevice)
        if device_id_or_code.isdigit():
            query = query.filter(NetworkDevice.id == int(device_id_or_code))
        else:
            query = query.filter(NetworkDevice.device_code == device_id_or_code)

        if market_id:
            query = query.filter(NetworkDevice.market_id == market_id)

        dev = query.first()
        if not dev:
            return None

        # Resolve time range parameters
        now = datetime.utcnow()
        if time_range == "24h":
            step_mins = 30
            num_points = 48
            start_time = now - timedelta(hours=24)
        elif time_range == "6h":
            step_mins = 10
            num_points = 36
            start_time = now - timedelta(hours=6)
        else:  # default 1h
            time_range = "1h"
            step_mins = 2
            num_points = 30
            start_time = now - timedelta(hours=1)

        dev_seed = sum(ord(c) for c in dev.device_code)
        base_metrics = self._compute_baseline_metrics(dev)
        base_cpu = base_metrics["cpu"]["value"] if base_metrics.get("cpu") else 35.0
        base_mem = base_metrics["memory"]["utilization_pct"] if base_metrics.get("memory") else 50.0
        base_temp = base_metrics["temperature"]["value"] if base_metrics.get("temperature") else 40.0

        points = []
        for i in range(num_points):
            pt_time = start_time + timedelta(minutes=i * step_mins)
            # Deterministic trigonometric fluctuation
            t_factor = i / float(num_points)
            cpu_fluct = math.sin(t_factor * 6.28 + dev_seed) * 6.5 + math.cos(t_factor * 12.56) * 3.0
            mem_fluct = math.cos(t_factor * 6.28 + dev_seed * 0.5) * 3.5
            temp_fluct = math.sin(t_factor * 3.14 + dev_seed) * 2.2

            c_val = round(max(5.0, min(99.0, base_cpu + cpu_fluct)), 1)
            m_val = round(max(10.0, min(99.0, base_mem + mem_fluct)), 1)
            t_val = round(max(20.0, min(95.0, base_temp + temp_fluct)), 1)

            pon_val = round(max(15.0, min(95.0, 48.0 + math.sin(t_factor * 6.28) * 8.0)), 1)
            points.append({
                "timestamp": pt_time.isoformat(),
                "time_label": pt_time.strftime("%H:%M"),
                "cpu_utilization": c_val,
                "cpu_utilization_pct": c_val,
                "memory_utilization": m_val,
                "memory_utilization_pct": m_val,
                "temperature": t_val,
                "temperature_celsius": t_val,
                "pon_utilization_pct": pon_val
            })

        return {
            "device_id": dev.id,
            "device_code": dev.device_code,
            "device_type": dev.device_type,
            "time_range": time_range,
            "data_points_count": len(points),
            "data_points": points,
            "points": points,
            "telemetry_source": self.TELEMETRY_SOURCE
        }

    def get_thresholds(
        self,
        db: Session,
        market_id: Optional[str] = None,
        device_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List configured metric thresholds."""
        query = db.query(DeviceMetricThreshold)
        if device_type:
            query = query.filter(DeviceMetricThreshold.device_type == device_type)
        if market_id:
            query = query.filter(DeviceMetricThreshold.market_id.in_([market_id, "all"]))

        records = query.all()
        results = [
            {
                "id": t.id,
                "market_id": t.market_id,
                "device_type": t.device_type,
                "metric_type": t.metric_type,
                "warning_threshold": t.warning_threshold,
                "critical_threshold": t.critical_threshold,
                "unit": t.unit,
                "enabled": t.enabled,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in records
        ]
        return results

    def update_threshold(
        self,
        db: Session,
        threshold_id: int,
        warning_threshold: float,
        critical_threshold: float,
        user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Update warning and critical thresholds with validation."""
        th = db.query(DeviceMetricThreshold).filter(DeviceMetricThreshold.id == threshold_id).first()
        if not th:
            raise ValueError(f"Threshold ID {threshold_id} not found.")

        if warning_threshold < 0 or critical_threshold < 0:
            raise ValueError("Threshold values must be non-negative.")
        if warning_threshold > critical_threshold:
            raise ValueError("Warning threshold cannot exceed critical threshold.")

        th.warning_threshold = float(warning_threshold)
        th.critical_threshold = float(critical_threshold)
        th.updated_at = datetime.utcnow()
        if user_id:
            th.updated_by_id = user_id
        db.commit()

        return {
            "id": th.id,
            "device_type": th.device_type,
            "metric_type": th.metric_type,
            "warning_threshold": th.warning_threshold,
            "critical_threshold": th.critical_threshold,
            "status": "UPDATED"
        }

    def get_monitoring_alarms(
        self,
        db: Session,
        market_id: str,
        severity: Optional[str] = None,
        status: str = "ACTIVE"
    ) -> List[Dict[str, Any]]:
        """Retrieve monitoring alarms with device metadata."""
        query = db.query(NetworkAlarm).filter(
            NetworkAlarm.market_id == market_id,
            NetworkAlarm.status == status
        )
        if severity:
            query = query.filter(NetworkAlarm.severity == severity.upper())

        alarms = query.order_by(NetworkAlarm.first_seen_at.desc()).all()
        return [
            {
                "id": a.id,
                "alarm_code": a.alarm_code,
                "device_id": a.device_id,
                "device_code": a.device.device_code if a.device else None,
                "device_name": a.device.device_name if a.device else None,
                "device_type": a.device.device_type if a.device else None,
                "severity": a.severity,
                "code": a.code,
                "alarm_type": a.alarm_code.replace(f"ALM-MON-{a.device_id}-", "") if a.device_id and a.alarm_code and a.alarm_code.startswith(f"ALM-MON-{a.device_id}-") else a.code,
                "message": a.message,
                "status": a.status,
                "first_seen_at": a.first_seen_at.isoformat() if a.first_seen_at else None,
                "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                "telemetry_source": self.TELEMETRY_SOURCE
            }
            for a in alarms
        ]

    def simulate_device_scenario(
        self,
        db: Session,
        device_id_or_code: str,
        scenario: str,
        market_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Controlled test fixture simulator for development and verification:
        Supported scenarios: HEALTHY, WARNING, CRITICAL, DOWN.
        """
        query = db.query(NetworkDevice)
        if device_id_or_code.isdigit():
            query = query.filter(NetworkDevice.id == int(device_id_or_code))
        else:
            query = query.filter(NetworkDevice.device_code == device_id_or_code)

        if market_id:
            query = query.filter(NetworkDevice.market_id == market_id)

        dev = query.first()
        if not dev:
            raise ValueError(f"Device {device_id_or_code} not found.")

        scen = scenario.upper()
        if scen == "HEALTHY":
            dev.status = HEALTH_HEALTHY
            dev.cpu_utilization_pct = 42.0
            dev.memory_utilization_pct = 48.0
            dev.temperature_c = 40.0
            dev.last_seen_at = datetime.utcnow()
            if dev.optical_rx_dbm is not None:
                dev.optical_rx_dbm = -19.5
            if dev.ports:
                for p in dev.ports:
                    p.utilization_pct = 45.0
                    p.status = "HEALTHY"
            active_alarms = db.query(NetworkAlarm).filter(
                NetworkAlarm.device_id == dev.id,
                NetworkAlarm.status == "ACTIVE"
            ).all()
            for a in active_alarms:
                a.status = "CLEARED"
                a.cleared_at = datetime.utcnow()
        elif scen == "WARNING":
            dev.status = HEALTH_WARNING
            dev.cpu_utilization_pct = 78.0
            dev.memory_utilization_pct = 78.0
            dev.temperature_c = 68.0
            dev.last_seen_at = datetime.utcnow()
        elif scen == "CRITICAL":
            dev.status = HEALTH_CRITICAL
            dev.cpu_utilization_pct = 94.0
            dev.memory_utilization_pct = 92.0
            dev.temperature_c = 84.0
            dev.last_seen_at = datetime.utcnow()
        elif scen == "DOWN":
            dev.status = HEALTH_DOWN
            dev.last_seen_at = datetime.utcnow() - timedelta(minutes=15)  # stale/offline
        else:
            raise ValueError(f"Unknown scenario '{scenario}'. Allowed: HEALTHY, WARNING, CRITICAL, DOWN.")

        db.commit()

        # Re-evaluate metrics and sync alarms
        return self.get_device_metrics(db, str(dev.id), dev.market_id)


# ─── Singleton Factory ────────────────────────────────────────────────────────

_monitoring_provider_instance: Optional[MonitoringProvider] = None

def get_monitoring_provider() -> MonitoringProvider:
    """Singleton getter for the active MonitoringProvider."""
    global _monitoring_provider_instance
    if _monitoring_provider_instance is None:
        _monitoring_provider_instance = SyntheticMonitoringProvider()
    return _monitoring_provider_instance
