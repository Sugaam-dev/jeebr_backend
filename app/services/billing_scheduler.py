"""
app/services/billing_scheduler.py

SentinelOS Automated Recurring Billing & Dunning Scheduler.
Provides durable, automated execution for:
1. Daily recurring billing runs (invoice generation for due subscriptions).
2. Dunning and overdue evaluations (aging categorization, reminders, grace period enforcement).
3. Distributed execution locks to prevent overlapping worker instances.
4. Durable job execution audit logs with start/finish timestamps, attempt counts, and metrics.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models_finance import BillingJobRun
from app.services.billing_service import run_billing_cycle, evaluate_overdue_and_dunning
from app.services.notification_service import enqueue_notification

logger = logging.getLogger("sentinel.billing_scheduler")


_LOCAL_LOCKS: Dict[str, float] = {}


class SchedulerDistributedLock:
    """
    Distributed lock using Redis when configured, with safe local fallback.
    Prevents concurrent workers from executing the same billing cycle.
    """

    def __init__(self, lock_key: str, ttl_seconds: int = 1800):
        self.lock_key = f"lock:scheduler:{lock_key}"
        self.ttl = ttl_seconds
        self._acquired = False

    def acquire(self) -> bool:
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                # SETNX with TTL
                res = r.set(self.lock_key, "locked", nx=True, ex=self.ttl)
                self._acquired = bool(res)
                return self._acquired
            except Exception as e:
                logger.warning(f"Redis lock acquisition failed ({e}); falling back to local memory lock.")
        # Local single-process memory lock with TTL
        import time
        now = time.time()
        expiry = _LOCAL_LOCKS.get(self.lock_key)
        if expiry and expiry > now:
            self._acquired = False
            return False
        _LOCAL_LOCKS[self.lock_key] = now + self.ttl
        self._acquired = True
        return True

    def release(self) -> None:
        if not self._acquired:
            return
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip())
                r.delete(self.lock_key)
            except Exception:
                pass
        _LOCAL_LOCKS.pop(self.lock_key, None)
        self._acquired = False


def execute_durable_billing_job(
    db: Session,
    job_type: str,
    market_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes a durable billing or dunning job with lock protection,
    persistent audit records, and error reconciliation.
    """
    lock_name = f"{job_type.lower()}_{market_id or 'all'}"
    lock = SchedulerDistributedLock(lock_name, ttl_seconds=900)

    if not lock.acquire():
        logger.warning(f"[SCHEDULER] Job {job_type} skipped: lock currently held by another worker.")
        return {
            "status": "SKIPPED",
            "job_type": job_type,
            "reason": "Concurrent worker execution in progress."
        }

    job_record = BillingJobRun(
        job_type=job_type,
        market_id=market_id,
        status="RUNNING",
        attempt_count=1,
        start_time=datetime.utcnow()
    )
    db.add(job_record)
    db.commit()
    db.refresh(job_record)

    try:
        if job_type == "RECURRING_BILLING":
            result = run_billing_cycle(db, market_id=market_id)
            job_record.records_processed = result.get("invoices_generated", 0)
            job_record.records_succeeded = result.get("invoices_generated", 0)
            job_record.records_failed = 0
            job_record.summary_metadata = {
                "total_billed": float(result.get("total_billed", 0.0)),
                "market_id": market_id or "all"
            }

        elif job_type == "OVERDUE_DUNNING":
            result = evaluate_overdue_and_dunning(db, market_id=market_id)
            marked = result.get("invoices_evaluated", 0)
            job_record.records_processed = marked
            job_record.records_succeeded = marked
            job_record.records_failed = 0
            job_record.summary_metadata = {
                "aging_breakdown": result.get("aging_breakdown", {}),
                "market_id": market_id or "all"
            }
        else:
            raise ValueError(f"Unknown job_type: {job_type}")

        job_record.status = "COMPLETED"
        job_record.finish_time = datetime.utcnow()
        db.commit()

        return {
            "job_id": str(job_record.id),
            "status": "COMPLETED",
            "job_type": job_type,
            "processed": job_record.records_processed,
            "summary": job_record.summary_metadata
        }

    except Exception as e:
        logger.error(f"[SCHEDULER] Job {job_type} failed: {e}")
        db.rollback()
        job_record.status = "FAILED"
        job_record.finish_time = datetime.utcnow()
        job_record.error_details = str(e)[:1000]
        db.commit()
        return {
            "job_id": str(job_record.id),
            "status": "FAILED",
            "job_type": job_type,
            "error": str(e)
        }
    finally:
        lock.release()
