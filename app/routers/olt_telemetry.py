from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User
from app.auth import get_current_user, require_roles, require_not_viewer
from app.markets import get_current_market
from app.services.olt import (
    OLTDeviceInfo,
    OLTSystemMetrics,
    PONPortTelemetry,
    ONTTelemetry,
    OLTAlarm,
    NormalizedTelemetrySnapshot,
    get_olt_adapter,
    OLTCollectorService
)

router = APIRouter(prefix="/olt", tags=["OLT Integration & Network Telemetry"])


@router.get("/health", response_model=Dict[str, Any])
async def get_olt_health(
    device_id: str = Query("OLT-MUM-001"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Executive", "Care"])),
    market: str = Depends(get_current_market)
):
    """
    Stage 1: Verify OLT reachability and connectivity status.
    Accessible to operational personnel (NOC, Admin, Executive, Care).
    """
    adapter = get_olt_adapter(device_id=device_id)
    return await adapter.health_check()


@router.get("/devices/{device_id}/telemetry", response_model=NormalizedTelemetrySnapshot)
async def get_device_telemetry(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Executive", "Care"])),
    market: str = Depends(get_current_market)
):
    """
    Retrieve full multi-stage normalized telemetry snapshot (Stages 1-5) for an OLT device.
    """
    try:
        return await OLTCollectorService.collect_device_snapshot(device_id)
    except TimeoutError as te:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"OLT Query Timeout: {str(te)}")
    except PermissionError as pe:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"OLT Authentication Failure: {str(pe)}")
    except ConnectionRefusedError as ce:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"OLT Unreachable: {str(ce)}")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Telemetry collection failed: {str(e)}")


@router.get("/devices/{device_id}/system-metrics", response_model=OLTSystemMetrics)
async def get_system_metrics(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Executive"])),
    market: str = Depends(get_current_market)
):
    """
    Stage 2: Device hardware telemetry (CPU, RAM, Temperature, Fan, Power).
    """
    adapter = get_olt_adapter(device_id=device_id)
    return await adapter.get_system_metrics()


@router.get("/devices/{device_id}/pon-ports", response_model=List[PONPortTelemetry])
async def get_pon_ports(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Executive"])),
    market: str = Depends(get_current_market)
):
    """
    Stage 3: PON Port optical interfaces, operational statuses, and Tx/Rx powers.
    """
    adapter = get_olt_adapter(device_id=device_id)
    return await adapter.get_pon_ports()


@router.get("/devices/{device_id}/onts", response_model=List[ONTTelemetry])
async def get_ont_subscribers(
    device_id: str,
    pon_port_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Executive"])),
    market: str = Depends(get_current_market)
):
    """
    Stage 4: Subscriber ONT devices registered to this OLT, with individual optical light levels.
    """
    adapter = get_olt_adapter(device_id=device_id)
    return await adapter.get_onts(pon_port_id=pon_port_id)


@router.get("/devices/{device_id}/alarms", response_model=List[OLTAlarm])
async def get_active_alarms(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin", "Executive"])),
    market: str = Depends(get_current_market)
):
    """
    Stage 5: Active alarms and optical loss warnings.
    """
    adapter = get_olt_adapter(device_id=device_id)
    return await adapter.get_alarms()


@router.post("/devices/{device_id}/collect", response_model=NormalizedTelemetrySnapshot)
async def trigger_telemetry_collection(
    device_id: str,
    provider: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["NOC", "Admin"])),
    market: str = Depends(get_current_market)
):
    """
    Triggers an immediate normalized poll of the OLT device and updates network health.
    Restricted to NOC and Admin operators.
    """
    try:
        return await OLTCollectorService.collect_device_snapshot(device_id, provider_override=provider)
    except TimeoutError as te:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=f"OLT Timeout: {str(te)}")
    except PermissionError as pe:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"OLT Auth Failure: {str(pe)}")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Poll failed: {str(e)}")
