"""Shared HTTP resilience helpers for provider adapters."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx


async def request_with_retries(
    request: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int,
    backoff: float,
) -> httpx.Response:
    """Run an HTTP request with retries for transient failures."""

    for attempt in range(max_retries + 1):
        try:
            response = await request()
        except httpx.RequestError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(backoff * (2**attempt) + random.random() * 0.25)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt < max_retries:
                retry_after = response.headers.get("Retry-After")
                delay = _retry_after_seconds(retry_after)
                await asyncio.sleep(delay or backoff * (2**attempt) + random.random() * 0.25)
                continue
        return response

    raise AssertionError("HTTP request loop completed without a response")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if delay >= 0 else None
