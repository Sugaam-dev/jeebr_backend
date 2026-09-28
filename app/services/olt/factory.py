from typing import Optional, Dict, Any
from app.config import settings
from app.services.olt.base import OLTAdapter
from app.services.olt.synthetic_adapter import SyntheticOLTAdapter
from app.services.olt.mock_snmp_adapter import MockSNMPOLTAdapter


def get_olt_adapter(
    device_id: str = "OLT-MUM-001",
    management_ip: Optional[str] = None,
    credentials: Optional[Dict[str, Any]] = None,
    provider_override: Optional[str] = None
) -> OLTAdapter:
    """
    Factory function returning the configured OLT Adapter instance.
    Switches between SyntheticOLTAdapter, MockSNMPOLTAdapter, or future real hardware collectors
    via the OLT_PROVIDER setting or explicit parameter.
    """
    provider = (provider_override or getattr(settings, "OLT_PROVIDER", "synthetic")).lower().strip()
    ip = management_ip or ("10.120.1.1" if provider == "synthetic" else "192.168.100.1")

    if provider in ("snmp", "snmp_mock", "mock"):
        return MockSNMPOLTAdapter(
            device_id=device_id,
            management_ip=ip,
            credentials=credentials
        )
    
    # Default to synthetic provider preserving complete backward compatibility
    return SyntheticOLTAdapter(
        device_id=device_id,
        management_ip=ip,
        credentials=credentials
    )
