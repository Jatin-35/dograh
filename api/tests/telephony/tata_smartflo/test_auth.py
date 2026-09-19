"""Token minting and caching for TATA SmartFlo.

SmartFlo issues no long-lived API token — every authenticated call needs a
bearer from ``/v1/auth/login`` that expires in about an hour. Two things make
the cache load-bearing rather than an optimisation:

* SmartFlo's rate limit is *combined across all APIs* and equals the account's
  CPS. On a CPS-3 trunk that is three requests per second covering auth,
  dialling, transfers and hangups together. Tokens fetched per request would
  eat the budget that places calls.
* A campaign dialling concurrently would otherwise send every worker to
  /v1/auth/login the moment a token expires.

So the tests that matter here are about concurrency and expiry, not happy path.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.providers.tata_smartflo.auth import (
    TataSmartfloAuthError,
    TataSmartfloTokenCache,
)

_API_BASE = "https://api-smartflo.tatateleservices.com"
_EMAIL = "ops@example.test"
_PASSWORD = "placeholder-password"


def _cache_with_login(token="tok-1", ttl=3600):
    """A cache whose network login is replaced by a counted stub."""
    cache = TataSmartfloTokenCache()
    login = AsyncMock(return_value=(token, ttl))
    cache._login = login  # noqa: SLF001 - the seam under test
    return cache, login


@pytest.mark.asyncio
async def test_a_token_is_minted_once_and_then_reused():
    cache, login = _cache_with_login()

    first = await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)
    second = await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)

    assert first == second == "tok-1"
    login.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_callers_mint_exactly_one_token():
    """The stampede case.

    Twenty workers starting a campaign must produce one login, not twenty —
    nineteen wasted requests is most of a CPS-3 second.
    """
    cache, login = _cache_with_login()

    async def slow_login(*_args, **_kwargs):
        await asyncio.sleep(0.05)  # long enough for the others to pile up
        return ("tok-1", 3600)

    login.side_effect = slow_login

    tokens = await asyncio.gather(
        *(cache.get_token(_API_BASE, _EMAIL, _PASSWORD) for _ in range(20))
    )

    assert set(tokens) == {"tok-1"}
    assert login.await_count == 1


@pytest.mark.asyncio
async def test_different_credentials_do_not_share_a_token():
    """Two orgs on one worker must never be handed each other's token."""
    cache, login = _cache_with_login()
    login.side_effect = [("tok-a", 3600), ("tok-b", 3600)]

    a = await cache.get_token(_API_BASE, "a@example.test", "pw")
    b = await cache.get_token(_API_BASE, "b@example.test", "pw")

    assert a == "tok-a"
    assert b == "tok-b"
    assert login.await_count == 2


@pytest.mark.asyncio
async def test_a_token_close_to_expiry_is_refreshed_early():
    """Refreshing at the stated expiry is too late — the call is already in flight.

    A token with less than the safety margin left is treated as unusable.
    """
    cache, login = _cache_with_login()
    # 60s of life left, which is inside the 120s margin.
    login.side_effect = [("tok-old", 60), ("tok-new", 3600)]

    first = await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)
    second = await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)

    assert first == "tok-old"
    assert second == "tok-new"
    assert login.await_count == 2


@pytest.mark.asyncio
async def test_force_refresh_bypasses_a_cached_token():
    """Used after a 401: our clock said the token was good, SmartFlo disagreed."""
    cache, login = _cache_with_login()
    login.side_effect = [("tok-1", 3600), ("tok-2", 3600)]

    assert await cache.get_token(_API_BASE, _EMAIL, _PASSWORD) == "tok-1"
    assert (
        await cache.get_token(_API_BASE, _EMAIL, _PASSWORD, force_refresh=True)
        == "tok-2"
    )


@pytest.mark.asyncio
async def test_invalidate_drops_the_cached_token():
    cache, login = _cache_with_login()
    login.side_effect = [("tok-1", 3600), ("tok-2", 3600)]

    await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)
    cache.invalidate(_API_BASE, _EMAIL)

    assert await cache.get_token(_API_BASE, _EMAIL, _PASSWORD) == "tok-2"


@pytest.mark.asyncio
async def test_a_missing_expires_in_does_not_produce_an_immortal_token():
    """An over-long guess fails calls; an over-short one costs one refresh."""
    cache = TataSmartfloTokenCache()

    class _Response:
        status = 200

        async def json(self, content_type=None):
            return {"access_token": "tok-1"}  # no expires_in

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    with patch(
        "api.services.telephony.providers.tata_smartflo.auth.aiohttp.ClientSession",
        return_value=_Session(),
    ):
        token = await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)

    assert token == "tok-1"
    cached = cache._tokens[(_API_BASE, _EMAIL)]  # noqa: SLF001
    remaining = cached.expires_at - time.monotonic()
    assert 0 < remaining < 3600, "a token with no stated expiry must not be immortal"


@pytest.mark.asyncio
async def test_a_login_rejection_raises_rather_than_returning_empty():
    cache = TataSmartfloTokenCache()

    class _Response:
        status = 401

        async def json(self, content_type=None):
            return {"message": "Invalid credentials"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    with patch(
        "api.services.telephony.providers.tata_smartflo.auth.aiohttp.ClientSession",
        return_value=_Session(),
    ):
        with pytest.raises(TataSmartfloAuthError, match="HTTP 401"):
            await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)


@pytest.mark.asyncio
async def test_a_login_failure_never_echoes_the_response_body():
    """SmartFlo echoes the login id, and a misconfigured field could carry the
    password. The error says the status and nothing else."""
    cache = TataSmartfloTokenCache()

    class _Response:
        status = 403

        async def json(self, content_type=None):
            return {"email": _EMAIL, "password_hint": _PASSWORD}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    with patch(
        "api.services.telephony.providers.tata_smartflo.auth.aiohttp.ClientSession",
        return_value=_Session(),
    ):
        with pytest.raises(TataSmartfloAuthError) as exc:
            await cache.get_token(_API_BASE, _EMAIL, _PASSWORD)

    assert _PASSWORD not in str(exc.value)
    assert _EMAIL not in str(exc.value)
