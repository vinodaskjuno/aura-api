"""Sliding-window rate limiter for the AURA AI Gateway.

Tracks requests per user in memory using a deque of timestamps.

TWO KNOWN LIMITATIONS, both deliberate for now:

1. PER-PROCESS. State lives in this module, so with N uvicorn workers or ECS
   tasks the effective limit is rate_limit_rpm x N. Move to DynamoDB or Redis
   before treating this as an enforcement boundary rather than a safety valve.

2. Applies only to the /gateway wire routes. The OTLP telemetry receiver is
   deliberately exempt: throttling it would make an exporter retry in a loop and
   surface errors inside the user's Claude Code session.

Idle user entries are reaped so the dicts cannot grow without bound — they
previously kept one deque and one asyncio.Lock per user id forever.
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

_WINDOW_SECS = 60.0
# Reap when the tracked-user count crosses this; cheap and rare.
_REAP_THRESHOLD = 5_000
_last_reap = monotonic()
_REAP_INTERVAL = 300.0


def _maybe_reap(now: float) -> None:
    """Drop users with no activity inside the window."""
    global _last_reap
    if len(_windows) < _REAP_THRESHOLD or now - _last_reap < _REAP_INTERVAL:
        return
    _last_reap = now
    stale = [
        uid for uid, dq in _windows.items()
        if not dq or now - dq[-1] >= _WINDOW_SECS
    ]
    for uid in stale:
        # Never reap a lock that is currently held — the holder would then be
        # serialising on a different lock object than the next caller.
        lock = _locks.get(uid)
        if lock is not None and lock.locked():
            continue
        _windows.pop(uid, None)
        _locks.pop(uid, None)
    if stale:
        log.info("rate_limiter: reaped %d idle users (%d tracked)", len(stale), len(_windows))


async def check(user_id: str) -> None:
    """Raise HTTP 429 if the user has exceeded their RPM limit."""
    s = get_settings()
    rpm = s.rate_limit_rpm
    window_secs = _WINDOW_SECS
    now = monotonic()
    _maybe_reap(now)

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
