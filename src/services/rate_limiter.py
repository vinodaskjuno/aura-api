"""Sliding-window rate limiter for the AURA AI Gateway.

Tracks requests per user in memory using a deque of timestamps.
For distributed deployments, swap the in-memory store for DynamoDB/Redis.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from time import monotonic

from fastapi import HTTPException

from src.config_settings import get_settings

log = logging.getLogger(__name__)

# user_id → deque of monotonic timestamps (seconds)
_windows: dict[str, deque] = defaultdict(deque)
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def check(user_id: str) -> None:
    """Raise HTTP 429 if the user has exceeded their RPM limit."""
    s = get_settings()
    rpm = s.rate_limit_rpm
    window_secs = 60.0
    now = monotonic()

    async with _locks[user_id]:
        dq = _windows[user_id]
        # Evict timestamps older than 60 s
        while dq and now - dq[0] >= window_secs:
            dq.popleft()

        if len(dq) >= rpm:
            retry_after = int(window_secs - (now - dq[0])) + 1
            log.warning("Rate limit hit for user %s (%d/%d RPM)", user_id, len(dq), rpm)
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({rpm} RPM). Retry after {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

        dq.append(now)
