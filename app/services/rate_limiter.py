import os
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
        elif is_prod:
            logger.error("[RATE_LIMITER] Distributed Redis unconfigured or unreachable in production. Failing closed.")
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
        elif is_prod:
            raise HTTPException(status_code=503, detail="Distributed rate limiter is unconfigured or unreachable in production.")

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



# ─── Distributed Login Rate Limiting (Phase 9B Workstream C) ─────────────────

_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes


class DistributedLoginRateLimiter:
    """
    Atomic rate limiter with Redis backend and bounded in-memory development fallback.
    Protects user login endpoints from credential stuffing and brute force attacks.
    """

    def __init__(self):
        self._memory_store: Dict[str, list] = {}
        self._max_memory_entries = 10000

    def _get_redis_client(self):
        redis_url = getattr(settings, "REDIS_URL", None)
        if redis_url and redis_url.strip():
            try:
                import redis
                return redis.from_url(redis_url.strip(), decode_responses=True)
            except Exception as e:
                logger.error(f"[RATE_LIMITER] Redis connection failed: {e}")
                if settings.ENVIRONMENT.lower() in ("production", "prod"):
                    raise HTTPException(
                        status_code=503,
                        detail="Authentication rate-limiting service unavailable in production."
                    )
        elif settings.ENVIRONMENT.lower() in ("production", "prod"):
            logger.error("[RATE_LIMITER] REDIS_URL not configured in production.")
            raise HTTPException(
                status_code=503,
                detail="Production authentication rate limiting is unconfigured."
            )
        return None

    def check_rate_limit(self, identifier: str, max_attempts: int = _MAX_LOGIN_ATTEMPTS) -> None:
        """Throws 429 if rate limit has been exceeded."""
        clean_key = f"rate_limit:login:{identifier.lower().strip()}"
        r = self._get_redis_client()

        if r is not None:
            try:
                attempts = r.get(clean_key)
                if attempts is not None and int(attempts) >= max_attempts:
                    ttl = r.ttl(clean_key)
                    minutes = max(1, (ttl // 60) + 1)
                    raise HTTPException(
                        status_code=429,
                        detail=f"Too many failed login attempts. Please try again after {minutes} minutes."
                    )
                return
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[RATE_LIMITER] Redis query failed: {e}")
                if settings.ENVIRONMENT.lower() in ("production", "prod"):
                    raise HTTPException(
                        status_code=503,
                        detail="Authentication rate-limiting service unavailable in production."
                    )

        # In-memory development/testing fallback
        now = time.time()
        history = self._memory_store.get(clean_key, [])
        recent = [t for t in history if (now - t) < _LOCKOUT_SECONDS]
        self._memory_store[clean_key] = recent
        if len(recent) >= max_attempts:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts. Please try again after {_LOCKOUT_SECONDS // 60} minutes."
            )

    def record_failure(self, identifier: str) -> None:
        """Atomically increment failed attempts counter with TTL."""
        clean_key = f"rate_limit:login:{identifier.lower().strip()}"
        r = self._get_redis_client()

        if r is not None:
            try:
                pipe = r.pipeline()
                pipe.incr(clean_key)
                pipe.expire(clean_key, _LOCKOUT_SECONDS)
                pipe.execute()
                return
            except Exception as e:
                logger.error(f"[RATE_LIMITER] Redis record failed: {e}")
                if settings.ENVIRONMENT.lower() in ("production", "prod"):
                    raise HTTPException(
                        status_code=503,
                        detail="Authentication rate-limiting service unavailable in production."
                    )

        # In-memory development fallback
        now = time.time()
        if len(self._memory_store) > self._max_memory_entries:
            self._memory_store.clear()
        attempts = self._memory_store.get(clean_key, [])
        attempts.append(now)
        self._memory_store[clean_key] = attempts

    def clear(self, identifier: str) -> None:
        """Clear rate limit counter upon successful authentication."""
        clean_key = f"rate_limit:login:{identifier.lower().strip()}"
        r = self._get_redis_client()
        if r is not None:
            try:
                r.delete(clean_key)
                return
            except Exception:
                pass
        self._memory_store.pop(clean_key, None)

    def reset_in_memory(self):
        self._memory_store.clear()


login_rate_limiter = DistributedLoginRateLimiter()


def get_trustworthy_client_ip(request) -> str:
    """
    Extracts client IP. Only inspects X-Forwarded-For if client IP matches TRUSTED_PROXIES.
    """
    if not request.client:
        return "unknown"

    host = request.client.host
    trusted_proxies_cfg = getattr(settings, "TRUSTED_PROXIES", "").strip() if hasattr(settings, "TRUSTED_PROXIES") else os.getenv("TRUSTED_PROXIES", "").strip()
    if not trusted_proxies_cfg:
        return host

    trusted = [p.strip() for p in trusted_proxies_cfg.split(",") if p.strip()]
    if host in trusted:
        x_forwarded_for = request.headers.get("X-Forwarded-For")
        if x_forwarded_for:
            parts = [p.strip() for p in x_forwarded_for.split(",")]
            return parts[0]
    return host
