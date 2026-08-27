"""Provider health probe — runs every 60 s via APScheduler."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.database import dynamo_client as db
from src.config_settings import get_settings

log = logging.getLogger(__name__)

_HEALTH_TABLE = "gateway-provider-health"

# Minimal test payload for each provider protocol
_ANTHROPIC_PROBE = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}
_OPENAI_PROBE = {
    "model": "gpt-4o-mini",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


async def _probe_anthropic(api_key: str, base_url: str = "https://api.anthropic.com") -> tuple[bool, int, str]:
    url = base_url.rstrip("/") + "/v1/messages"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url,
                json=_ANTHROPIC_PROBE,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            )
        return r.status_code < 500, r.status_code, ""
    except Exception as exc:
        return False, 0, str(exc)


async def _probe_openai(api_key: str, base_url: str = "https://api.openai.com/v1") -> tuple[bool, int, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url,
                json=_OPENAI_PROBE,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        return r.status_code < 500, r.status_code, ""
    except Exception as exc:
        return False, 0, str(exc)


def _store_result(provider_id: str, healthy: bool, status_code: int, error: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        db.put_item(_HEALTH_TABLE, {
            "providerId": provider_id,
            "checkedAt": now,
            "healthy": healthy,
            "statusCode": status_code,
            "error": error,
        })
    except Exception as exc:
        log.debug("provider_health: failed to store result for %s: %s", provider_id, exc)


def get_latest_health() -> list[dict]:
    """Return the most recent health record per provider."""
    try:
        items = db.scan_items(_HEALTH_TABLE, limit=500)
    except Exception:
        return []
    by_provider: dict[str, dict] = {}
    for item in items:
        pid = item.get("providerId", "")
        existing = by_provider.get(pid)
        if not existing or item.get("checkedAt", "") > existing.get("checkedAt", ""):
            by_provider[pid] = item
    return list(by_provider.values())


async def run_health_probes() -> None:
    """Async probe all configured providers. Called by APScheduler every 60 s."""
    s = get_settings()

    # Always probe the configured Anthropic backend
    if s.anthropic_api_key:
        base = "https://api.anthropic.com" if s.llm_backend != "bedrock" else None
        if base:
            ok, code, err = await _probe_anthropic(s.anthropic_api_key, base)
            _store_result("anthropic", ok, code, err)
            log.debug("provider_health: anthropic → healthy=%s code=%s", ok, code)

    # Probe any extra providers stored in DynamoDB
    try:
        providers = db.scan_items("gateway-providers", limit=50)
    except Exception:
        providers = []

    tasks = []
    for p in providers:
        pid = p.get("providerId", "")
        protocol = p.get("protocol", "openai")
        base_url = p.get("baseUrl", "")
        api_key = p.get("apiKey", "")
        if not base_url:
            continue
        if protocol == "anthropic":
            tasks.append((pid, _probe_anthropic(api_key, base_url)))
        else:
            tasks.append((pid, _probe_openai(api_key, base_url)))

    results = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)
    for (pid, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            _store_result(pid, False, 0, str(result))
        else:
            ok, code, err = result
            _store_result(pid, ok, code, err)
            log.debug("provider_health: %s → healthy=%s code=%s", pid, ok, code)


def schedule_health_probes(scheduler: Any) -> None:
    """Register the 60-second health probe job with APScheduler."""
    try:
        scheduler.add_job(
            lambda: asyncio.run(run_health_probes()),
            "interval",
            seconds=60,
            id="gateway_health_probe",
            replace_existing=True,
        )
        log.info("provider_health: 60-second health probe job registered")
    except Exception as exc:
        log.warning("provider_health: failed to register scheduler job: %s", exc)
