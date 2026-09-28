"""Per-endpoint request rate limit (fixed one-minute window in Redis)."""

import time
from typing import Optional

import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL

_redis: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def allow_request(endpoint_id: int, limit_per_minute: int) -> tuple[bool, int]:
    """Count one request against the endpoint's current minute.

    Returns (allowed, seconds until the window resets). A Redis error lets
    the request through: an unavailable Redis must not drop a client's leads.
    """
    now = time.time()
    window = int(now // 60)
    retry_after = max(1, 60 - int(now % 60))
    key = f"webhook_sync_rl:{endpoint_id}:{window}"
    try:
        client = await _get_redis()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 90)
    except Exception as e:
        logger.warning(f"Webhook Sync rate limit check failed, allowing: {e}")
        return True, 0
    return count <= limit_per_minute, retry_after
