"""A QA run, as an OpenTelemetry trace.

A run already emits a precise, structured event stream — `service.execute` calls its
local `emit()` closure at every phase boundary, and that stream is what the live
WebSocket renders. It was never stored as anything an operator could look at
afterwards: the run row keeps a status, a tally and a console blob, so "why did last
Tuesday's run take nine minutes" had no answer.

This turns that same stream into spans and posts them to Aura's own OTLP receiver.
Nothing new is measured; the existing events are simply given a shape the AI Traces
page can already read.

THE STREAM IS THE PRODUCT, THIS IS NOT. `observe()` and `flush()` swallow everything.
A run must not fail, slow down or change its output because a trace could not be
written — the same rule `routers/otlp.py` states for the receiving end, applied at
the sending end.

WHY PHASE SPANS ARE EVENT-TO-EVENT. A run reports when a phase *finished*, not when
it began, so each span runs from the previous event to this one. That is an honest
rendering of a sequential run — the gap between two reports is the time spent getting
from one to the other — but it is an attribution, not a measurement, and a phase that
overlaps another (nothing in `service.execute` does today) would be drawn wrongly.

Emitted spans are ordinary `chain`/`tool` spans as far as `aiobs/ingest.py` is
concerned: they carry no model and no tokens, so they cost nothing and cannot be
mistaken for LLM traffic. A run's model calls, when it makes any, go through the
gateway and are attributed by `X-Aura-Project-Id` instead.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any

log = logging.getLogger(__name__)

#: Where to send, and what to send it with. Set by `qatest/agent.py` from its own
#: `--api`/`--key` so a self-hosted runner needs no extra configuration, and read
#: from settings when a run executes in-process on the server.
ENDPOINT_ENV = "AURA_OTLP_ENDPOINT"
KEY_ENV = "AURA_OTLP_KEY"

#: Bounded so a run with hundreds of cases cannot grow the payload without limit.
#: A run past this many spans has already told the reader what they needed.
MAX_SPANS = 500

#: Event types that close a phase and open the next one. Anything else is recorded
#: as an attribute on the run rather than as a span of its own — `evidence` fires
#: per line and would otherwise bury the timeline in one-line spans.
_PHASE_EVENTS = {"plan", "planned", "emulator", "app", "running", "step", "graph"}

_HTTP_TIMEOUT_S = 5.0


def _hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


def _endpoint() -> str:
    base = os.environ.get(ENDPOINT_ENV, "").strip()
    if not base:
        try:
            from src.config_settings import get_settings
            base = str(getattr(get_settings(), "public_base_url", "") or "").strip()
        except Exception:                                     # noqa: BLE001
            return ""
    if not base:
        return ""
    return base.rstrip("/") + "/otlp/v1/traces"


def _key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def _attr(key: str, value: Any) -> dict:
    """One OTLP attribute. Ints stay ints so `_anyval` does not stringify a count."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)[:1000]}}


