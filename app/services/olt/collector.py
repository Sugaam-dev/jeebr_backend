import logging
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.config import settings
from app.database import SessionLocal
from app.models import Node
from app.services.olt.factory import get_olt_adapter
from app.services.olt.models import NormalizedTelemetrySnapshot, OLTStatus

logger = logging.getLogger(__name__)


class OLTCollectorService:
    """
    Normalized OLT Telemetry Collector (Section 26 & 27).
    Fetches raw telemetry via the configured OLTAdapter, validates/normalizes it,
    synchronizes SentinelOS Node records, and logs collection health events safely.
    """

    @classmethod
    async def collect_device_snapshot(
        cls,
        device_id: str,
        provider_override: Optional[str] = None
    ) -> NormalizedTelemetrySnapshot:
        adapter = get_olt_adapter(device_id=device_id, provider_override=provider_override)

        logger.info(f"[OLT_COLLECT] Initiating normalized telemetry poll for device '{device_id}'")
        try:
            snapshot = await adapter.collect_full_snapshot()
            logger.info(
                f"[OLT_COLLECT_SUCCESS] Polled device '{device_id}' via {snapshot.provider_type}: "
                f"Status={snapshot.olt.status.value}, "
                f"Ports={len(snapshot.pon_ports)}, "
                f"ONTs={len(snapshot.onts)}, "
                f"Alarms={len(snapshot.active_alarms)}"
            )

            # Synchronize normalized values back into PostgreSQL Node entity if it exists
            cls._sync_to_node_db(snapshot)

            return snapshot

        except TimeoutError as te:
            logger.error(f"[OLT_COLLECT_TIMEOUT] Device '{device_id}' timed out during telemetry poll: {te}")
            raise
        except PermissionError as pe:
            logger.error(f"[OLT_COLLECT_AUTH_FAIL] Device '{device_id}' authentication failed during poll: {pe}")
            raise
        except Exception as e:
            logger.error(f"[OLT_COLLECT_ERROR] Unexpected error polling device '{device_id}': {e}")
            raise

    @classmethod
    def _sync_to_node_db(cls, snapshot: NormalizedTelemetrySnapshot) -> None:
        """
        Updates database Node record to align with newly polled normalized telemetry.
        """
        db: Session = SessionLocal()
        try:
            node = db.query(Node).filter(Node.node_code == snapshot.olt.id).first()
            if not node:
                # Fallback to match first OLT node if exact ID is generic
                node = db.query(Node).filter(Node.node_type == "OLT").first()

            if node:
                node.utilization_pct = snapshot.system_metrics.cpu_utilization_pct
                node.last_telemetry_at = snapshot.collected_at
                node.alarm_count = len(snapshot.active_alarms)

                if snapshot.olt.status == OLTStatus.ONLINE:
                    node.status = "Healthy" if not snapshot.active_alarms else "Degraded"
                elif snapshot.olt.status == OLTStatus.DEGRADED:
                    node.status = "Degraded"
                elif snapshot.olt.status in (OLTStatus.UNREACHABLE, OLTStatus.TIMEOUT, OLTStatus.AUTH_FAILURE):
                    node.status = "Critical"

                if snapshot.onts:
                    avg_rx = sum(o.signal.rx_power_dbm for o in snapshot.onts) / len(snapshot.onts)
                    node.optical_power_dbm = round(avg_rx, 2)

                db.commit()
                logger.debug(f"[OLT_DB_SYNC] Synced Node '{node.node_code}' to health status '{node.status}'")
        except Exception as err:
            logger.warning(f"[OLT_DB_SYNC_WARN] Failed syncing to node DB: {err}")
        finally:
            db.close()
