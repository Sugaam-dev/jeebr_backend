import time
import asyncio
import logging
from typing import Tuple, Dict, Optional
from fastapi import HTTPException
from app.config import settings

logger = logging.getLogger(__name__)


class EngineerLocationRateLimiter:
    """
    Server-side rate limiter for engineer GPS location pings.
    Enforces a maximum rate of 1 accepted operational ping per engineer per interval (default: 3 seconds).
    
    In multi-instance production, backed by Redis when REDIS_URL is configured.
    In single-instance development/testing, uses an in-memory timestamp store.
    """
    def __init__(self, min_interval_seconds: float = 3.0):
        self.min_interval_seconds = min_interval_seconds
        self._in_memory_timestamps: Dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._redis_client = None

    async def _get_redis(self):
        redis_url = getattr(settings, "REDIS_URL", None)
        if not redis_url or not redis_url.strip():
            return None

        if self._redis_client is None:
            try:
                import redis.asyncio as aioredis
                self._redis_client = aioredis.from_url(redis_url.strip(), decode_responses=True)
            except Exception as e:
                logger.warning(f"[RATE_LIMITER] Redis connection failed ({e}), falling back to in-memory limiter.")
                self._redis_client = False

        return self._redis_client if self._redis_client is not False else None

    async def is_throttled(self, engineer_id: int) -> Tuple[bool, float]:
        """
        Checks if an engineer is currently throttled without consuming the slot.
        Returns: (is_throttled: bool, retry_after_seconds: float)
        """
        redis = await self._get_redis()
        now = time.time()
        is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")

        redis_url = getattr(settings, "REDIS_URL", None)
        if redis:
            key = f"rate_limit:engineer_ping:{engineer_id}"
            try:
                pttl = await redis.pttl(key)
                if pttl > 0:
                    retry_after = round(max(0.1, pttl / 1000.0), 2)
                    return True, retry_after
                return False, 0.0
            except Exception as e:
                logger.warning(f"[RATE_LIMITER] Redis error in is_throttled ({e})")
                if is_prod:
                    logger.error("[RATE_LIMITER] Distributed Redis unavailable in production. Failing closed.")
                    return True, self.min_interval_seconds
        elif is_prod and redis_url and redis_url.strip():
            logger.error("[RATE_LIMITER] Distributed Redis connection failed in production. Failing closed.")
            return True, self.min_interval_seconds

        async with self._lock:
            last_ping = self._in_memory_timestamps.get(engineer_id, 0.0)
            elapsed = now - last_ping
            if elapsed < self.min_interval_seconds:
                retry_after = round(self.min_interval_seconds - elapsed, 2)
                return True, max(0.1, retry_after)
            return False, 0.0

    async def record_accepted_ping(self, engineer_id: int) -> None:
        """
        Records the timestamp of an accepted operational ping, consuming the rate limit slot.
        """
        redis = await self._get_redis()
        now = time.time()
        is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")
        redis_url = getattr(settings, "REDIS_URL", None)

        if redis:
            key = f"rate_limit:engineer_ping:{engineer_id}"
            try:
                px_ttl = int(self.min_interval_seconds * 1000)
                await redis.set(key, str(now), px=px_ttl)
                return
            except Exception as e:
                logger.warning(f"[RATE_LIMITER] Redis error in record_accepted_ping ({e})")
                if is_prod:
                    raise HTTPException(status_code=503, detail="Distributed rate limiter service unavailable in production.")
        elif is_prod and redis_url and redis_url.strip():
            raise HTTPException(status_code=503, detail="Distributed rate limiter is unreachable in production.")

        async with self._lock:
            self._in_memory_timestamps[engineer_id] = now
            if len(self._in_memory_timestamps) > 1000:
                cutoff = now - 3600
                self._in_memory_timestamps = {
                    eid: t for eid, t in self._in_memory_timestamps.items() if t > cutoff
                }

    async def check_rate_limit(self, engineer_id: int) -> Tuple[bool, float]:
        """Atomic check-and-consume convenience method."""
        throttled, retry_after = await self.is_throttled(engineer_id)
        if throttled:
            return False, retry_after
        await self.record_accepted_ping(engineer_id)
        return True, 0.0

    def reset_in_memory(self):
        """Helper to reset in-memory state during tests."""
        self._in_memory_timestamps.clear()


# Global singleton instance
location_rate_limiter = EngineerLocationRateLimiter(min_interval_seconds=3.0)