class RunTracer:
    """Collects a run's events as spans and posts them once, at the end.

    Buffered rather than streamed on purpose. A run is bounded and short, the runner
    is on someone's laptop behind NAT, and an exporter that fires per event would put
    a network round trip inside the run's own critical path — which is precisely the
    thing this must never do.
    """

    def __init__(self, project_id: str, run_id: str, *,
                 endpoint: str = "", api_key: str = "") -> None:
        self.project_id = str(project_id or "")
        self.run_id = str(run_id or "")
        self.endpoint = endpoint or _endpoint()
        self.api_key = api_key or _key()
        self.trace_id = _hex(16)
        self.root_span_id = _hex(8)
        self.started_ns = time.time_ns()
        self._last_ns = self.started_ns
        self._spans: list[dict] = []
        self._status = ""
        self._dropped = 0

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.api_key and self.project_id)

    # ── collection ──────────────────────────────────────────────────────────

    def observe(self, event: dict) -> None:
        """Record one `emit()` event. Never raises, never blocks on the network."""
        if not self.enabled:
            return
        try:
            self._observe(event)
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest tracing: dropped an event: %s", exc)

    def _observe(self, event: dict) -> None:
        etype = str(event.get("type") or "")
        now = time.time_ns()

        if etype == "done":
            self._status = str(event.get("status") or "")
            self._close(now, event)
            return

        if etype not in _PHASE_EVENTS:
            self._last_ns = max(self._last_ns, now)
            return

        if len(self._spans) >= MAX_SPANS:
            self._dropped += 1
            self._last_ns = now
            return

        name = self._name_for(etype, event)
        failed = self._failed(etype, event)
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": _hex(8),
            "parentSpanId": self.root_span_id,
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(self._last_ns),
            "endTimeUnixNano": str(now),
            "attributes": self._attrs_for(etype, event),
        }
        if failed:
            span["status"] = {"code": 2, "message": str(failed)[:400]}
        self._spans.append(span)
        self._last_ns = now

    @staticmethod
    def _name_for(etype: str, event: dict) -> str:
        if etype == "step":
            # The case name, so the waterfall reads as the plan rather than as
            # "step" repeated forty times.
            return f"case: {str(event.get('name') or event.get('case') or 'unnamed')[:120]}"
        if etype == "app":
            stage = str(event.get("stage") or "")
            return f"app: {stage or event.get('kind') or 'start'}"
        if etype == "emulator":
            return f"emulator: {event.get('cloud') or event.get('name') or 'setup'}"
        return etype

    @staticmethod
    def _failed(etype: str, event: dict) -> str:
        if event.get("error"):
            return str(event["error"])
        if etype == "step" and str(event.get("status") or "").lower() in ("failed", "error"):
            return str(event.get("message") or "case failed")
        if etype == "app" and event.get("started") is False:
            return str(event.get("message") or "the app did not start")
        return ""

    def _attrs_for(self, etype: str, event: dict) -> list[dict]:
        out = [_attr("aura.run_id", self.run_id), _attr("aura.event", etype)]
        # `message` is what the live stream shows a reader, so it is what a span
        # should show them too. Everything else is copied verbatim but bounded.
        for key in ("message", "status", "kind", "name", "cloud", "url", "cases",
                    "planTotal", "passed", "failed", "skipped", "reason", "stage"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                out.append(_attr(f"qa.{key}", value))
        return out

    def _close(self, now: int, event: dict) -> None:
        root: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.root_span_id,
            "name": f"qa run: {self.project_id}",
            "kind": 1,
            "startTimeUnixNano": str(self.started_ns),
            "endTimeUnixNano": str(now),
            "attributes": [
                _attr("aura.run_id", self.run_id),
                _attr("aura.project", self.project_id),
                _attr("qa.status", str(event.get("status") or "")),
                _attr("qa.passed", int(event.get("passed") or 0)),
                _attr("qa.failed", int(event.get("failed") or 0)),
                _attr("qa.spans_dropped", self._dropped),
            ],
        }
        if str(event.get("status") or "").lower() in ("failed", "error"):
            root["status"] = {"code": 2,
                              "message": str(event.get("reason") or "run failed")[:400]}
        # Root last: `assemble()` picks the root as the span whose parent is absent
        # from the batch, and order is otherwise irrelevant to it.
        self._spans.append(root)

    # ── delivery ────────────────────────────────────────────────────────────

    def payload(self) -> dict:
        """The OTLP/JSON body. Public so a test can assert on it without a network."""
        return {
            "resourceSpans": [{
                "resource": {"attributes": [
                    # `aura.project` first by contract: `aiobs.service.project_of`
                    # checks it before `service.name`, and it is what joins these
                    # traces to the Aura project rather than to a bare string.
                    _attr("aura.project", self.project_id),
                    _attr("service.name", f"aura-qa/{self.project_id}"),
                    _attr("session.id", self.run_id),
                ]},
                "scopeSpans": [{
                    "scope": {"name": "aura.qatest"},
                    "spans": self._spans,
                }],
            }],
        }

    def flush(self) -> bool:
        """Post the run's spans. Returns True only on a real 2xx."""
        if not self.enabled or not self._spans:
            return False
        try:
            import httpx
            response = httpx.post(
                self.endpoint, json=self.payload(),
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=_HTTP_TIMEOUT_S,
            )
            ok = 200 <= response.status_code < 300
            if not ok:
                log.debug("qatest tracing: receiver answered %s", response.status_code)
            return ok
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest tracing: flush failed: %s", exc)
            return False


def tracer_for(project_id: str, run_id: str) -> RunTracer:
    """A tracer for this run — disabled, and therefore free, when unconfigured."""
    return RunTracer(project_id, run_id)
