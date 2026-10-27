"""
app/services/billing_scheduler_runner.py

SentinelOS Automated Background Scheduler Runner.
Provides reliable, automatic, time-based execution for background jobs:
1. RECURRING_BILLING (Daily at 00:00 UTC)
2. OVERDUE_DUNNING (Daily at 06:00 UTC)
3. OUTBOX_DISPATCH (Every 5 minutes / 300 seconds)

Guarantees:
- Single-leader coordination across worker processes (Redis distributed leader election).
- Strict fail-closed distributed locking in production environments.
- Anti-burst protection: skips backlog bursts after prolonged downtime.
- Explicit timezone awareness via IANA timezones (default: UTC).
- Full auditability via durable BillingJobRun records.
- Graceful startup, shutdown, and health telemetry.
"""

import os
import sys
import uuid
import time
import signal
import asyncio
import logging
from datetime import datetime, timedelta, timezone as dt_tz
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore

from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.services.billing_scheduler import (
    execute_durable_billing_job, SchedulerDistributedLock,
    ALLOWLISTED_JOBS, normalize_job_type
)

logger = logging.getLogger("sentinel.scheduler_runner")

_LEADER_RUNNER_STATE: Dict[str, Any] = {}


# ─── 1. Timezone & Next-Run Cadence Calculator ────────────────────────────────

def get_scheduler_timezone() -> zoneinfo.ZoneInfo:
    """Returns validated IANA timezone configured for the scheduler runner."""
    tz_name = getattr(settings, "SCHEDULER_TIMEZONE", "UTC") or "UTC"
    try:
        return zoneinfo.ZoneInfo(tz_name.strip())
    except Exception as e:
        logger.warning(f"[SCHEDULER_RUNNER] Invalid timezone '{tz_name}' ({e}); falling back to UTC.")
        return zoneinfo.ZoneInfo("UTC")


