import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Set, Any, AsyncGenerator
import json
import logging
from app.config import settings

from datetime import datetime

logger = logging.getLogger(__name__)

class LocationEventPublisher(ABC):
    """Abstract base class for broadcasting real-time location and status events."""

    @abstractmethod
    async def publish(self, channel: str, message: dict) -> None:
        """Publish a message dictionary to a given channel."""
        pass

    @abstractmethod
    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        """Async generator yielding messages published to the specified channel."""
        pass


class InMemoryLocationPublisher(LocationEventPublisher):
    """
    In-memory async pub-sub broadcaster for single-node POC deployment.
    Uses asyncio Queues per subscriber for zero external dependencies.
    Directly swappable with RedisLocationPublisher in multi-node clusters.
    """
    def __init__(self):
        # Map channel -> Set of subscriber asyncio.Queue objects
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, message: dict) -> None:
        async with self._lock:
            queues = self._subscribers.get(channel, set()).copy()

        if not queues:
            return

        json_str = json.dumps(message)
        for queue in queues:
            try:
                queue.put_nowait(json_str)
            except asyncio.QueueFull:
                logger.warning(f"Subscriber queue full on channel {channel}, dropping message")

    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            if channel not in self._subscribers:
                self._subscribers[channel] = set()
            self._subscribers[channel].add(queue)

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield json.dumps({"event": "heartbeat", "timestamp": datetime.utcnow().isoformat()})
        finally:
            async with self._lock:
                if channel in self._subscribers:
                    self._subscribers[channel].discard(queue)
                    if not self._subscribers[channel]:
                        del self._subscribers[channel]


class RedisLocationPublisher(LocationEventPublisher):
    """
    Production-ready Redis pub-sub broadcaster for multi-node / multi-container cluster deployments.
    Delegates to in-memory fallback if Redis connection fails or is unavailable.
    """
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._fallback = InMemoryLocationPublisher()
        self._redis = None

    async def _get_redis(self):
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
            except Exception as e:
                logger.warning(f"Redis initialization failed ({e}), falling back to InMemoryPublisher")
                self._redis = False
        return self._redis if self._redis is not False else None

    async def publish(self, channel: str, message: dict) -> None:
        client = await self._get_redis()
        if client:
            try:
                json_str = json.dumps(message)
                await client.publish(channel, json_str)
                return
            except Exception as e:
                logger.warning(f"Redis publish failed ({e}), using in-memory fallback")
        await self._fallback.publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        client = await self._get_redis()
        if client:
            try:
                pubsub = client.pubsub()
                await pubsub.subscribe(channel)
                async for item in pubsub.listen():
                    if item and item.get("type") == "message":
                        yield item.get("data")
                return
            except Exception as e:
                logger.warning(f"Redis subscribe failed ({e}), using in-memory fallback")

        async for msg in self._fallback.subscribe(channel):
            yield msg


def create_event_publisher() -> LocationEventPublisher:
    redis_url = getattr(settings, "REDIS_URL", None)
    if redis_url and redis_url.strip():
        logger.info(f"Configuring RedisLocationPublisher with Redis URL: {redis_url}")
        return RedisLocationPublisher(redis_url.strip())
    return InMemoryLocationPublisher()


# Application singleton instance
event_publisher: LocationEventPublisher = create_event_publisher()
