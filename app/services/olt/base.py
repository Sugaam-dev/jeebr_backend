from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from app.services.olt.models import (
    OLTDeviceInfo,
    OLTSystemMetrics,
    PONPortTelemetry,
    ONTTelemetry,
    ONTStatus,
    ONTSignal,
    OLTAlarm,
    NormalizedTelemetrySnapshot
)


class OLTAdapter(ABC):
    """
    Abstract Base Class defining the normalized OLT adapter contract.
    Decouples Sentinel OS business logic and UI dashboards from vendor-specific
    hardware communication protocols (SNMP, NETCONF, vendor REST APIs, CLI).
    """

    def __init__(self, device_id: str, management_ip: str, credentials: Optional[Dict[str, Any]] = None):
        self.device_id = device_id
        self.management_ip = management_ip
        self.credentials = credentials or {}
        self._connected = False

    @abstractmethod
    async def connect(self) -> bool:
        """Establish management session/socket with the target OLT device."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Safely terminate session with the target OLT device."""
        pass

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """
        Stage 1: Verify device reachability and session status.
        Distinguishes: ONLINE, DEGRADED, UNREACHABLE, AUTHENTICATION_FAILURE, TIMEOUT.
        """
        pass

    @abstractmethod
    async def get_device_info(self) -> OLTDeviceInfo:
        """Fetch OLT chassis identity, software release, uptime, and status."""
        pass

    @abstractmethod
    async def get_system_metrics(self) -> OLTSystemMetrics:
        """Stage 2: Fetch CPU, RAM, temperature, and fan/power telemetry."""
        pass

    @abstractmethod
    async def get_pon_ports(self) -> List[PONPortTelemetry]:
        """Stage 3: Enumerate PON interfaces, optical Tx power, and connected ONT count."""
        pass

    @abstractmethod
    async def get_onts(self, pon_port_id: Optional[str] = None) -> List[ONTTelemetry]:
        """Stage 4: Fetch registered ONT subscriber units, optical Rx power, and operational status."""
        pass

    @abstractmethod
    async def get_ont_status(self, ont_id: str) -> ONTStatus:
        """Retrieve operational status for a specific subscriber ONT."""
        pass

    @abstractmethod
    async def get_ont_signal(self, ont_id: str) -> Optional[ONTSignal]:
        """Retrieve optical signal telemetry for a specific subscriber ONT."""
        pass

    @abstractmethod
    async def get_alarms(self) -> List[OLTAlarm]:
        """Stage 5: Retrieve currently active alarms and optical loss events."""
        pass

    async def collect_full_snapshot(self) -> NormalizedTelemetrySnapshot:
        """
        Orchestrates full normalized telemetry collection across Stages 1–5.
        """
        dev_info = await self.get_device_info()
        sys_metrics = await self.get_system_metrics()
        pon_ports = await self.get_pon_ports()
        onts = await self.get_onts()
        alarms = await self.get_alarms()

        return NormalizedTelemetrySnapshot(
            olt=dev_info,
            system_metrics=sys_metrics,
            pon_ports=pon_ports,
            onts=onts,
            active_alarms=alarms,
            provider_type=self.__class__.__name__
        )