def compute_next_run(
    job_type: str,
    from_dt_utc: datetime,
    tz: zoneinfo.ZoneInfo,
    interval_seconds: Optional[int] = None
) -> datetime:
    """
    Computes deterministic, authoritative next-run UTC datetime.
    Handles daily schedules at specified hours and periodic intervals.
    """
    canon = normalize_job_type(job_type)
    # Convert UTC to scheduler target timezone
    local_now = from_dt_utc.replace(tzinfo=dt_tz.utc).astimezone(tz)

    if canon == "RECURRING_BILLING":
        # Target: Daily at 00:00 local time
        target = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        # Convert back to naive UTC
        return target.astimezone(dt_tz.utc).replace(tzinfo=None)

    elif canon == "OVERDUE_DUNNING":
        # Target: Daily at 06:00 local time
        target = local_now.replace(hour=6, minute=0, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target.astimezone(dt_tz.utc).replace(tzinfo=None)

    elif canon == "OUTBOX_DISPATCH":
        # Target: Fixed periodic interval (default 300s / 5m)
        interval = interval_seconds or getattr(settings, "SCHEDULER_OUTBOX_INTERVAL_SECONDS", 300)
        # Ensure UTC timezone awareness before calling timestamp() to avoid naive local timezone skew
        utc_dt = from_dt_utc.replace(tzinfo=dt_tz.utc) if from_dt_utc.tzinfo is None else from_dt_utc
        current_ts = utc_dt.timestamp()
        next_ts = current_ts + interval - (current_ts % interval)
        if next_ts <= current_ts:
            next_ts += interval
        return datetime.fromtimestamp(next_ts, tz=dt_tz.utc).replace(tzinfo=None)

    else:
        # Generic default: 1 hour from now
        return from_dt_utc + timedelta(hours=1)


# ─── 2. Distributed Leader Election ───────────────────────────────────────────

class SchedulerLeaderElection:
    """
    Ensures only a single worker process or replica acts as the active scheduler leader.
    Standby workers monitor leadership without executing schedules.
    """

    LUA_RENEW_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("expire", KEYS[1], ARGV[2])
    else
        return 0
    end
    """

    LUA_RELEASE_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """

    def __init__(self, runner_id: str, leader_ttl_seconds: int = 30):
        self.runner_id = runner_id
        self.leader_key = "lock:scheduler:leader_runner"
        self.ttl = leader_ttl_seconds
        self.is_leader = False

    def try_acquire_or_renew(self) -> bool:
        """Attempts to acquire or extend the leader lease."""
        is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")
        redis_url = getattr(settings, "REDIS_URL", None)

        if is_prod and not (redis_url and redis_url.strip()):
            logger.error("[SCHEDULER_LEADER] REDIS_URL required in production for leader election. Failing closed.")
            self.is_leader = False
            return False

        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip(), decode_responses=True)
                if self.is_leader:
                    # Renew existing lease
                    res = r.eval(self.LUA_RENEW_SCRIPT, 1, self.leader_key, self.runner_id, self.ttl)
                    if res == 1:
                        return True
                    else:
                        logger.warning("[SCHEDULER_LEADER] Lost leader lease during renewal.")
                        self.is_leader = False
                        return False
                else:
                    # Attempt to claim leadership
                    acquired = r.set(self.leader_key, self.runner_id, nx=True, ex=self.ttl)
                    self.is_leader = bool(acquired)
                    if self.is_leader:
                        logger.info(f"[SCHEDULER_LEADER] Runner {self.runner_id} acquired scheduler leadership.")
                    return self.is_leader
            except Exception as e:
                logger.warning(f"[SCHEDULER_LEADER] Redis leader election error: {e}")
                if is_prod:
                    self.is_leader = False
                    return False

        # Local single-process memory leader election (development / testing only)
        now = time.time()
        current_leader = _LEADER_RUNNER_STATE.get("leader_id")
        expiry = _LEADER_RUNNER_STATE.get("expiry", 0)

        if current_leader == self.runner_id and expiry > now:
            # Renew local lease
            _LEADER_RUNNER_STATE["expiry"] = now + self.ttl
            self.is_leader = True
            return True

        if not current_leader or expiry <= now:
            _LEADER_RUNNER_STATE["leader_id"] = self.runner_id
            _LEADER_RUNNER_STATE["expiry"] = now + self.ttl
            self.is_leader = True
            logger.info(f"[SCHEDULER_LEADER_LOCAL] Runner {self.runner_id} assumed local leadership.")
            return True

        self.is_leader = False
        return False

    def release(self) -> None:
        """Voluntarily releases scheduler leadership during graceful shutdown."""
        if not self.is_leader:
            return
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                r = redis.from_url(redis_url.strip())
                r.eval(self.LUA_RELEASE_SCRIPT, 1, self.leader_key, self.runner_id)
            except Exception:
                pass
        if _LEADER_RUNNER_STATE.get("leader_id") == self.runner_id:
            _LEADER_RUNNER_STATE.clear()
        self.is_leader = False
        logger.info(f"[SCHEDULER_LEADER] Runner {self.runner_id} released leadership.")


# ─── 3. Automated Scheduler Runner Daemon ──────────────────────────────────────

