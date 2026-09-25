"""The atomic claim that lets only one not-connected report per workflow run
schedule a campaign retry (see status_processor._process_status_update)."""

import pytest

from api.services.campaign.rate_limiter import RateLimiter


class _FakeRedis:
    """SET with NX/EX semantics, which is all the claim relies on."""

    def __init__(self):
        self.store: dict[str, tuple[str, int | None]] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = (value, ex)
        return True


class _BrokenRedis:
    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down")


def _limiter(redis_client) -> RateLimiter:
    limiter = RateLimiter()
    limiter.redis_client = redis_client
    return limiter


@pytest.mark.asyncio
async def test_only_the_first_claim_for_a_run_wins():
    redis = _FakeRedis()
    limiter = _limiter(redis)

    assert await limiter.claim_not_connected_report(470) is True
    assert await limiter.claim_not_connected_report(470) is False
    assert await limiter.claim_not_connected_report(470) is False
    # The claim expires, so a stale key can never block the run forever.
    assert redis.store["not_connected_reported:470"] == ("1", 3600)


@pytest.mark.asyncio
async def test_claims_are_per_run():
    limiter = _limiter(_FakeRedis())

    assert await limiter.claim_not_connected_report(469) is True
    assert await limiter.claim_not_connected_report(470) is True


@pytest.mark.asyncio
async def test_a_redis_error_never_suppresses_a_retry():
    limiter = _limiter(_BrokenRedis())

    assert await limiter.claim_not_connected_report(470) is True
