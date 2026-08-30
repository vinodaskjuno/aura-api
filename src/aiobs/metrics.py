"""Scoring metrics for evaluation runs.

Two families, deliberately separated:

  Heuristic — pure functions over strings. Deterministic, free, instant. These are
  the default because a metric that costs money and varies run to run is a poor
  regression gate.

  LLM-as-judge — a model scores the output (see judges.py). Necessary for
  qualities no string comparison can express (is this hallucinated? does it answer
  the question?), but non-deterministic and billable.

Every metric returns the same Score shape so a run can mix both freely.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Score:
    name: str
    value: float                 # 0.0-1.0, where 1.0 is best
    passed: bool
    reason: str = ""
    cost_usd: float = 0.0        # non-zero only for judge metrics
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_item(self) -> dict:
        return {
            "name": self.name, "value": self.value, "passed": self.passed,
            "reason": self.reason, "costUsd": self.cost_usd,
            "metadata": self.metadata,
        }


def _norm(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


# ── Heuristic metrics ────────────────────────────────────────────────────────

def exact_match(output: str, expected: str = "", **_: Any) -> Score:
    hit = _norm(output) == _norm(expected)
    return Score("exact_match", 1.0 if hit else 0.0, hit,
                 "" if hit else f"expected {expected!r}, got {str(output)[:120]!r}")


def contains(output: str, expected: str = "", **_: Any) -> Score:
    hit = _norm(expected) in _norm(output) if expected else False
    return Score("contains", 1.0 if hit else 0.0, hit,
                 "" if hit else f"{expected!r} not found in output")


def regex_match(output: str, pattern: str = "", **_: Any) -> Score:
    if not pattern:
        return Score("regex_match", 0.0, False, "no pattern supplied")
    try:
        hit = re.search(pattern, str(output or ""), re.I | re.S) is not None
    except re.error as exc:
        return Score("regex_match", 0.0, False, f"invalid pattern: {exc}")
    return Score("regex_match", 1.0 if hit else 0.0, hit,
                 "" if hit else f"pattern {pattern!r} did not match")


def is_json(output: str, **_: Any) -> Score:
    """Structured-output checks are the most common non-LLM assertion there is."""
    try:
        json.loads(str(output or ""))
        return Score("is_json", 1.0, True)
    except (TypeError, ValueError) as exc:
        return Score("is_json", 0.0, False, f"not valid JSON: {exc}")


def json_has_keys(output: str, keys: list[str] | None = None, **_: Any) -> Score:
    try:
        parsed = json.loads(str(output or ""))
    except (TypeError, ValueError) as exc:
        return Score("json_has_keys", 0.0, False, f"not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        return Score("json_has_keys", 0.0, False, "JSON is not an object")
    wanted = keys or []
    missing = [k for k in wanted if k not in parsed]
    ratio = (len(wanted) - len(missing)) / len(wanted) if wanted else 1.0
    return Score("json_has_keys", ratio, not missing,
                 f"missing keys: {missing}" if missing else "")


def not_empty(output: str, **_: Any) -> Score:
    hit = bool(_norm(output))
    return Score("not_empty", 1.0 if hit else 0.0, hit, "" if hit else "empty output")


def latency_under(latency_ms: int = 0, threshold_ms: int = 5000, **_: Any) -> Score:
    """A correct answer that takes 40 seconds still fails the user."""
    hit = latency_ms <= threshold_ms
    return Score("latency_under", 1.0 if hit else 0.0, hit,
                 "" if hit else f"{latency_ms}ms exceeds {threshold_ms}ms",
                 metadata={"latencyMs": latency_ms, "thresholdMs": threshold_ms})


def cost_under(cost_usd: float = 0.0, threshold_usd: float = 0.05, **_: Any) -> Score:
    hit = cost_usd <= threshold_usd
    return Score("cost_under", 1.0 if hit else 0.0, hit,
                 "" if hit else f"${cost_usd:.6f} exceeds ${threshold_usd:.6f}",
                 metadata={"costUsd": cost_usd, "thresholdUsd": threshold_usd})


def levenshtein_ratio(output: str, expected: str = "", **_: Any) -> Score:
    """Similarity without a dependency — difflib is in the standard library and
    close enough for a regression signal."""
    from difflib import SequenceMatcher
    a, b = _norm(output), _norm(expected)
    if not a and not b:
        ratio = 1.0
    else:
        ratio = SequenceMatcher(None, a, b).ratio()
    return Score("levenshtein_ratio", round(ratio, 4), ratio >= 0.8,
                 "" if ratio >= 0.8 else f"similarity {ratio:.2f} below 0.80")


HEURISTICS: dict[str, Callable[..., Score]] = {
    "exact_match": exact_match,
    "contains": contains,
    "regex_match": regex_match,
    "is_json": is_json,
    "json_has_keys": json_has_keys,
    "not_empty": not_empty,
    "latency_under": latency_under,
    "cost_under": cost_under,
    "levenshtein_ratio": levenshtein_ratio,
}


def run_heuristic(name: str, **kwargs: Any) -> Score:
    fn = HEURISTICS.get(name)
    if fn is None:
        return Score(name, 0.0, False, f"unknown metric {name!r}")
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 — a broken metric must not fail the run
        return Score(name, 0.0, False, f"metric raised: {exc}")


def aggregate(scores: list[Score]) -> dict:
    """Per-metric mean and pass rate, plus an overall. What the summary row shows."""
    by_name: dict[str, list[Score]] = {}
    for s in scores:
        by_name.setdefault(s.name, []).append(s)

    metrics = {}
    for name, group in by_name.items():
        metrics[name] = {
            "mean": round(sum(s.value for s in group) / len(group), 4),
            "passRate": round(sum(1 for s in group if s.passed) / len(group), 4),
            "count": len(group),
            "costUsd": round(sum(s.cost_usd for s in group), 6),
        }
    total = len(scores)
    return {
        "metrics": metrics,
        "overallPassRate": round(sum(1 for s in scores if s.passed) / total, 4) if total else 0.0,
        "totalScores": total,
        "totalCostUsd": round(sum(s.cost_usd for s in scores), 6),
    }