class AutomatedSchedulerRunner:
    """
    Authoritative background scheduler daemon.
    Manages schedules, leader heartbeat, graceful drain, and execution dispatching.
    """

    def __init__(self):
        self.runner_id = f"runner_{uuid.uuid4().hex[:12]}"
        self.is_running = False
        self._stop_event = asyncio.Event()
        self._runner_task: Optional[asyncio.Task] = None
        self.leader_election = SchedulerLeaderElection(
            self.runner_id,
            leader_ttl_seconds=getattr(settings, "SCHEDULER_LEADER_TTL_SECONDS", 30)
        )
        self.tz = get_scheduler_timezone()
        self.started_at: Optional[datetime] = None
        self.last_heartbeat: Optional[datetime] = None
        self.recent_errors: List[Dict[str, Any]] = []
        self.active_jobs: int = 0

        # In-memory schedule definitions
        self.schedules: Dict[str, Dict[str, Any]] = {
            "RECURRING_BILLING": {
                "name": "Recurring Billing Cycle",
                "cadence": getattr(settings, "SCHEDULER_BILLING_CYCLE_CRON", "0 0 * * *"),
                "next_run_utc": None,
                "last_run_utc": None,
                "last_status": None
            },
            "OVERDUE_DUNNING": {
                "name": "Overdue Dunning & Aging Evaluation",
                "cadence": getattr(settings, "SCHEDULER_DUNNING_CRON", "0 6 * * *"),
                "next_run_utc": None,
                "last_run_utc": None,
                "last_status": None
            },
            "OUTBOX_DISPATCH": {
                "name": "Notification Outbox Dispatcher",
                "cadence": f"Every {getattr(settings, 'SCHEDULER_OUTBOX_INTERVAL_SECONDS', 300)}s",
                "interval_seconds": getattr(settings, "SCHEDULER_OUTBOX_INTERVAL_SECONDS", 300),
                "next_run_utc": None,
                "last_run_utc": None,
                "last_status": None
            }
        }

    def initialize_schedules(self, from_dt_utc: Optional[datetime] = None) -> None:
        """
        Initializes next execution times deterministically.
        Applies anti-burst rule: historical backlog is NOT queued in bursts.
        """
        now = from_dt_utc or datetime.utcnow()
        self.tz = get_scheduler_timezone()

        for j_type, conf in self.schedules.items():
            conf["next_run_utc"] = compute_next_run(
                job_type=j_type,
                from_dt_utc=now,
                tz=self.tz,
                interval_seconds=conf.get("interval_seconds")
            )
            logger.info(
                f"[SCHEDULER_RUNNER] Initialized schedule for {j_type}. "
                f"Next run: {conf['next_run_utc'].isoformat()} UTC (TZ: {self.tz})"
            )

    async def start(self) -> None:
        """Starts the scheduler loop asynchronously."""
        if not getattr(settings, "SCHEDULER_ENABLED", True):
            logger.info("[SCHEDULER_RUNNER] Scheduler disabled by configuration (SCHEDULER_ENABLED=false).")
            return

        if self.is_running:
            logger.warning("[SCHEDULER_RUNNER] Scheduler runner is already running.")
            return

        self.is_running = True
        self.started_at = datetime.utcnow()
        self._stop_event.clear()
        self.initialize_schedules(self.started_at)

        self._runner_task = asyncio.create_task(self._main_loop())
        logger.info(f"[SCHEDULER_RUNNER] Started AutomatedSchedulerRunner ({self.runner_id}).")

    async def stop(self, timeout_seconds: float = 10.0) -> None:
        """Gracefully halts the scheduler runner and releases leader lease."""
        if not self.is_running:
            return

        logger.info("[SCHEDULER_RUNNER] Initiating graceful shutdown...")
        self.is_running = False
        self._stop_event.set()

        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._runner_task), timeout=timeout_seconds)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Release leader lease so standby replica takes over immediately
        self.leader_election.release()
        logger.info("[SCHEDULER_RUNNER] Graceful shutdown complete.")

    async def _main_loop(self) -> None:
        """Core asynchronous evaluation loop."""
        heartbeat_interval = getattr(settings, "SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", 10)
        last_hb_time = 0.0

        while not self._stop_event.is_set():
            try:
                now_mono = time.time()
                # 1. Leader election check / heartbeat
                if now_mono - last_hb_time >= heartbeat_interval:
                    self.leader_election.try_acquire_or_renew()
                    self.last_heartbeat = datetime.utcnow()
                    last_hb_time = now_mono

                # 2. If leader, evaluate schedules
                if self.leader_election.is_leader:
                    await self._evaluate_and_dispatch_due_jobs()

                # Sleep tick (1.0 second for responsive timing)
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SCHEDULER_RUNNER] Unexpected loop error: {e}")
                self._record_error(str(e))
                await asyncio.sleep(2.0)

    async def _evaluate_and_dispatch_due_jobs(self) -> None:
        """Checks registered jobs and dispatches any due tasks."""
        now_utc = datetime.utcnow()
        max_concurrent = getattr(settings, "SCHEDULER_MAX_CONCURRENT_JOBS", 3)

        for j_type, conf in self.schedules.items():
            next_run = conf.get("next_run_utc")
            if not next_run or now_utc < next_run:
                continue

            if self.active_jobs >= max_concurrent:
                logger.warning(
                    f"[SCHEDULER_RUNNER] Max concurrent jobs limit reached ({max_concurrent}). "
                    f"Postponing {j_type}."
                )
                break

            # Update next run time IMMEDIATELY to prevent double-dispatch in next tick
            conf["next_run_utc"] = compute_next_run(
                job_type=j_type,
                from_dt_utc=now_utc,
                tz=self.tz,
                interval_seconds=conf.get("interval_seconds")
            )

            # Dispatch job execution in background task
            asyncio.create_task(self._execute_job_task(j_type))

    async def _execute_job_task(self, job_type: str) -> None:
        """Executes a durable billing job in threadpool to prevent blocking the event loop."""
        self.active_jobs += 1
        conf = self.schedules.get(job_type, {})
        start_time = datetime.utcnow()

        logger.info(f"[SCHEDULER_RUNNER] Dispatching automated scheduled run for {job_type}.")
        try:
            # Run in worker thread since execute_durable_billing_job performs synchronous database I/O
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._run_job_sync, job_type)

            conf["last_run_utc"] = start_time
            conf["last_status"] = result.get("status", "COMPLETED")
            logger.info(f"[SCHEDULER_RUNNER] Job {job_type} finished with status: {conf['last_status']}.")

        except Exception as e:
            logger.error(f"[SCHEDULER_RUNNER] Error executing scheduled job {job_type}: {e}")
            conf["last_run_utc"] = start_time
            conf["last_status"] = "FAILED"
            self._record_error(f"{job_type}: {e}")
        finally:
            self.active_jobs = max(0, self.active_jobs - 1)

    def _run_job_sync(self, job_type: str) -> Dict[str, Any]:
        """Synchronous execution wrapper using isolated database session."""
        db: Session = SessionLocal()
        try:
            res = execute_durable_billing_job(
                db=db,
                job_type=job_type,
                market_id=None,  # Multi-tenant all markets
                initiated_by=f"AUTOMATED_SCHEDULER_RUNNER_{self.runner_id[:8]}"
            )
            return res
        finally:
            db.close()

    def run_once_for_test(self, job_type: str) -> Dict[str, Any]:
        """Synchronous single-shot execution for testing and verification."""
        return self._run_job_sync(job_type)

    def _record_error(self, err_msg: str) -> None:
        self.recent_errors.append({
            "timestamp": datetime.utcnow().isoformat(),
            "message": err_msg[:500]
        })
        if len(self.recent_errors) > 20:
            self.recent_errors.pop(0)

    def get_status(self) -> Dict[str, Any]:
        """Returns deep telemetry for API and operational console."""
        redis_url = getattr(settings, "REDIS_URL", None)
        has_redis = bool(redis_url and redis_url.strip())

        sched_list = []
        for j_type, conf in self.schedules.items():
            sched_list.append({
                "job_type": j_type,
                "name": conf["name"],
                "cadence": conf["cadence"],
                "next_run_utc": conf["next_run_utc"].isoformat() if conf["next_run_utc"] else None,
                "last_run_utc": conf["last_run_utc"].isoformat() if conf["last_run_utc"] else None,
                "last_status": conf["last_status"]
            })

        return {
            "runner_enabled": getattr(settings, "SCHEDULER_ENABLED", True),
            "is_running": self.is_running,
            "is_leader": self.leader_election.is_leader,
            "runner_id": self.runner_id,
            "timezone": str(self.tz),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "coordination_backend": "REDIS" if has_redis else "LOCAL_MEMORY",
            "active_jobs_count": self.active_jobs,
            "schedules": sched_list,
            "recent_errors": self.recent_errors
        }


# Global singleton runner instance
scheduler_runner = AutomatedSchedulerRunner()


# ─── 4. Standalone Process Entrypoint ──────────────────────────────────────────

def run_standalone_daemon():
    """
    Standalone process runner for dedicated Docker / container deployment.
    Usage: python -m app.services.billing_scheduler_runner
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    logger.info("Initializing SentinelOS Standalone Automated Scheduler Runner...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal():
        logger.info("Received termination signal. Stopping runner...")
        loop.create_task(scheduler_runner.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except (NotImplementedError, RuntimeError):
            # Windows does not support add_signal_handler in all event loops
            pass

    async def main():
        await scheduler_runner.start()
        while scheduler_runner.is_running:
            await asyncio.sleep(1.0)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        loop.run_until_complete(scheduler_runner.stop())
    finally:
        loop.close()
        logger.info("Scheduler daemon terminated.")


if __name__ == "__main__":
    run_standalone_daemon()
