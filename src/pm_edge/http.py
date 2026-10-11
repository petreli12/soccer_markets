"""Async HTTP helper: per-venue rate limiting plus retry with exponential backoff.

Read-only by construction: only GET requests and read-only batch lookups (Polymarket ``POST /books``)
are exposed. There is no code path that signs requests or touches order endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)

READ_ONLY_POST_PATHS = frozenset({"/books"})


class RateLimiter:
    """Simple async limiter that spaces requests to at most ``rate`` per second."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self.interval


class RetryableHTTPError(Exception):
    """Raised for responses worth retrying (429 and 5xx)."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"{response.status_code} for {response.request.url}")
        self.response = response


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableHTTPError | httpx.TransportError)


class ApiClient:
    """Thin wrapper over ``httpx.AsyncClient`` for one base URL."""

    def __init__(
        self,
        base_url: str,
        rate_per_second: float,
        *,
        timeout: float = 30.0,
        max_attempts: int = 6,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "pm-edge-soccer/0.1 (read-only research)"},
        )
        self._limiter = RateLimiter(rate_per_second)
        self._max_attempts = max_attempts
        self.last_response: httpx.Response | None = None

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: Any = None) -> Any:
        """GET ``path`` and return parsed JSON, retrying 429/5xx/transport errors with backoff."""
        return await self._request("GET", path, params=params)

    async def post_json_readonly(self, path: str, body: Any) -> Any:
        """POST to an explicitly allow-listed read-only lookup endpoint."""
        if path not in READ_ONLY_POST_PATHS:
            raise PermissionError(f"POST {path} is not an allow-listed read-only endpoint")
        return await self._request("POST", path, json=body)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_is_retryable),
            wait=wait_exponential_jitter(initial=1, max=60),
            stop=stop_after_attempt(self._max_attempts),
            reraise=True,
        ):
            with attempt:
                await self._limiter.wait()
                resp = await self._client.request(method, path, **kwargs)
                self.last_response = resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    log.warning("retryable %s on %s %s", resp.status_code, method, resp.request.url)
                    raise RetryableHTTPError(resp)
                resp.raise_for_status()
                return resp.json()
        raise AssertionError("unreachable")
