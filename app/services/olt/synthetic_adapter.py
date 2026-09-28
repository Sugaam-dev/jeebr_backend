from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Node, Customer, Ticket
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


class SyntheticOLTAdapter(OLTAdapter):
    """
    Synthetic OLT Telemetry Provider (Section 22).
    Preserves existing development/POC telemetry generation backed by PostgreSQL Node
    and Customer records, providing 100% backward compatibility for local testing and demos.
    """

    def __init__(self, device_id: str = "OLT-MUM-001", management_ip: str = "10.120.1.1", credentials: Optional[Dict[str, Any]] = None):
        super().__init__(device_id=device_id, management_ip=management_ip, credentials=credentials)
        self._connected = True

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def health_check(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "status": OLTStatus.ONLINE.value,
            "management_ip": self.management_ip,
            "latency_ms": 1.2,
            "packet_loss_pct": 0.0,
            "reachable": True,
            "provider": "SyntheticOLTAdapter"
        }

    async def get_device_info(self) -> OLTDeviceInfo:
        db: Session = SessionLocal()
        try:
            node = db.query(Node).filter(Node.node_code == self.device_id).first()
            if not node:
                node = db.query(Node).filter(Node.node_type == "OLT").first()

            name = node.node_name if node else "Central OLT - Bandra Hub"
            market = node.market_id if node else "mumbai"
            status = OLTStatus.ONLINE if (not node or node.status == "Healthy") else OLTStatus.DEGRADED

            return OLTDeviceInfo(
                id=self.device_id,
                name=name,
                vendor="Simulated-Huawei-SmartAX",
                model="MA5800-X17",
                firmware_version="V100R019C10SPC200",
                management_ip=self.management_ip,
                status=status,
                uptime_seconds=864000,
                pon_ports_count=16,
                market_id=market
            )
        finally:
            db.close()

    async def get_system_metrics(self) -> OLTSystemMetrics:
        db: Session = SessionLocal()
        try:
            node = db.query(Node).filter(Node.node_code == self.device_id).first()
            util = node.utilization_pct if node else 48.5
            temp = 42.0 + (util * 0.15)

            return OLTSystemMetrics(
                olt_id=self.device_id,
                cpu_utilization_pct=min(99.0, util * 0.8),
                memory_utilization_pct=min(95.0, 35.0 + (util * 0.4)),
                temperature_celsius=round(temp, 1),
                fan_status="NORMAL" if temp < 65.0 else "HIGH_SPEED",
                power_supply_status="REDUNDANT_OK"
            )
        finally:
            db.close()

    async def get_pon_ports(self) -> List[PONPortTelemetry]:
        db: Session = SessionLocal()
        try:
            node = db.query(Node).filter(Node.node_code == self.device_id).first()
            market = node.market_id if node else "mumbai"
            customers = db.query(Customer).filter(Customer.market_id == market).all()
            total_cust = len(customers)

            ports = []
            for i in range(1, 9):
                port_id = f"0/1/{i}"
                assigned_onts = total_cust // 8 if total_cust >= 8 else (1 if i <= total_cust else 0)
                ports.append(
                    PONPortTelemetry(
                        olt_id=self.device_id,
                        port_id=port_id,
                        port_name=f"GPON-0/1/{i}",
                        technology="GPON",
                        admin_status="UP",
                        operational_status="UP",
                        ont_count_online=assigned_onts,
                        ont_count_total=assigned_onts,
                        tx_power_dbm=3.8,
                        rx_power_dbm=-20.5 + (i * 0.2)
                    )
                )
            return ports
        finally:
            db.close()

    async def get_onts(self, pon_port_id: Optional[str] = None) -> List[ONTTelemetry]:
        db: Session = SessionLocal()
        try:
            node = db.query(Node).filter(Node.node_code == self.device_id).first()
            market = node.market_id if node else "mumbai"
            node_opt_power = node.optical_power_dbm if node else -19.5

            customers = db.query(Customer).filter(Customer.market_id == market).limit(20).all()
            ont_list = []

            for idx, cust in enumerate(customers):
                port = f"0/1/{(idx % 8) + 1}"
                if pon_port_id and port != pon_port_id:
                    continue

                ont_rx = round(node_opt_power - (0.5 * (idx % 4)), 1)
                ont_status = ONTStatus.ONLINE
                if ont_rx < -26.5:
                    ont_status = ONTStatus.DEGRADED
                if ont_rx < -28.5:
                    ont_status = ONTStatus.LOS

                ont_list.append(
                    ONTTelemetry(
                        olt_id=self.device_id,
                        pon_port_id=port,
                        ont_id=f"ONT-{cust.id:04d}",
                        serial_number=f"HWTC{cust.customer_code.replace('-', '')[-8:]}",
                        customer_id=cust.id,
                        customer_name=cust.name,
                        status=ont_status,
                        signal=ONTSignal(
                            rx_power_dbm=ont_rx,
                            tx_power_dbm=2.4,
                            olt_rx_power_dbm=-21.2,
                            ber_rate=0.00001 if ont_status == ONTStatus.ONLINE else 0.002,
                            snr_db=34.0 if ont_status == ONTStatus.ONLINE else 22.0
                        ),
                        distance_meters=850 + (idx * 120)
                    )
                )
            return ont_list
        finally:
            db.close()

    async def get_ont_status(self, ont_id: str) -> ONTStatus:
        onts = await self.get_onts()
        target = next((o for o in onts if o.ont_id == ont_id), None)
        return target.status if target else ONTStatus.OFFLINE

    async def get_ont_signal(self, ont_id: str) -> Optional[ONTSignal]:
        onts = await self.get_onts()
        target = next((o for o in onts if o.ont_id == ont_id), None)
        return target.signal if target else None

    async def get_alarms(self) -> List[OLTAlarm]:
        db: Session = SessionLocal()
        try:
            node = db.query(Node).filter(Node.node_code == self.device_id).first()
            alarms = []

            if node and node.optical_power_dbm < -26.5:
                alarms.append(
                    OLTAlarm(
                        id=f"ALM-{self.device_id}-LOS-01",
                        olt_id=self.device_id,
                        severity=AlarmSeverity.CRITICAL,
                        source=f"PON-Port-0/1/1",
                        code="OPTICAL_POWER_ATTENUATION",
                        message=f"Severe optical loss detected: {node.optical_power_dbm:.1f} dBm (Threshold: -26.5 dBm)",
                        occurred_at=datetime.utcnow() - timedelta(minutes=45),
                        is_active=True
                    )
                )

            if node and node.utilization_pct > 75.0:
                alarms.append(
                    OLTAlarm(
                        id=f"ALM-{self.device_id}-UTIL-02",
                        olt_id=self.device_id,
                        severity=AlarmSeverity.MAJOR,
                        source="Uplink-Port-10GE-0/0/1",
                        code="BANDWIDTH_SATURATION",
                        message=f"Uplink port bandwidth saturation at {node.utilization_pct:.1f}%",
                        occurred_at=datetime.utcnow() - timedelta(minutes=15),
                        is_active=True
                    )
                )

            return alarms
        finally:
            db.close()
