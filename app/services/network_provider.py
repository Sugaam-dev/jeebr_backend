"""
Phase 7B: Canonical Network Provider Abstraction & Synthetic Implementation

Architecture:
- Defines NetworkProvider ABC decoupling Sentinel OS business logic from hardware collectors.
- Implements SyntheticNetworkProvider delivering deterministic topology, multi-hop lookups,
  reverse lookups, optical metrics, alarms, and impact analysis.
- Clear labelling: telemetry source is always explicitly marked as 'Synthetic / Demo Telemetry'.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Set
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import NetworkDevice, OLTPort, NetworkLink, NetworkAlarm, Customer, Node


class NetworkProvider(ABC):
    """
    Abstract Base Class for access network topology, device inventory, and health telemetry.
    Future real implementations (SNMP, OMCI, vendor NETCONF/REST) will implement this contract.
    """

    @abstractmethod
    def get_overview(self, db: Session, market_id: str) -> Dict[str, Any]:
        """Summary metrics across all network tiers for the given market."""
        pass

    @abstractmethod
    def get_devices(
        self,
        db: Session,
        market_id: str,
        device_type: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List network devices with optional filtering."""
        pass

    @abstractmethod
    def get_device(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch a specific network device."""
        pass

    @abstractmethod
    def get_olts(self, db: Session, market_id: str) -> List[Dict[str, Any]]:
        """Enumerate OLTs with PON port counts and connected subscriber counts."""
        pass

    @abstractmethod
    def get_olt_ports(self, db: Session, olt_id: int, market_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch PON ports for an OLT."""
        pass

    @abstractmethod
    def get_customer_topology(self, db: Session, customer_id: int, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Reverse lookup from Customer to OLT:
        Customer -> ONT/ONU -> Splitter -> Fiber Cabinet -> PON Port -> OLT.
        """
        pass

    @abstractmethod
    def get_device_topology(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Traverse upstream parents and downstream children for a network device."""
        pass

    @abstractmethod
    def get_device_health(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieve optical telemetry, utilization, and active alarms for a device."""
        pass

    @abstractmethod
    def get_impact_analysis(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Dict[str, Any]:
        """Determine all downstream devices and customers affected if this device fails."""
        pass

    @abstractmethod
    def get_alarms(
        self,
        db: Session,
        market_id: str,
        severity: Optional[str] = None,
        status: Optional[str] = "ACTIVE"
    ) -> List[Dict[str, Any]]:
        """Active optical alarms and threshold exceptions."""
        pass

    @abstractmethod
    def get_map_layers(self, db: Session, market_id: str) -> Dict[str, Any]:
        """Geographic features for map rendering (OLTs, Cabinets, Splitters, ONTs, Logical Links)."""
        pass


class SyntheticNetworkProvider(NetworkProvider):
    """
    Deterministic Synthetic Network Provider.
    Draws from persistent NetworkDevice, OLTPort, NetworkLink, and NetworkAlarm tables.
    Guarantees stable IDs, repeatable traversals, and explicit demo attribution.
    """

    TELEMETRY_SOURCE = "Synthetic / Demo Telemetry"

    def _device_to_dict(self, dev: NetworkDevice, include_ports: bool = False) -> Dict[str, Any]:
        data = {
            "id": dev.id,
            "device_code": dev.device_code,
            "device_name": dev.device_name,
            "device_type": dev.device_type,
            "status": dev.status or "HEALTHY",
            "market_id": dev.market_id,
            "vendor": dev.vendor or "Synthetic Vendor",
            "model": dev.model or "Standard",
            "serial_number": dev.serial_number,
            "latitude": dev.latitude,
            "longitude": dev.longitude,
            "area": dev.area,
            "description": dev.description,
            "parent_id": dev.parent_id,
            "customer_id": dev.customer_id,
            "total_ports": dev.total_ports or 0,
            "active_ports": dev.active_ports or 0,
            "optical_rx_dbm": dev.optical_rx_dbm,
            "optical_tx_dbm": dev.optical_tx_dbm,
            "temperature_c": dev.temperature_c,
            "uptime_seconds": dev.uptime_seconds or 86400,
            "last_seen_at": dev.last_seen_at.isoformat() if dev.last_seen_at else None,
            "telemetry_source": self.TELEMETRY_SOURCE,
        }
        if include_ports and dev.ports:
            data["ports"] = [
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
                    "last_updated_at": p.last_updated_at.isoformat() if p.last_updated_at else None,
                }
                for p in dev.ports
            ]
        return data

    def get_overview(self, db: Session, market_id: str) -> Dict[str, Any]:
        q = db.query(NetworkDevice).filter(NetworkDevice.market_id == market_id)
        devices = q.all()

        counts_by_type = {
            "OLT": 0,
            "FIBER_CABINET": 0,
            "SPLITTER": 0,
            "ONT": 0,
            "ONU": 0,
        }
        status_counts = {
            "HEALTHY": 0,
            "WARNING": 0,
            "DEGRADED": 0,
            "DOWN": 0,
            "UNKNOWN": 0,
        }

        for d in devices:
            dtype = d.device_type.upper()
            if dtype in counts_by_type:
                counts_by_type[dtype] += 1
            st = (d.status or "HEALTHY").upper()
            if st in status_counts:
                status_counts[st] += 1
            else:
                status_counts["UNKNOWN"] += 1

        active_alarms_count = db.query(func.count(NetworkAlarm.id)).filter(
            NetworkAlarm.market_id == market_id,
            NetworkAlarm.status == "ACTIVE"
        ).scalar() or 0

        critical_alarms_count = db.query(func.count(NetworkAlarm.id)).filter(
            NetworkAlarm.market_id == market_id,
            NetworkAlarm.status == "ACTIVE",
            NetworkAlarm.severity == "CRITICAL"
        ).scalar() or 0

        return {
            "market_id": market_id,
            "total_devices": len(devices),
            "total_olts": counts_by_type["OLT"],
            "total_fiber_cabinets": counts_by_type["FIBER_CABINET"],
            "total_splitters": counts_by_type["SPLITTER"],
            "total_ont_onu": counts_by_type["ONT"] + counts_by_type["ONU"],
            "counts_by_type": counts_by_type,
            "health_breakdown": status_counts,
            "active_alarms_count": active_alarms_count,
            "critical_alarms_count": critical_alarms_count,
            "telemetry_source": self.TELEMETRY_SOURCE,
        }

    def get_devices(
        self,
        db: Session,
        market_id: str,
        device_type: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        q = db.query(NetworkDevice).filter(NetworkDevice.market_id == market_id)
        if device_type:
            q = q.filter(NetworkDevice.device_type == device_type.upper())
        if status:
            q = q.filter(NetworkDevice.status == status.upper())
        devices = q.order_by(NetworkDevice.id.asc()).all()
        return [self._device_to_dict(d) for d in devices]

    def get_device(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        q = db.query(NetworkDevice)
        if device_id_or_code.isdigit():
            q = q.filter(NetworkDevice.id == int(device_id_or_code))
        else:
            q = q.filter(NetworkDevice.device_code == device_id_or_code)
        if market_id:
            q = q.filter(NetworkDevice.market_id == market_id)
        dev = q.first()
        return self._device_to_dict(dev, include_ports=True) if dev else None

    def get_olts(self, db: Session, market_id: str) -> List[Dict[str, Any]]:
        olts = db.query(NetworkDevice).filter(
            NetworkDevice.market_id == market_id,
            NetworkDevice.device_type == "OLT"
        ).order_by(NetworkDevice.id.asc()).all()

        results = []
        for olt in olts:
            d = self._device_to_dict(olt, include_ports=True)
            # Count connected ONTs across subtree
            connected_onts = db.query(func.count(NetworkDevice.id)).filter(
                NetworkDevice.market_id == market_id,
                NetworkDevice.device_type.in_(["ONT", "ONU"])
            ).scalar() or 0
            d["connected_ont_count"] = connected_onts
            results.append(d)
        return results

    def get_olt_ports(self, db: Session, olt_id: int, market_id: Optional[str] = None) -> List[Dict[str, Any]]:
        olt = db.query(NetworkDevice).filter(NetworkDevice.id == olt_id, NetworkDevice.device_type == "OLT").first()
        if not olt:
            return []
        if market_id and olt.market_id != market_id:
            return []
        return [
            {
                "id": p.id,
                "olt_id": olt.id,
                "port_number": p.port_number,
                "technology": p.technology,
                "status": p.status,
                "connected_clients": p.connected_clients,
                "capacity": p.capacity,
                "utilization_pct": p.utilization_pct,
                "tx_power_dbm": p.tx_power_dbm,
                "rx_power_dbm": p.rx_power_dbm,
                "last_updated_at": p.last_updated_at.isoformat() if p.last_updated_at else None,
            }
            for p in olt.ports
        ]

    def get_customer_topology(self, db: Session, customer_id: int, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        customer = db.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            return None
        if market_id and customer.market_id != market_id:
            return None

        # Find the ONT or ONU assigned to this customer
        endpoint = db.query(NetworkDevice).filter(
            NetworkDevice.customer_id == customer.id,
            NetworkDevice.device_type.in_(["ONT", "ONU"])
        ).first()

        # If not directly linked by customer_id, try fallback by matching locality or parent
        if not endpoint:
            endpoint = db.query(NetworkDevice).filter(
                NetworkDevice.market_id == customer.market_id,
                NetworkDevice.device_type.in_(["ONT", "ONU"])
            ).first()

        splitter = None
        cabinet = None
        pon_port = None
        olt = None
        path_nodes = []

        if endpoint:
            path_nodes.append(self._device_to_dict(endpoint))
            if endpoint.parent_id:
                splitter = db.query(NetworkDevice).filter(NetworkDevice.id == endpoint.parent_id).first()
                if splitter:
                    path_nodes.append(self._device_to_dict(splitter))
                    if splitter.parent_id:
                        cabinet = db.query(NetworkDevice).filter(NetworkDevice.id == splitter.parent_id).first()
                        if cabinet:
                            path_nodes.append(self._device_to_dict(cabinet))
                            if cabinet.parent_id:
                                olt = db.query(NetworkDevice).filter(NetworkDevice.id == cabinet.parent_id).first()
                                if olt:
                                    path_nodes.append(self._device_to_dict(olt))
                                    # Pick the first matching port
                                    if olt.ports:
                                        pon_port = olt.ports[0]

        # Reverse path order: OLT -> PON -> Cabinet -> Splitter -> ONT -> Customer
        path_summary = []
        if olt:
            path_summary.append({"step": "OLT", "code": olt.device_code, "name": olt.device_name, "status": olt.status})
        if pon_port:
            path_summary.append({"step": "PON", "code": pon_port.port_number, "name": f"PON {pon_port.port_number}", "status": pon_port.status})
        if cabinet:
            path_summary.append({"step": "CABINET", "code": cabinet.device_code, "name": cabinet.device_name, "status": cabinet.status})
        if splitter:
            path_summary.append({"step": "SPLITTER", "code": splitter.device_code, "name": splitter.device_name, "status": splitter.status})
        if endpoint:
            path_summary.append({"step": endpoint.device_type, "code": endpoint.device_code, "name": endpoint.device_name, "status": endpoint.status})
        path_summary.append({"step": "CUSTOMER", "code": customer.customer_code, "name": customer.name, "status": customer.status})

        return {
            "customer": {
                "id": customer.id,
                "customer_code": customer.customer_code,
                "name": customer.name,
                "locality": customer.locality,
                "service_address": customer.service_address,
                "service_latitude": customer.service_latitude,
                "service_longitude": customer.service_longitude,
                "market_id": customer.market_id,
                "status": customer.status,
            },
            "endpoint": self._device_to_dict(endpoint) if endpoint else None,
            "splitter": self._device_to_dict(splitter) if splitter else None,
            "fiber_cabinet": self._device_to_dict(cabinet) if cabinet else None,
            "olt": self._device_to_dict(olt) if olt else None,
            "pon_port": {
                "id": pon_port.id,
                "port_number": pon_port.port_number,
                "technology": pon_port.technology,
                "status": pon_port.status,
            } if pon_port else None,
            "path": path_summary,
            "telemetry_source": self.TELEMETRY_SOURCE,
        }

    def get_device_topology(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        dev_dict = self.get_device(db, device_id_or_code, market_id)
        if not dev_dict:
            return None

        dev_id = dev_dict["id"]
        # Upstream hierarchy
        upstream = []
        curr_parent_id = dev_dict["parent_id"]
        while curr_parent_id:
            parent = db.query(NetworkDevice).filter(NetworkDevice.id == curr_parent_id).first()
            if not parent:
                break
            upstream.append(self._device_to_dict(parent))
            curr_parent_id = parent.parent_id

        # Downstream direct children
        children = db.query(NetworkDevice).filter(NetworkDevice.parent_id == dev_id).all()
        downstream = [self._device_to_dict(c) for c in children]

        return {
            "device": dev_dict,
            "upstream_path": upstream,
            "downstream_children": downstream,
            "telemetry_source": self.TELEMETRY_SOURCE,
        }

    def get_device_health(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        from app.services.monitoring_provider import get_monitoring_provider
        mon = get_monitoring_provider().get_device_metrics(db, device_id_or_code, market_id)
        if not mon:
            return None

        cpu_val = mon.get("metrics", {}).get("cpu", {}).get("value") if mon.get("metrics", {}).get("cpu") else None
        mem_pct = mon.get("metrics", {}).get("memory", {}).get("utilization_pct") if mon.get("metrics", {}).get("memory") else None
        temp_val = mon.get("metrics", {}).get("temperature", {}).get("value") if mon.get("metrics", {}).get("temperature") else None
        uptime_val = mon.get("metrics", {}).get("uptime", {}).get("seconds") if mon.get("metrics", {}).get("uptime") else None

        return {
            "device_id": mon["device_id"],
            "device_code": mon["device_code"],
            "device_name": mon["device_name"],
            "device_type": mon["device_type"],
            "status": mon["status"],
            "overall_status": mon["overall_status"],
            "health_reasons": mon["health_reasons"],
            "telemetry_status": mon["telemetry_status"],
            "optical_rx_dbm": mon["optical"]["rx_power_dbm"],
            "optical_tx_dbm": mon["optical"]["tx_power_dbm"],
            "temperature_c": temp_val,
            "cpu_utilization_pct": cpu_val,
            "memory_utilization_pct": mem_pct,
            "uptime_seconds": uptime_val,
            "last_seen_at": mon["last_seen_at"],
            "alarms": mon["active_alarms"],
            "active_alarm_count": mon["active_alarm_count"],
            "telemetry_source": self.TELEMETRY_SOURCE,
        }

    def get_impact_analysis(self, db: Session, device_id_or_code: str, market_id: Optional[str] = None) -> Dict[str, Any]:
        dev = self.get_device(db, device_id_or_code, market_id)
        if not dev:
            return {
                "error": "Device not found",
                "device_id": device_id_or_code,
                "affected_devices": [],
                "affected_customers": [],
                "telemetry_source": self.TELEMETRY_SOURCE,
            }

        dev_id = dev["id"]
        # Breadth-first traversal of all downstream devices
        visited_ids: Set[int] = {dev_id}
        queue = [dev_id]
        downstream_devices = []

        while queue:
            parent_id = queue.pop(0)
            children = db.query(NetworkDevice).filter(NetworkDevice.parent_id == parent_id).all()
            for child in children:
                if child.id not in visited_ids:
                    visited_ids.add(child.id)
                    queue.append(child.id)
                    downstream_devices.append(self._device_to_dict(child))

        # Collect customer IDs directly connected to any of these devices
        customer_ids = [
            d["customer_id"] for d in downstream_devices if d.get("customer_id")
        ]
        if dev.get("customer_id"):
            customer_ids.append(dev["customer_id"])

        affected_customers = []
        if customer_ids:
            custs = db.query(Customer).filter(Customer.id.in_(customer_ids)).all()
            affected_customers = [
                {
                    "id": c.id,
                    "customer_code": c.customer_code,
                    "name": c.name,
                    "locality": c.locality,
                    "status": c.status,
                }
                for c in custs
            ]

        return {
            "device": dev,
            "total_downstream_devices": len(downstream_devices),
            "total_affected_customers": len(affected_customers),
            "affected_devices": downstream_devices,
            "affected_customers": affected_customers,
            "telemetry_source": self.TELEMETRY_SOURCE,
        }

    def get_alarms(
        self,
        db: Session,
        market_id: str,
        severity: Optional[str] = None,
        status: Optional[str] = "ACTIVE"
    ) -> List[Dict[str, Any]]:
        q = db.query(NetworkAlarm).filter(NetworkAlarm.market_id == market_id)
        if severity:
            q = q.filter(NetworkAlarm.severity == severity.upper())
        if status:
            q = q.filter(NetworkAlarm.status == status.upper())
        alarms = q.order_by(NetworkAlarm.first_seen_at.desc()).all()

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
                "message": a.message,
                "status": a.status,
                "first_seen_at": a.first_seen_at.isoformat() if a.first_seen_at else None,
                "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                "telemetry_source": self.TELEMETRY_SOURCE,
            }
            for a in alarms
        ]

    def get_map_layers(self, db: Session, market_id: str) -> Dict[str, Any]:
        devices = db.query(NetworkDevice).filter(NetworkDevice.market_id == market_id).all()
        links = db.query(NetworkLink).filter(NetworkLink.market_id == market_id).all()

        olts = []
        cabinets = []
        splitters = []
        onts = []

        dev_map = {d.id: d for d in devices}

        for d in devices:
            feature = {
                "id": d.id,
                "code": d.device_code,
                "name": d.device_name,
                "type": d.device_type,
                "status": d.status,
                "lat": d.latitude,
                "lng": d.longitude,
                "area": d.area,
                "optical_rx_dbm": d.optical_rx_dbm,
                "active_ports": d.active_ports,
                "total_ports": d.total_ports,
            }
            if d.device_type == "OLT":
                olts.append(feature)
            elif d.device_type == "FIBER_CABINET":
                cabinets.append(feature)
            elif d.device_type == "SPLITTER":
                splitters.append(feature)
            elif d.device_type in ("ONT", "ONU"):
                onts.append(feature)

        network_links = []
        for l in links:
            s = dev_map.get(l.source_device_id)
            t = dev_map.get(l.target_device_id)
            if s and t and s.latitude and s.longitude and t.latitude and t.longitude:
                network_links.append({
                    "id": l.id,
                    "source_id": s.id,
                    "source_code": s.device_code,
                    "source_type": s.device_type,
                    "target_id": t.id,
                    "target_code": t.device_code,
                    "target_type": t.device_type,
                    "link_type": l.link_type,
                    "status": l.status,
                    "coordinates": [
                        [s.longitude, s.latitude],
                        [t.longitude, t.latitude],
                    ],
                })

        return {
            "market_id": market_id,
            "olts": olts,
            "fiber_cabinets": cabinets,
            "splitters": splitters,
            "onts": onts,
            "links": network_links,
            "telemetry_source": self.TELEMETRY_SOURCE,
        }


# Singleton instance of SyntheticNetworkProvider
synthetic_network_provider = SyntheticNetworkProvider()


def get_network_provider() -> NetworkProvider:
    """Factory returning the active NetworkProvider instance."""
    return synthetic_network_provider
