"""
Phase 7B: OLT / ONT / ONU Network Topology & Health REST API Router

Endpoints:
  GET /api/network/overview
  GET /api/network/devices
  GET /api/network/devices/{device_id_or_code}
  GET /api/network/devices/{device_id_or_code}/health
  GET /api/network/olts
  GET /api/network/olts/{olt_id}/ports
  GET /api/network/cabinets
  GET /api/network/splitters
  GET /api/network/topology/customer/{customer_id}
  GET /api/network/topology/device/{device_id_or_code}
  GET /api/network/impact/{device_id_or_code}
  GET /api/network/alarms
  GET /api/network/map-layers

RBAC: Restricted to SUPER_ADMIN, Admin, and NOC.
Tenant/Market isolation enforced authoritative server-side via X-Market-Id.
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import require_roles
from app.markets import get_current_market
from app.services.network_provider import get_network_provider
from app.services.monitoring_provider import get_monitoring_provider

router = APIRouter(prefix="/network", tags=["Network Topology & Optical Health"])

OPERATIONAL_ROLES = ["SUPER_ADMIN", "Admin", "NOC"]
ADMIN_ROLES = ["SUPER_ADMIN", "Admin"]


class ThresholdUpdateRequest(BaseModel):
    warning_threshold: float = Field(..., ge=0, description="Warning threshold limit")
    critical_threshold: float = Field(..., ge=0, description="Critical threshold limit")


class SimulationRequest(BaseModel):
    scenario: str = Field(..., description="HEALTHY, WARNING, CRITICAL, DOWN")


@router.get("/overview")
def get_network_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    High-level infrastructure overview: Total OLTs, Cabinets, Splitters, ONTs,
    health breakdown, and active optical alarms.
    """
    provider = get_network_provider()
    return provider.get_overview(db, market_id=market)


