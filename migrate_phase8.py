"""
Phase 8 Database Schema Migration & Seeding Script
"""

from sqlalchemy import text
from app.database import engine, SessionLocal, Base
from app.models import NetworkDevice, DeviceMetricThreshold
from app.services.monitoring_provider import DEFAULT_THRESHOLDS

def migrate_and_seed():
    print("Beginning Phase 8 schema migration...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Seed Phase 8 Default Metric Alert Thresholds
        thresh_count = db.query(DeviceMetricThreshold).count()
        print(f"Current threshold count: {thresh_count}")
        if thresh_count == 0:
            for th_def in DEFAULT_THRESHOLDS:
                th = DeviceMetricThreshold(
                    market_id="all",
                    device_type=th_def["device_type"],
                    metric_type=th_def["metric_type"],
                    warning_threshold=th_def["warning"],
                    critical_threshold=th_def["critical"],
                    unit=th_def["unit"],
                    enabled=True
                )
                db.add(th)
            db.commit()
            print(f"Seeded {len(DEFAULT_THRESHOLDS)} default thresholds.")

        # Update OLT devices with initial metrics if null
        olts = db.query(NetworkDevice).filter(NetworkDevice.device_type == "OLT").all()
        for olt in olts:
            if olt.cpu_utilization_pct is None:
                st = olt.status or "HEALTHY"
                olt.cpu_utilization_pct = 48.5 if st == "HEALTHY" else (78.0 if st == "WARNING" else 88.5)
                olt.memory_utilization_pct = 58.0 if st == "HEALTHY" else (74.0 if st == "WARNING" else 89.0)
                olt.memory_total_gb = 16.0
                olt.memory_used_gb = round(16.0 * (olt.memory_utilization_pct / 100.0), 1)
                olt.memory_available_gb = round(16.0 - olt.memory_used_gb, 1)
                olt.uptime_seconds = 86400 * 14
        db.commit()
        print(f"Updated {len(olts)} OLTs with baseline metrics.")
    finally:
        db.close()

if __name__ == "__main__":
    migrate_and_seed()
