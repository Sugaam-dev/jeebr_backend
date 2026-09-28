import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from app.services.olt.base import OLTAdapter
from app.services.olt.models import (
    OLTDeviceInfo,
    OLTSystemMetrics,
    PONPortTelemetry,
    ONTTelemetry,
    ONTStatus,
    ONTSignal,
    OLTAlarm,
    OLTStatus,
    AlarmSeverity
)

logger = logging.getLogger(__name__)


class MockSNMPOLTAdapter(OLTAdapter):
    """
    Simulated Hardware SNMP Collector Adapter (Section 23 & 27).
    Architectural foundation for real SNMP/MIB polling (e.g. via pysnmp/netsnmp).
    Demonstrates Stage 1–5 normalization, safe credential masking, and granular failure modes:
    - OLT ONLINE
    - OLT DEGRADED
    - OLT UNREACHABLE
    - AUTHENTICATION_FAILURE
    - TIMEOUT
    NOTE: Real physical hardware integration remains NOT VERIFIED due to absence of physical target credentials/MIBs.
    """

    def __init__(
        self,
        device_id: str = "OLT-HW-001",
        management_ip: str = "192.168.100.1",
        credentials: Optional[Dict[str, Any]] = None,
        simulate_failure_mode: Optional[str] = None
    ):
        super().__init__(device_id=device_id, management_ip=management_ip, credentials=credentials)
        self.simulate_failure_mode = simulate_failure_mode  # 'TIMEOUT', 'AUTH_FAILURE', 'UNREACHABLE', 'DEGRADED', None
        # Mask credentials in logs (Section 27 & 28)
        community = (credentials or {}).get("snmp_community", "******")
        logger.info(
            f"[OLT_INIT] Initialized SNMP Adapter for Device '{device_id}' at {management_ip} "
            f"(SNMP Community: [PROTECTED-{len(community)}chars])"
        )

    async def connect(self) -> bool:
        if self.simulate_failure_mode == "TIMEOUT":
            await asyncio.sleep(0.05)
            raise TimeoutError(f"Connection timed out while querying SNMP agent at {self.management_ip}:161")
        if self.simulate_failure_mode == "AUTH_FAILURE":
            raise PermissionError(f"SNMPv2/v3 authentication failed for host {self.management_ip} (bad community/user)")
        if self.simulate_failure_mode == "UNREACHABLE":
            raise ConnectionRefusedError(f"Host {self.management_ip} unreachable (ICMP host unreachable)")

        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def health_check(self) -> Dict[str, Any]:
        if self.simulate_failure_mode == "TIMEOUT":
            return {"device_id": self.device_id, "status": OLTStatus.TIMEOUT.value, "reachable": False, "error": "Connection timed out"}
        if self.simulate_failure_mode == "AUTH_FAILURE":
            return {"device_id": self.device_id, "status": OLTStatus.AUTH_FAILURE.value, "reachable": False, "error": "Authentication failure"}
        if self.simulate_failure_mode == "UNREACHABLE":
            return {"device_id": self.device_id, "status": OLTStatus.UNREACHABLE.value, "reachable": False, "error": "Host unreachable"}
        if self.simulate_failure_mode == "DEGRADED":
            return {"device_id": self.device_id, "status": OLTStatus.DEGRADED.value, "reachable": True, "latency_ms": 145.0}

        return {
            "device_id": self.device_id,
            "status": OLTStatus.ONLINE.value,
            "management_ip": self.management_ip,
            "latency_ms": 4.5,
            "packet_loss_pct": 0.0,
            "reachable": True,
            "provider": "MockSNMPOLTAdapter"
        }

    async def get_device_info(self) -> OLTDeviceInfo:
        await self.connect()
        status = OLTStatus.DEGRADED if self.simulate_failure_mode == "DEGRADED" else OLTStatus.ONLINE
        return OLTDeviceInfo(
            id=self.device_id,
            name=f"Edge OLT Gateway ({self.management_ip})",
            vendor="Mock-Vendor-ZTE",
            model="ZXA10-C300",
            firmware_version="V2.1.0",
            management_ip=self.management_ip,
            status=status,
            uptime_seconds=1254000,
            pon_ports_count=16,
            market_id="mumbai"
        )

    async def get_system_metrics(self) -> OLTSystemMetrics:
        await self.connect()
        cpu = 88.0 if self.simulate_failure_mode == "DEGRADED" else 24.5
        temp = 68.0 if self.simulate_failure_mode == "DEGRADED" else 41.5
        return OLTSystemMetrics(
            olt_id=self.device_id,
            cpu_utilization_pct=cpu,
            memory_utilization_pct=45.0,
            temperature_celsius=temp,
            fan_status="DEGRADED" if temp > 65.0 else "NORMAL",
            power_supply_status="REDUNDANT_OK"
        )

    async def get_pon_ports(self) -> List[PONPortTelemetry]:
        await self.connect()
        return [
            PONPortTelemetry(
                olt_id=self.device_id,
                port_id=f"1/1/{i}",
                port_name=f"GPON-1/1/{i}",
                technology="GPON",
                admin_status="UP",
                operational_status="UP",
                ont_count_online=14,
                ont_count_total=16,
                tx_power_dbm=4.2,
                rx_power_dbm=-19.8
            )
            for i in range(1, 5)
        ]

    async def get_onts(self, pon_port_id: Optional[str] = None) -> List[ONTTelemetry]:
        await self.connect()
        return [
            ONTTelemetry(
                olt_id=self.device_id,
                pon_port_id="1/1/1",
                ont_id="ONT-SNMP-001",
                serial_number="ZTEGC1234567",
                customer_id=1,
                customer_name="Sample Subscriber",
                status=ONTStatus.ONLINE,
                signal=ONTSignal(
                    rx_power_dbm=-20.5,
                    tx_power_dbm=2.1,
                    olt_rx_power_dbm=-21.0,
                    ber_rate=0.000001,
                    snr_db=36.0
                ),
                distance_meters=1120
            )
        ]

    async def get_ont_status(self, ont_id: str) -> ONTStatus:
        await self.connect()
        return ONTStatus.ONLINE

    async def get_ont_signal(self, ont_id: str) -> Optional[ONTSignal]:
        await self.connect()
        return ONTSignal(
            rx_power_dbm=-20.5,
            tx_power_dbm=2.1,
            olt_rx_power_dbm=-21.0,
            ber_rate=0.000001,
            snr_db=36.0
        )

    async def get_alarms(self) -> List[OLTAlarm]:
        await self.connect()
        if self.simulate_failure_mode == "DEGRADED":
            return [
                OLTAlarm(
                    id=f"ALM-{self.device_id}-TEMP-HIGH",
                    olt_id=self.device_id,
                    severity=AlarmSeverity.MAJOR,
                    source="Chassis-Fan-Tray",
                    code="HIGH_TEMPERATURE",
                    message="Chassis temperature exceeded 65C threshold",
                    occurred_at=datetime.utcnow() - timedelta(minutes=10),
                    is_active=True
                )
            ]
        return []
