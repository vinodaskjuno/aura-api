"""Deterministic correlation — NO LLM anywhere in this module.

This is the single most important design choice in the feature. It is what the eval
harness can score exactly, and it changes the model's job from "invent a causal story"
to "explain this timeline", which is where models are actually reliable.

Change-point detection is a rolling-mean shift with a CUSUM-style guard: no scipy,
no numpy, nothing added to requirements.txt.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from src.observability.types import (
    EventRecord, EvidenceRecord, LogRecord, MetricSeries, TimeWindow, TraceSummary,
)

log = logging.getLogger(__name__)

# Normalisation rules applied before counting error signatures. Order matters:
# UUIDs and hex ids first, then long numbers, then timestamps.
_NORMALIZERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    # Mask tokens BEFORE the numeric rules, or AURA_POD_01 degrades to AURA_POD_<n>
    # and two references to the same pod stop collapsing together.
    (re.compile(r"\bAURA_[A-Z]+_\d{1,4}\b"), "<id>"),
    # Lookarounds rather than \b: logs are full of unit-suffixed numbers ("13ms",
    # "5GB", "checkout-7"), and \b never fires between a digit and a letter.
    (re.compile(r"(?<!\d)\d+\.\d+(?!\d)"), "<float>"),
    (re.compile(r"(?<!\d)\d+(?!\d)"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize_message(body: str, max_len: int = 160) -> str:
    """Collapse a log line to its shape so 500 lines become ~8 signatures."""
    text = body or ""
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.strip()[:max_len]


def error_signatures(records: list[LogRecord], top: int = 10) -> list[dict]:
    """Group logs by normalized shape. This is what makes logs fit in a prompt."""
    buckets: dict[str, dict[str, Any]] = {}
    for r in records:
        sig = normalize_message(r.body)
        if not sig:
            continue
        b = buckets.setdefault(sig, {
            "signature": sig, "count": 0, "level": r.level,
            "first_seen": r.timestamp, "last_seen": r.timestamp,
            "record_ids": [], "services": set(),
        })
        b["count"] += 1
        b["first_seen"] = min(b["first_seen"], r.timestamp)
        b["last_seen"] = max(b["last_seen"], r.timestamp)
        if len(b["record_ids"]) < 5:
            b["record_ids"].append(r.record_id)
        b["services"].add(r.service)
        if r.level in ("ERROR", "FATAL"):
            b["level"] = r.level

    out = sorted(buckets.values(), key=lambda b: b["count"], reverse=True)[:top]
    for b in out:
        b["services"] = sorted(x for x in b["services"] if x)
    return out


# ── Change-point detection ───────────────────────────────────────────────────

def detect_change_point(series: MetricSeries, min_delta_pct: float = 25.0) -> dict | None:
    """Find the index where the rolling mean shifts most, if the shift is material."""
    pts = [p for p in series.points if p.value is not None]
    n = len(pts)
    if n < 6:
        return None

    best_idx, best_score = None, 0.0
    total = sum(p.value for p in pts)
    for i in range(2, n - 2):
        left = pts[:i]
        right = pts[i:]
        mean_l = sum(p.value for p in left) / len(left)
        mean_r = sum(p.value for p in right) / len(right)
        # Weight by how balanced the split is, so edge noise doesn't win.
        balance = (len(left) * len(right)) / (n * n)
        score = abs(mean_r - mean_l) * balance
        if score > best_score:
            best_score, best_idx = score, i

    if best_idx is None:
        return None

    left = pts[:best_idx]
    right = pts[best_idx:]
    before = sum(p.value for p in left) / len(left)
    after = sum(p.value for p in right) / len(right)
    if before == 0:
        delta_pct = 100.0 if after else 0.0
    else:
        delta_pct = (after - before) / abs(before) * 100.0

    if abs(delta_pct) < min_delta_pct:
        return None

    return {
        "metric": series.metric,
        "service": series.service,
        "series_id": series.series_id,
        "change_point_t": pts[best_idx].timestamp,
        "before": round(before, 4),
        "after": round(after, 4),
        "delta_pct": round(delta_pct, 2),
        "direction": "up" if delta_pct > 0 else "down",
        "unit": series.unit,
        "source_url": series.source_url,
        "mean_total": round(total / n, 4),
    }


def find_anomalies(series_list: list[MetricSeries],
                   evidence_by_key: dict[str, str] | None = None) -> list[dict]:
    out = []
    for s in series_list:
        cp = detect_change_point(s)
        if cp:
            ev = (evidence_by_key or {}).get(s.series_id)
            cp["evidence_ids"] = [ev] if ev else []
            out.append(cp)
    return sorted(out, key=lambda a: abs(a["delta_pct"]), reverse=True)


# ── Deploy / change alignment ────────────────────────────────────────────────

def _parse(ts: str):
    try:
        return TimeWindow.parse(ts)
    except Exception:  # noqa: BLE001
        return None


def align_changes(events: list[EventRecord], error_onset_t: str | None,
                  anomalies: list[dict],
                  evidence_by_key: dict[str, str] | None = None,
                  max_lead_s: int = 1800) -> list[dict]:
    """Score change events by how well they precede the observed onset.

    A deploy 4 minutes before the knee scores far higher than one 25 minutes before.
    """
    onset = _parse(error_onset_t) if error_onset_t else None
    anomaly_times = [_parse(a["change_point_t"]) for a in anomalies]
    anomaly_times = [t for t in anomaly_times if t]

    suspects = []
    for e in events:
        if e.kind not in ("deploy", "config_change", "scale", "release"):
            continue
        et = _parse(e.timestamp)
        if not et:
            continue

        targets = ([onset] if onset else []) + anomaly_times
        if not targets:
            lead_s, score = None, 0.35
        else:
            leads = [(t - et).total_seconds() for t in targets]
            forward = [l for l in leads if 0 <= l <= max_lead_s]
            if not forward:
                continue                      # change came after, or far too early
            lead_s = min(forward)
            # Linear decay: immediately-before = 1.0, at the horizon = 0.
            score = round(max(0.0, 1.0 - (lead_s / max_lead_s)), 3)

        ev = (evidence_by_key or {}).get(e.event_id)
        suspects.append({
            "event_id": e.event_id,
            "kind": e.kind,
            "service": e.service,
            "version": e.version,
            "actor": e.actor,
            "t": e.timestamp,
            "title": e.title,
            "lead_time_s": int(lead_s) if lead_s is not None else None,
            "score": score,
            "source_url": e.source_url,
            "evidence_ids": [ev] if ev else [],
        })
    return sorted(suspects, key=lambda s: s["score"], reverse=True)


def error_onset(records: list[LogRecord]) -> str | None:
    """Timestamp of the first ERROR/FATAL log in the window."""
    errs = sorted([r.timestamp for r in records if r.level in ("ERROR", "FATAL")])
    return errs[0] if errs else None


# ── Timeline ─────────────────────────────────────────────────────────────────

def build_timeline(evidence: list[EvidenceRecord], limit: int = 200) -> list[dict]:
    """One merged, time-ordered view across every signal."""
    rows = [{
        "t": e.timestamp,
        "kind": {"logs": "log", "metrics": "metric",
                 "traces": "trace", "events": "deploy"}.get(e.signal, e.signal),
        "signal": e.signal,
        "service": e.service,
        "summary": e.summary,
        "evidence_ids": [e.evidence_id],
        "source_url": e.source_url,
        "weight": e.weight,
    } for e in evidence]
    rows.sort(key=lambda r: (r["t"], -r["weight"]))
    return rows[:limit]


def blast_radius(service: str, hops: int = 2) -> dict:
    try:
        from src.graph.neo4j_client import service_neighbours
        return service_neighbours(service, hops=hops)
    except Exception as exc:  # noqa: BLE001
        log.debug("blast_radius unavailable: %s", exc)
        return {"upstream": [], "downstream": [], "hops": hops, "source": "unavailable"}


def trace_error_summary(traces: list[TraceSummary]) -> dict:
    if not traces:
        return {"count": 0, "error_traces": 0, "p95_ms": 0.0, "services_touched": []}
    durations = sorted(t.duration_ms for t in traces)
    idx = min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))
    touched: set[str] = set()
    for t in traces:
        touched.update(t.services_touched or [])
    return {
        "count": len(traces),
        "error_traces": sum(1 for t in traces if t.status == "error"),
        "p95_ms": round(durations[idx], 2),
        "max_ms": round(durations[-1], 2),
        "services_touched": sorted(x for x in touched if x),
    }
