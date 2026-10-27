"""
app/services/billing_scheduler.py

SentinelOS Automated Recurring Billing & Dunning Scheduler.
Provides durable, automated execution for:
1. Daily recurring billing runs (invoice generation for due subscriptions).
2. Dunning and overdue evaluations (aging categorization, reminders, grace period enforcement).
3. Distributed execution locks to prevent overlapping worker instances.
4. Durable job execution audit logs with start/finish timestamps, attempt counts, and metrics.
5. Operational query interfaces, safe reconciliation, and notification outbox visibility.
"""

import os
import re
import json
import uuid
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.config import settings
from app.database import SessionLocal
from app.models_finance import BillingJobRun, NotificationOutbox
from app.services.billing_service import run_billing_cycle, evaluate_overdue_and_dunning
from app.services.notification_service import enqueue_notification, process_notification_outbox

logger = logging.getLogger("sentinel.billing_scheduler")


_LOCAL_LOCKS: Dict[str, float] = {}

ALLOWLISTED_JOBS = ("RECURRING_BILLING", "OVERDUE_DUNNING", "OUTBOX_DISPATCH")


# ─── PII & Sanitization Helpers ───────────────────────────────────────────────

def mask_recipient(recipient: str) -> str:
    """Masks phone numbers or email addresses to protect PII in UI & logs."""
    if not recipient:
        return "***"
    recip = recipient.strip()
    if "@" in recip:
        parts = recip.split("@", 1)
        name, domain = parts[0], parts[1]
        if len(name) <= 2:
            masked_name = name[0] + "*"
        else:
            masked_name = name[0] + ("*" * min(len(name) - 2, 6)) + name[-1]
        return f"{masked_name}@{domain}"
    digits = re.sub(r'[\s\-+]', '', recip)
    if len(digits) >= 10:
        return f"{digits[:2]}******{digits[-4:]}"
    elif len(digits) >= 6:
        return f"{digits[:2]}***{digits[-2:]}"
    return "***"


def sanitize_notification_message(message: str) -> str:
    """Masks OTP codes, auth tokens, passwords, and card digits."""
    if not message:
        return ""
    # Mask OTP/code/pin/token patterns including "OTP is 123456", "token-XYZ123", "secret token: XYZ123"
    msg = re.sub(
        r'(?i)\b(otp|code|pin|password|secret|token)(?:\s+(?:is|token|code))?[\s:=#\-]+([a-zA-Z0-9_\-]{4,32})\b',
        r'\1: [REDACTED]',
        message
    )
    # Also mask standalone 6-digit OTP codes
    msg = re.sub(r'(?i)\b(?:is\s+)?(\d{6})\b', r'[REDACTED_OTP]', msg)
    # Mask payment card numbers (16 digits)
    msg = re.sub(r'\b(?:\d{4}[ -]?){3}\d{4}\b', r'[CARD_REDACTED]', msg)
    return msg


def sanitize_error_message(err: Optional[str]) -> Optional[str]:
    """Sanitizes sensitive database or infrastructure credentials in error strings."""
    if not err:
        return None
    clean = re.sub(r'//[^:]+:[^@]+@', r'//[REDACTED_CREDENTIALS]@', err)
    clean = re.sub(r'(?i)(bearer|token|secret|key|password)=([^\s;&]+)', r'\1=[REDACTED]', clean)
    return clean[:1000]


def normalize_job_type(job_type: str) -> str:
    """Maps aliases and user input to canonical allowlisted job types."""
    jt = (job_type or "").strip().upper()
    if jt in ("BILLING_CYCLE", "BILLING_RUN", "RECURRING_BILLING"):
        return "RECURRING_BILLING"
    if jt in ("DUNNING_EVALUATION", "EVALUATE_OVERDUE", "OVERDUE_DUNNING"):
        return "OVERDUE_DUNNING"
    if jt in ("OUTBOX_DISPATCH", "NOTIFICATION_DISPATCH", "PROCESS_OUTBOX"):
        return "OUTBOX_DISPATCH"
    return jt


# ─── Distributed Lock ─────────────────────────────────────────────────────────

