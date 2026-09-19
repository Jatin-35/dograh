"""SmartFlo bearer-token minting and caching.

SmartFlo has no long-lived API token. Every authenticated call needs a bearer
token from ``POST /v1/auth/login`` (email + password), and that token expires —
``expires_in`` is around an hour in their documentation.

Two things make this worth its own module rather than inlining:

1. **Every refresh spends rate-limit budget.** SmartFlo's limit is *combined
   across all APIs* and equals the account's CPS — on a CPS-3 trunk that is
   three requests per second total, shared with dialling, transfers and
   hangups. A token fetched per request would eat the budget that places calls.

2. **A stampede is easy to cause.** A campaign dialling concurrently would
   otherwise send every worker to /v1/auth/login the moment a token expires.
   One lock per credential keeps that to a single refresh.

Tokens are cached in-process. Workers therefore hold one token each rather than
sharing via Redis — deliberate: a token is small, cheap to mint, and sharing it
would mean writing a credential to Redis for no real saving.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp
from loguru import logger

# Refresh this far before the stated expiry. A token that expires mid-flight
# fails the call it was fetched for, and SmartFlo's clock is not ours.
_EXPIRY_MARGIN_SECONDS = 120

# If SmartFlo omits expires_in, assume something short rather than something
# convenient — an over-long guess fails calls, an over-short one costs a refresh.
_DEFAULT_TTL_SECONDS = 1800


@dataclass
class _CachedToken:
    token: str
    expires_at: float

    @property
    def is_usable(self) -> bool:
        return time.monotonic() < self.expires_at - _EXPIRY_MARGIN_SECONDS


class TataSmartfloAuthError(RuntimeError):
    """Raised when SmartFlo refuses the configured credentials."""


class TataSmartfloTokenCache:
    """Mints and caches bearer tokens, one entry per (api_base, email)."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], _CachedToken] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        # setdefault rather than get-or-create: two coroutines racing here would
        # otherwise each build a lock and neither would exclude the other.
        return self._locks.setdefault(key, asyncio.Lock())

    async def get_token(
        self, api_base: str, email: str, password: str, *, force_refresh: bool = False
    ) -> str:
        """Return a usable bearer token, minting one if needed.

        Args:
            api_base: SmartFlo API base URL.
            email: Login id.
            password: Account password.
            force_refresh: Skip the cache. Use when SmartFlo has just answered
                401 — the cached token may be valid by our clock and rejected
                by theirs.
        """
        key = (api_base.rstrip("/"), email)

        if not force_refresh:
            cached = self._tokens.get(key)
            if cached and cached.is_usable:
                return cached.token

        async with self._lock_for(key):
            # Re-check inside the lock: whoever held it may have just refreshed,
            # and a second login would be wasted rate-limit budget.
            if not force_refresh:
                cached = self._tokens.get(key)
                if cached and cached.is_usable:
                    return cached.token

            token, ttl = await self._login(api_base, email, password)
            self._tokens[key] = _CachedToken(
                token=token, expires_at=time.monotonic() + ttl
            )
            logger.info(
                f"SmartFlo token minted for {email} at {api_base} (ttl={ttl}s)"
            )
            return token

    async def _login(self, api_base: str, email: str, password: str) -> tuple[str, int]:
        endpoint = f"{api_base.rstrip('/')}/v1/auth/login"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json={"email": email, "password": password},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status != 200:
                        # Never log the body: SmartFlo echoes the login id, and
                        # a misconfigured field could put the password in it.
                        raise TataSmartfloAuthError(
                            f"SmartFlo login failed with HTTP {response.status}"
                        )
        except aiohttp.ClientError as exc:
            raise TataSmartfloAuthError(f"SmartFlo login request failed: {exc}") from exc

        token = (body or {}).get("access_token")
        if not token:
            raise TataSmartfloAuthError(
                "SmartFlo login returned no access_token; check the credentials "
                "and whether the 90-day password expiry has elapsed."
            )

        try:
            ttl = int((body or {}).get("expires_in") or _DEFAULT_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = _DEFAULT_TTL_SECONDS

        return token, max(ttl, _EXPIRY_MARGIN_SECONDS + 60)

    def invalidate(self, api_base: str, email: str) -> None:
        """Drop a cached token, so the next call mints a fresh one."""
        self._tokens.pop((api_base.rstrip("/"), email), None)


# Module-level so every provider instance in this worker shares one cache.
# Provider objects are constructed per call; a per-instance cache would mint a
# token per call and defeat the point.
token_cache = TataSmartfloTokenCache()
