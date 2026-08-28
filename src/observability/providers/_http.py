"""Shared HTTP plumbing for provider adapters.

Built on httpx (already a dependency) rather than each vendor's SDK: those are
enormous for four endpoints apiece and pin their own transitive httpx/urllib3.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.config_settings import get_settings
from src.observability.base import ProviderError

log = logging.getLogger(__name__)


class HttpMixin:
    """Timeout, auth headers, and provider-native error translation."""

    provider_id: str = ""

    def _timeout(self) -> int:
        return get_settings().observability_query_timeout_s

    async def _request(self, method: str, url: str, *, headers: dict | None = None,
                       params: dict | None = None, json_body: Any = None,
                       auth: tuple[str, str] | None = None) -> tuple[Any, int]:
        """Returns (parsed_json, elapsed_ms). Raises ProviderError on failure."""
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                resp = await client.request(method, url, headers=headers or {},
                                            params=params, json=json_body, auth=auth)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"timed out after {self._timeout()}s",
                                provider_id=self.provider_id) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc), provider_id=self.provider_id) from exc

        elapsed = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            body = (resp.text or "")[:300]
            raise ProviderError(f"HTTP {resp.status_code}: {body}",
                                provider_id=self.provider_id, status=resp.status_code)
        try:
            return resp.json(), elapsed
        except Exception:  # noqa: BLE001 — some /ready endpoints return plain text
            return {"_text": resp.text}, elapsed

    async def _probe(self, url: str, headers: dict | None = None,
                     auth: tuple[str, str] | None = None) -> tuple[bool, int, str]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers=headers or {}, auth=auth)
            ok = r.status_code < 400
            return ok, int((time.monotonic() - t0) * 1000), "" if ok else f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, int((time.monotonic() - t0) * 1000), str(exc)