class SchedulerDistributedLock:
    """
    Distributed lock using Redis when configured, with safe local fallback.
    Prevents concurrent workers from executing the same billing cycle.
    Enforces strict lock ownership: a worker can only release or extend its own lock.
    """

    LUA_RELEASE_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """

    LUA_EXTEND_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("expire", KEYS[1], ARGV[2])
    else
        return 0
    end
    """

    def __init__(self, lock_key: str, ttl_seconds: int = 1800, owner_token: Optional[str] = None):
        self.lock_key = f"lock:scheduler:{lock_key}"
        self.raw_key = lock_key
        self.ttl = ttl_seconds
        self.owner_token = owner_token or f"worker_{uuid.uuid4().hex}"
        self._acquired = False

    def acquire(self) -> bool:
        is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")
        redis_url = getattr(settings, "REDIS_URL", None)
        if is_prod and not (redis_url and redis_url.strip()):
            logger.error("[SCHEDULER_LOCK] REDIS_URL required in production for distributed locking. Failing closed.")
            return False

        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                # SETNX with TTL and unique owner token
                res = r.set(self.lock_key, self.owner_token, nx=True, ex=self.ttl)
                self._acquired = bool(res)
                return self._acquired
            except Exception as e:
                logger.warning(f"Redis lock acquisition failed ({e}); falling back to local memory lock.")
                if is_prod:
                    logger.error("[SCHEDULER_LOCK] Redis lock unavailable in production. Failing closed.")
                    return False

        # Local single-process memory lock with TTL & ownership check
        import time
        now = time.time()
        entry = _LOCAL_LOCKS.get(self.lock_key)
        if entry:
            expiry = entry.get("expiry", 0) if isinstance(entry, dict) else entry
            if expiry > now:
                self._acquired = False
                return False
        _LOCAL_LOCKS[self.lock_key] = {"expiry": now + self.ttl, "owner": self.owner_token}
        self._acquired = True
        return True

    def release(self) -> bool:
        """
        Releases the lock ONLY if the current owner matches self.owner_token.
        Prevents one worker from releasing another worker's lock.
        """
        if not self._acquired:
            return False
        released = False
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                res = r.eval(self.LUA_RELEASE_SCRIPT, 1, self.lock_key, self.owner_token)
                released = bool(res)
            except Exception as e:
                logger.warning(f"Redis lock release error ({e}); attempting local cleanup.")
        # Local cleanup with ownership check
        entry = _LOCAL_LOCKS.get(self.lock_key)
        if entry:
            owner = entry.get("owner") if isinstance(entry, dict) else None
            if owner is None or owner == self.owner_token:
                _LOCAL_LOCKS.pop(self.lock_key, None)
                released = True
            else:
                logger.warning(
                    f"[SCHEDULER_LOCK] Cannot release lock {self.lock_key}: owner mismatch "
                    f"(lock owner: {owner}, caller: {self.owner_token})"
                )
                released = False
        self._acquired = False
        return released

    def extend(self, additional_ttl_seconds: int) -> bool:
        """Extends lock lease if caller is the authoritative owner."""
        if not self._acquired:
            return False
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                res = r.eval(self.LUA_EXTEND_SCRIPT, 1, self.lock_key, self.owner_token, additional_ttl_seconds)
                return bool(res)
            except Exception as e:
                logger.warning(f"Redis lock extend failed: {e}")
                return False
        import time
        entry = _LOCAL_LOCKS.get(self.lock_key)
        if entry and isinstance(entry, dict) and entry.get("owner") == self.owner_token:
            entry["expiry"] = time.time() + additional_ttl_seconds
            return True
        return False

    @classmethod
    def is_locked(cls, lock_key: str) -> bool:
        """Inspects if a lock is currently active."""
        full_key = f"lock:scheduler:{lock_key}"
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                return bool(r.exists(full_key))
            except Exception:
                pass
        import time
        entry = _LOCAL_LOCKS.get(full_key)
        if entry:
            expiry = entry.get("expiry", 0) if isinstance(entry, dict) else entry
            return bool(expiry > time.time())
        return False

    @classmethod
    def force_release(cls, lock_key: str) -> bool:
        """Forces release of a lock key during manual operator reconciliation."""
        full_key = f"lock:scheduler:{lock_key}"
        released = False
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip())
                res = r.delete(full_key)
                if res > 0:
                    released = True
            except Exception:
                pass
        if full_key in _LOCAL_LOCKS:
            _LOCAL_LOCKS.pop(full_key, None)
            released = True
        return released

    @classmethod
    def get_lock_info(cls, lock_key: str) -> Dict[str, Any]:
        """Returns active lock status, remaining TTL, and masked owner token."""
        full_key = f"lock:scheduler:{lock_key}"
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                owner = r.get(full_key)
                if owner:
                    ttl = r.ttl(full_key)
                    masked_owner = f"{owner[:8]}..." if len(owner) > 8 else owner
                    return {"is_locked": True, "ttl_remaining_seconds": ttl, "owner": masked_owner}
                return {"is_locked": False, "ttl_remaining_seconds": None, "owner": None}
            except Exception:
                pass
        import time
        entry = _LOCAL_LOCKS.get(full_key)
        now = time.time()
        if entry:
            expiry = entry.get("expiry", 0) if isinstance(entry, dict) else entry
            owner = entry.get("owner") if isinstance(entry, dict) else "unknown"
            if expiry > now:
                masked_owner = f"{owner[:8]}..." if len(owner) > 8 else owner
                return {"is_locked": True, "ttl_remaining_seconds": int(expiry - now), "owner": masked_owner}
        return {"is_locked": False, "ttl_remaining_seconds": None, "owner": None}