@router.get("/devices")
def list_network_devices(
    device_type: Optional[str] = Query(None, description="OLT, FIBER_CABINET, SPLITTER, ONT, ONU"),
    status: Optional[str] = Query(None, description="HEALTHY, WARNING, DEGRADED, DOWN, UNKNOWN"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List network devices in current market, optionally filtered by type or status.
    """
    provider = get_network_provider()
    return provider.get_devices(db, market_id=market, device_type=device_type, status=status)


@router.get("/olts")
def list_olts(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List all OLTs in current market with PON port counts and connected subscriber units.
    """
    provider = get_network_provider()
    return provider.get_olts(db, market_id=market)


@router.get("/olts/{olt_id}/ports")
def get_olt_ports(
    olt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List PON interfaces on a specific OLT chassis.
    """
    provider = get_network_provider()
    ports = provider.get_olt_ports(db, olt_id=olt_id, market_id=market)
    if not ports:
        # Check if OLT exists in another market (cross-market isolation)
        any_olt = provider.get_device(db, str(olt_id))
        if any_olt and any_olt["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: OLT {olt_id} belongs to another regional market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"OLT {olt_id} not found."
        )
    return ports


@router.get("/cabinets")
def list_fiber_cabinets(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List fiber distribution cabinets / FDHs in the current market.
    """
    provider = get_network_provider()
    return provider.get_devices(db, market_id=market, device_type="FIBER_CABINET")


@router.get("/splitters")
def list_splitters(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List optical splitters in the current market.
    """
    provider = get_network_provider()
    return provider.get_devices(db, market_id=market, device_type="SPLITTER")


@router.get("/topology/customer/{customer_id}")
def get_customer_network_topology(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Reverse topology lookup:
    Customer -> ONT/ONU -> Splitter -> Fiber Cabinet -> PON Port -> OLT.
    Enforces strict market isolation: returns 403 if customer belongs to another market.
    """
    provider = get_network_provider()
    topology = provider.get_customer_topology(db, customer_id=customer_id, market_id=market)
    if not topology:
        # Check if customer exists in another market
        other_top = provider.get_customer_topology(db, customer_id=customer_id, market_id=None)
        if other_top and other_top["customer"]["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Customer {customer_id} belongs to a different regional market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer {customer_id} topology not found."
        )
    return topology


@router.get("/topology/device/{device_id_or_code}")
def get_device_network_topology(
    device_id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Traverse upstream path and downstream children for a network device.
    """
    provider = get_network_provider()
    top = provider.get_device_topology(db, device_id_or_code=device_id_or_code, market_id=market)
    if not top:
        other_dev = provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another regional market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id_or_code} not found."
        )
    return top


@router.get("/devices/{device_id_or_code}/health")
def get_device_health(
    device_id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Retrieve device optical health, Tx/Rx powers, temperature, uptime, and active alarms.
    """
    provider = get_network_provider()
    health = provider.get_device_health(db, device_id_or_code=device_id_or_code, market_id=market)
    if not health:
        other_dev = provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id_or_code} not found."
        )
    return health


@router.get("/impact/{device_id_or_code}")
def get_device_impact_analysis(
    device_id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Deterministic topology traversal: Returns all downstream devices and customers
    impacted if the specified network device experiences degradation or failure.
    """
    provider = get_network_provider()
    impact = provider.get_impact_analysis(db, device_id_or_code=device_id_or_code, market_id=market)
    if impact.get("error"):
        other_dev = provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id_or_code} not found."
        )
    return impact


@router.get("/alarms")
def list_network_alarms(
    severity: Optional[str] = Query(None, description="INFO, WARNING, CRITICAL"),
    status: Optional[str] = Query("ACTIVE", description="ACTIVE, CLEARED, ACKNOWLEDGED"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List optical loss of signal, attenuation, and capacity alarms in current market.
    """
    provider = get_network_provider()
    return provider.get_alarms(db, market_id=market, severity=severity, status=status)


@router.get("/map-layers")
def get_network_map_layers(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Geographic features for NOC Live Map rendering:
    OLTs, Fiber Cabinets, Splitters, ONTs, and Logical Network Links.
    """
    provider = get_network_provider()
    return provider.get_map_layers(db, market_id=market)


@router.get("/devices/{device_id_or_code}")
def get_single_device(
    device_id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Get full record for a single network device.
    """
    provider = get_network_provider()
    dev = provider.get_device(db, device_id_or_code=device_id_or_code, market_id=market)
    if not dev:
        other_dev = provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another regional market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id_or_code} not found."
        )
    return dev


# ============================================================================
# PHASE 8: System & Network Health Monitoring Endpoints
# ============================================================================

@router.get("/monitoring/overview")
def get_monitoring_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Carrier-grade health status overview: breakdown of OLTs, ONTs, PONs, and active alarms.
    """
    provider = get_monitoring_provider()
    return provider.get_monitoring_overview(db, market_id=market)


@router.get("/monitoring/thresholds")
def list_monitoring_thresholds(
    device_type: Optional[str] = Query(None, description="OLT, ONT, ONU, FIBER_CABINET, ALL"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List configured alert thresholds per device type and metric.
    """
    provider = get_monitoring_provider()
    return provider.get_thresholds(db, market_id=market, device_type=device_type)


@router.put("/monitoring/thresholds/{threshold_id}")
def update_monitoring_threshold(
    threshold_id: int,
    req: ThresholdUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(ADMIN_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Update metric warning and critical thresholds.
    Restricted to SUPER_ADMIN and Admin roles.
    """
    provider = get_monitoring_provider()
    try:
        return provider.update_threshold(
            db,
            threshold_id=threshold_id,
            warning_threshold=req.warning_threshold,
            critical_threshold=req.critical_threshold,
            user_id=current_user.id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/monitoring/alarms")
def list_monitoring_alarms(
    severity: Optional[str] = Query(None, description="INFO, WARNING, CRITICAL"),
    status: Optional[str] = Query("ACTIVE", description="ACTIVE, CLEARED, ACKNOWLEDGED"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> List[Dict[str, Any]]:
    """
    List active monitoring alarms for CPU, memory, temperature, and link faults.
    """
    provider = get_monitoring_provider()
    return provider.get_monitoring_alarms(db, market_id=market, severity=severity, status=status)


@router.get("/devices/{device_id_or_code}/metrics")
def get_device_monitoring_metrics(
    device_id_or_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Detailed current monitoring payload:
    CPU %, Memory (used/avail/%), Temperature, Uptime, PON utilization, Optical Rx/Tx,
    backend-calculated overall_status, health_reasons, and telemetry freshness.
    """
    provider = get_monitoring_provider()
    metrics = provider.get_device_metrics(db, device_id_or_code=device_id_or_code, market_id=market)
    if not metrics:
        net_provider = get_network_provider()
        other_dev = net_provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id_or_code} not found."
        )
    return metrics


@router.get("/devices/{device_id_or_code}/metrics/history")
def get_device_metric_history(
    device_id_or_code: str,
    time_range: Optional[str] = Query("1h", description="1h, 6h, 24h"),
    metric_type: Optional[str] = Query(None, description="Optional metric filter"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(OPERATIONAL_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Retrieve historical time-series data points for charts.
    """
    provider = get_monitoring_provider()
    history = provider.get_metric_history(
        db,
        device_id_or_code=device_id_or_code,
        time_range=time_range or "1h",
        metric_type=metric_type,
        market_id=market
    )
    if not history:
        net_provider = get_network_provider()
        other_dev = net_provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another market."
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id_or_code} not found."
        )
    return history


@router.post("/devices/{device_id_or_code}/simulate")
def simulate_device_health_scenario(
    device_id_or_code: str,
    req: SimulationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(ADMIN_ROLES)),
    market: str = Depends(get_current_market)
) -> Dict[str, Any]:
    """
    Safe test simulation endpoint for verifying alarm lifecycle & health transitions.
    Restricted to SUPER_ADMIN and Admin roles.
    Supported scenarios: HEALTHY, WARNING, CRITICAL, DOWN.
    """
    provider = get_monitoring_provider()
    try:
        return provider.simulate_device_scenario(
            db,
            device_id_or_code=device_id_or_code,
            scenario=req.scenario,
            market_id=market
        )
    except ValueError as e:
        net_provider = get_network_provider()
        other_dev = net_provider.get_device(db, device_id_or_code=device_id_or_code, market_id=None)
        if other_dev and other_dev["market_id"] != market:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Device {device_id_or_code} belongs to another market."
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

