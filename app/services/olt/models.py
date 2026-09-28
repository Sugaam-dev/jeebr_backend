from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class OLTStatus(str, Enum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    UNREACHABLE = "UNREACHABLE"
    AUTH_FAILURE = "AUTHENTICATION_FAILURE"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class AlarmSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    WARNING = "WARNING"
    INFO = "INFO"


class ONTStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    LOS = "LOS"  # Loss of Signal
    DYING_GASP = "DYING_GASP"
    DEGRADED = "DEGRADED"


# --- Stage 1: Device Connectivity & Identity ---

class OLTDeviceInfo(BaseModel):
    id: str = Field(..., description="Unique internal identifier or node_code")
    name: str = Field(..., description="Human-readable OLT name")
    vendor: str = Field("Generic", description="Hardware vendor (e.g., Huawei, ZTE, Nokia, FiberHome, Generic)")
    model: str = Field("Standard-OLT", description="Hardware chassis model")
    firmware_version: Optional[str] = Field(None, description="Current firmware/software build")
    management_ip: str = Field(..., description="Management IPv4/IPv6 address")
    mac_address: Optional[str] = Field(None, description="Chassis base MAC address")
    status: OLTStatus = Field(OLTStatus.ONLINE, description="Reachability and connectivity health")
    last_seen: datetime = Field(default_factory=datetime.utcnow, description="Last successful poll timestamp")
    uptime_seconds: int = Field(0, description="System uptime in seconds")
    pon_ports_count: int = Field(16, description="Total physical PON interfaces")
    market_id: str = Field("mumbai", description="Regional network boundary")


# --- Stage 2: Hardware Device Health ---

class OLTSystemMetrics(BaseModel):
    olt_id: str
    cpu_utilization_pct: float = Field(0.0, ge=0.0, le=100.0)
    memory_utilization_pct: float = Field(0.0, ge=0.0, le=100.0)
    temperature_celsius: float = Field(42.0, description="Chassis internal ambient temperature")
    fan_status: str = Field("NORMAL", description="NORMAL, DEGRADED, FAILED")
    power_supply_status: str = Field("REDUNDANT_OK", description="Power supply telemetry")
    recorded_at: datetime = Field(default_factory=datetime.utcnow)


# --- Stage 3: PON Port Telemetry ---

class PONPortTelemetry(BaseModel):
    olt_id: str
    port_id: str = Field(..., description="Interface slot/port notation, e.g., 0/1/0")
    port_name: str = Field(..., description="Friendly label, e.g., PON-Port-1")
    technology: str = Field("GPON", description="GPON, XGS-PON, NG-PON2, EPON")
    admin_status: str = Field("UP", description="UP, DOWN")
    operational_status: str = Field("UP", description="UP, DOWN, DEGRADED")
    ont_count_online: int = Field(0, ge=0)
    ont_count_total: int = Field(0, ge=0)
    tx_power_dbm: float = Field(3.5, description="OLT Transceiver Tx optical output power in dBm")
    rx_power_dbm: float = Field(-20.0, description="Aggregated optical Rx power received from splitters")
    wavelength_nm: int = Field(1490, description="Downstream optical wavelength")
    recorded_at: datetime = Field(default_factory=datetime.utcnow)


# --- Stage 4: ONT Subscriber Endpoint Telemetry ---

class ONTSignal(BaseModel):
    rx_power_dbm: float = Field(..., description="Optical power received at subscriber ONT (-18 to -24 nominal)")
    tx_power_dbm: float = Field(2.5, description="Optical power transmitted by subscriber ONT")
    olt_rx_power_dbm: float = Field(-20.0, description="Upstream power arriving back at the OLT port")
    ber_rate: float = Field(0.0, description="Bit error rate")
    snr_db: float = Field(32.0, description="Signal to noise ratio in dB")


class ONTTelemetry(BaseModel):
    olt_id: str
    pon_port_id: str
    ont_id: str = Field(..., description="ONT equipment index or serial number")
    serial_number: str = Field(..., description="ONT optical vendor serial number")
    customer_id: Optional[int] = Field(None, description="Linked SentinelOS Customer ID")
    customer_name: Optional[str] = None
    status: ONTStatus = Field(ONTStatus.ONLINE)
    signal: ONTSignal
    distance_meters: Optional[int] = Field(None, description="Estimated fiber distance from OLT")
    last_seen: datetime = Field(default_factory=datetime.utcnow)


# --- Stage 5: Alarms & Events ---

class OLTAlarm(BaseModel):
    id: str = Field(..., description="Unique alarm identifier")
    olt_id: str
    severity: AlarmSeverity = Field(AlarmSeverity.WARNING)
    source: str = Field(..., description="Alarm source entity, e.g., Port 0/1/2 or Chassis")
    code: str = Field(..., description="Standardized alarm code, e.g., LOS, LOF, DYING_GASP, HIGH_TEMP")
    message: str = Field(..., description="Descriptive alarm explanation")
    occurred_at: datetime = Field(default_factory=datetime.utcnow)
    cleared_at: Optional[datetime] = None
    is_active: bool = True


# --- Master Normalized Telemetry Snapshot ---

class NormalizedTelemetrySnapshot(BaseModel):
    olt: OLTDeviceInfo
    system_metrics: OLTSystemMetrics
    pon_ports: List[PONPortTelemetry] = Field(default_factory=list)
    onts: List[ONTTelemetry] = Field(default_factory=list)
    active_alarms: List[OLTAlarm] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=datetime.utcnow)
    provider_type: str = Field("synthetic", description="Provider engine that supplied this snapshot")