# ─── Authoritative Durable Job Execution ──────────────────────────────────────

def execute_durable_billing_job(
    db: Session,
    job_type: str,
    market_id: Optional[str] = None,
    initiated_by: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes a durable background job with distributed lock protection,
    persistent audit records, and error reconciliation.
    Supports: RECURRING_BILLING, OVERDUE_DUNNING, OUTBOX_DISPATCH.
    """
    canonical_type = normalize_job_type(job_type)
    if canonical_type not in ALLOWLISTED_JOBS:
        raise ValueError(
            f"Unsupported job type: '{job_type}'. "
            f"Allowlisted jobs: {', '.join(ALLOWLISTED_JOBS)}"
        )

    lock_name = f"{canonical_type.lower()}_{market_id or 'all'}"
    lock = SchedulerDistributedLock(lock_name, ttl_seconds=900)

    if not lock.acquire():
        logger.warning(f"[SCHEDULER] Job {canonical_type} skipped: lock currently held by another worker.")
        return {
            "status": "SKIPPED",
            "job_type": canonical_type,
            "market_id": market_id,
            "reason": f"Distributed lock for {canonical_type} ({market_id or 'all'}) is currently held by another active worker."
        }

    job_record = BillingJobRun(
        job_type=canonical_type,
        market_id=market_id,
        status="RUNNING",
        attempt_count=1,
        start_time=datetime.utcnow()
    )
    db.add(job_record)
    db.commit()
    db.refresh(job_record)

    try:
        if canonical_type == "RECURRING_BILLING":
            result = run_billing_cycle(db, market_id=market_id)
            job_record.records_processed = result.get("invoices_generated", 0)
            job_record.records_succeeded = result.get("invoices_generated", 0)
            job_record.records_failed = 0
            job_record.summary_metadata = {
                "total_billed": float(result.get("total_billed", 0.0)),
                "market_id": market_id or "all",
                "initiated_by": initiated_by or "SCHEDULER_CRON"
            }

        elif canonical_type == "OVERDUE_DUNNING":
            result = evaluate_overdue_and_dunning(db, market_id=market_id)
            marked = result.get("invoices_evaluated", 0)
            job_record.records_processed = marked
            job_record.records_succeeded = marked
            job_record.records_failed = 0
            job_record.summary_metadata = {
                "aging_breakdown": result.get("aging_breakdown", {}),
                "market_id": market_id or "all",
                "initiated_by": initiated_by or "SCHEDULER_CRON"
            }

        elif canonical_type == "OUTBOX_DISPATCH":
            result = process_notification_outbox(db)
            job_record.records_processed = result.get("processed", 0)
            job_record.records_succeeded = result.get("succeeded", 0)
            job_record.records_failed = result.get("failed", 0)
            job_record.summary_metadata = {
                "provider": result.get("provider", "UNKNOWN"),
                "is_simulated": result.get("is_simulated", True),
                "market_id": market_id or "all",
                "initiated_by": initiated_by or "SCHEDULER_CRON"
            }

        job_record.status = "COMPLETED"
        job_record.finish_time = datetime.utcnow()
        db.commit()

        return {
            "job_id": str(job_record.id),
            "status": "COMPLETED",
            "job_type": canonical_type,
            "processed": job_record.records_processed,
            "summary": job_record.summary_metadata
        }

    except Exception as e:
        logger.error(f"[SCHEDULER] Job {canonical_type} failed: {e}")
        db.rollback()
        job_record.status = "FAILED"
        job_record.finish_time = datetime.utcnow()
        job_record.error_details = sanitize_error_message(str(e))
        db.commit()
        return {
            "job_id": str(job_record.id),
            "status": "FAILED",
            "job_type": canonical_type,
            "error": str(e)
        }
    finally:
        lock.release()


# ─── Operational Queries & Descriptors ────────────────────────────────────────

def _format_job_run_dict(run: BillingJobRun) -> Dict[str, Any]:
    duration = None
    if run.finish_time and run.start_time:
        duration = round((run.finish_time - run.start_time).total_seconds(), 2)
    return {
        "id": str(run.id),
        "job_type": run.job_type,
        "market_id": run.market_id,
        "status": run.status,
        "attempt_count": run.attempt_count,
        "start_time": run.start_time,
        "finish_time": run.finish_time,
        "duration_seconds": duration,
        "records_processed": run.records_processed,
        "records_succeeded": run.records_succeeded,
        "records_failed": run.records_failed,
        "records_skipped": run.records_skipped,
        "error_details": sanitize_error_message(run.error_details),
        "summary_metadata": run.summary_metadata or {},
        "created_at": run.created_at
    }


def get_scheduled_job_descriptors(
    db: Session,
    market_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Returns canonical list of allowlisted background jobs with their cadence,
    real-time distributed lock state, and last recorded execution outcome.
    Calculates authoritative next scheduled run via scheduler runner.
    """
    from app.services.billing_scheduler_runner import compute_next_run, get_scheduler_timezone
    tz = get_scheduler_timezone()
    now_utc = datetime.utcnow()

    job_defs = [
        {
            "job_type": "RECURRING_BILLING",
            "name": "Recurring Billing Cycle",
            "description": "Evaluates active subscriptions across market tenants, generates monthly cycle invoices with GST breakdown, and tracks accounts receivable.",
            "cadence": getattr(settings, "SCHEDULER_BILLING_CYCLE_CRON", "0 0 * * *"),
            "allowed_roles": ["Admin", "SUPER_ADMIN", "Revenue"],
        },
        {
            "job_type": "OVERDUE_DUNNING",
            "name": "Overdue Dunning & Aging Evaluation",
            "description": "Evaluates unpaid past-due invoices, calculates aging brackets (1-30, 31-60, 61-90, 90+ days), and enqueues overdue reminders to Notification Outbox.",
            "cadence": getattr(settings, "SCHEDULER_DUNNING_CRON", "0 6 * * *"),
            "allowed_roles": ["Admin", "SUPER_ADMIN", "Revenue"],
        },
        {
            "job_type": "OUTBOX_DISPATCH",
            "name": "Notification Outbox Dispatcher",
            "description": "Processes pending outbox items and dispatches SMS/Email/WhatsApp alerts with exponential backoff and carrier delivery receipts.",
            "cadence": f"Every {getattr(settings, 'SCHEDULER_OUTBOX_INTERVAL_SECONDS', 300)}s",
            "allowed_roles": ["Admin", "SUPER_ADMIN", "Revenue"],
        }
    ]

    descriptors = []
    for jd in job_defs:
        j_type = jd["job_type"]
        lock_name = f"{j_type.lower()}_{market_id or 'all'}"
        lock_info = SchedulerDistributedLock.get_lock_info(lock_name)

        # Retrieve last recorded run
        query = db.query(BillingJobRun).filter(BillingJobRun.job_type == j_type)
        if market_id and market_id != "all":
            query = query.filter(or_(BillingJobRun.market_id == market_id, BillingJobRun.market_id == None))
        last_run = query.order_by(BillingJobRun.start_time.desc()).first()

        next_dt = compute_next_run(j_type, now_utc, tz)

        descriptors.append({
            "job_type": j_type,
            "name": jd["name"],
            "description": jd["description"],
            "cadence": jd["cadence"],
            "next_run_estimated": next_dt.isoformat() if next_dt else None,
            "is_locked": lock_info.get("is_locked", False),
            "lock_info": lock_info,
            "last_run": _format_job_run_dict(last_run) if last_run else None,
            "allowed_roles": jd["allowed_roles"]
        })

    return descriptors


def get_job_runs(
    db: Session,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    market_id: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20
) -> Dict[str, Any]:
    """Retrieves paginated and filtered BillingJobRun records."""
    query = db.query(BillingJobRun)

    if job_type:
        canon = normalize_job_type(job_type)
        query = query.filter(BillingJobRun.job_type == canon)
    if status:
        query = query.filter(BillingJobRun.status == status.upper())
    if market_id and market_id != "all":
        query = query.filter(or_(BillingJobRun.market_id == market_id, BillingJobRun.market_id == None))
    if date_from:
        query = query.filter(BillingJobRun.start_time >= date_from)
    if date_to:
        query = query.filter(BillingJobRun.start_time <= date_to)

    total = query.count()
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    pages = (total + page_size - 1) // page_size if total > 0 else 1

    runs = query.order_by(BillingJobRun.start_time.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return {
        "items": [_format_job_run_dict(r) for r in runs],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages
    }


def get_job_run_detail(db: Session, run_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves deep execution details for a specific run record."""
    run = db.query(BillingJobRun).filter(BillingJobRun.id == run_id).first()
    if not run:
        return None
    return _format_job_run_dict(run)


def reconcile_job_run(
    db: Session,
    run_id: str,
    action: str = "RECONCILE",
    reason: str = "Manual operator intervention",
    initiated_by: Optional[str] = None
) -> Dict[str, Any]:
    """
    Safely reconciles a stalled or failed job run.
    Clears orphaned distributed locks and transitions status.
    If action is 'RETRY', re-executes the job idempotently.
    """
    run = db.query(BillingJobRun).filter(BillingJobRun.id == run_id).first()
    if not run:
        raise ValueError(f"Job run '{run_id}' not found.")

    act = action.strip().upper()
    lock_name = f"{run.job_type.lower()}_{run.market_id or 'all'}"

    if act == "RECONCILE":
        if run.status in ("COMPLETED", "RECONCILED"):
            raise ValueError(f"Job run is already in terminal state '{run.status}' and cannot be reconciled.")

        # Release any stuck lock
        SchedulerDistributedLock.force_release(lock_name)

        run.status = "RECONCILED"
        if not run.finish_time:
            run.finish_time = datetime.utcnow()
        meta = dict(run.summary_metadata or {})
        meta["reconciled_at"] = datetime.utcnow().isoformat()
        meta["reconciled_by"] = initiated_by or "operator"
        meta["reconciliation_reason"] = reason
        run.summary_metadata = meta
        db.commit()
        db.refresh(run)
        return _format_job_run_dict(run)

    elif act == "RETRY":
        if run.status == "RUNNING":
            # Force release lock before retry
            SchedulerDistributedLock.force_release(lock_name)

        # Mark previous run as RETRIED
        run.status = "RETRIED"
        if not run.finish_time:
            run.finish_time = datetime.utcnow()
        db.commit()

        # Re-execute job idempotently
        new_result = execute_durable_billing_job(
            db=db,
            job_type=run.job_type,
            market_id=run.market_id,
            initiated_by=f"RETRY_BY_{initiated_by or 'operator'}"
        )
        return new_result

    else:
        raise ValueError(f"Unsupported reconciliation action '{action}'. Supported: RECONCILE, RETRY.")


# ─── Notification Outbox Visibility Queries ───────────────────────────────────

def get_notification_outbox_items(
    db: Session,
    channel: Optional[str] = None,
    status: Optional[str] = None,
    market_id: Optional[str] = None,
    customer_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 20
) -> Dict[str, Any]:
    """
    Returns sanitized notification queue items with PII masking.
    Exposes delivery status, simulated flag, and attempt counts.
    """
    query = db.query(NotificationOutbox)

    if channel:
        query = query.filter(NotificationOutbox.channel == channel.upper())
    if status:
        query = query.filter(NotificationOutbox.status == status.upper())
    if market_id and market_id != "all":
        query = query.filter(NotificationOutbox.market_id == market_id)
    if customer_id:
        query = query.filter(NotificationOutbox.customer_id == customer_id)

    total = query.count()
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    pages = (total + page_size - 1) // page_size if total > 0 else 1

    items = query.order_by(NotificationOutbox.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    # Calculate status summary
    status_counts = {}
    counts = db.query(NotificationOutbox.status, func.count(NotificationOutbox.id)).group_by(NotificationOutbox.status).all()
    for s, c in counts:
        status_counts[s] = c

    formatted_items = []
    for item in items:
        formatted_items.append({
            "id": str(item.id),
            "market_id": item.market_id,
            "customer_id": item.customer_id,
            "recipient_masked": mask_recipient(item.recipient),
            "channel": item.channel,
            "event_type": item.event_type,
            "message_preview": sanitize_notification_message(item.message),
            "status": item.status,
            "attempt_count": item.attempt_count,
            "max_attempts": item.max_attempts,
            "last_error_sanitized": sanitize_error_message(item.last_error),
            "idempotency_key": item.idempotency_key,
            "is_simulated": item.is_simulated,
            "scheduled_at": item.scheduled_at,
            "sent_at": item.sent_at,
            "created_at": item.created_at
        })

    return {
        "items": formatted_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "summary_by_status": status_counts
    }


def get_scheduler_overview_kpis(
    db: Session,
    market_id: Optional[str] = None
) -> Dict[str, Any]:
    """Aggregates high-level scheduler and notification KPIs."""
    run_q = db.query(BillingJobRun)
    if market_id and market_id != "all":
        run_q = run_q.filter(or_(BillingJobRun.market_id == market_id, BillingJobRun.market_id == None))

    total_runs = run_q.count()
    successful_runs = run_q.filter(BillingJobRun.status == "COMPLETED").count()
    failed_runs = run_q.filter(BillingJobRun.status == "FAILED").count()
    running_jobs = run_q.filter(BillingJobRun.status == "RUNNING").count()

    last_completed = (
        run_q.filter(BillingJobRun.status == "COMPLETED")
        .order_by(BillingJobRun.finish_time.desc())
        .first()
    )

    # Count active locks
    active_locks = 0
    for jt in ALLOWLISTED_JOBS:
        lock_name = f"{jt.lower()}_{market_id or 'all'}"
        if SchedulerDistributedLock.is_locked(lock_name):
            active_locks += 1

    # Notification Outbox counts
    notif_q = db.query(NotificationOutbox)
    if market_id and market_id != "all":
        notif_q = notif_q.filter(NotificationOutbox.market_id == market_id)

    outbox_queued = notif_q.filter(NotificationOutbox.status.in_(["QUEUED", "RETRYING"])).count()
    outbox_failed = notif_q.filter(NotificationOutbox.status == "FAILED").count()
    outbox_sent = notif_q.filter(NotificationOutbox.status.in_(["SENT", "DELIVERED"])).count()

    descriptors = get_scheduled_job_descriptors(db, market_id=market_id)

    # Runner telemetry
    runner_status = None
    try:
        from app.services.billing_scheduler_runner import scheduler_runner
        runner_status = scheduler_runner.get_status()
    except Exception as e:
        logger.warning(f"Could not retrieve scheduler runner telemetry: {e}")

    return {
        "total_runs": total_runs,
        "successful_runs": successful_runs,
        "failed_runs": failed_runs,
        "running_jobs": running_jobs,
        "last_completed_at": last_completed.finish_time if last_completed else None,
        "active_locks": active_locks,
        "outbox_queued": outbox_queued,
        "outbox_failed": outbox_failed,
        "outbox_sent": outbox_sent,
        "jobs": descriptors,
        "runner": runner_status
    }
