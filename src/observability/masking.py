"""Reversible identifier masking for external LLM egress.

Token format: AURA_POD_01, AURA_HOST_04, AURA_IP_12, AURA_ACCT_01, ...
Matches \\bAURA_[A-Z]+_\\d{1,4}\\b with zero ambiguity against real log content.

Chosen over ⟪k7:pod:3⟫ / <<POD_1>> because uppercase-underscore ASCII tokenizes into
a small stable token set across every provider's tokenizer (unicode brackets fragment
unpredictably and get mangled through JSON escaping and SSE), and because the token
CARRIES THE ENTITY TYPE — "AURA_POD_01 and AURA_POD_02 both OOMKilled on AURA_HOST_04"
stays analyzable, whereas "<<T7>> and <<T9>> on <<T3>>" does not.

Numbering is per-session, per-type, in first-seen order, so token indices are stable
across a rerun of the same investigation — which the eval harness depends on.

WHAT IS NOT MASKED: service names. They are the join key to the knowledge graph, the
vocabulary of the runbooks, and what the analysis is *about*. Masking them destroys
the model's ability to reason about topology and produces a root cause you cannot act
on. `service` exists as an opt-in class for regulated deployments; the cost is a
materially worse RCA.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Iterable

from src.config_settings import get_settings

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"\bAURA_[A-Z]+_\d{1,4}\b")

# class -> (token infix, detection regex, capture-group index)
# The group index is explicit rather than inferred from `m.groups()`: several
# patterns bracket the identifier with surrounding syntax that must survive
# (namespace=, service=, https://), and inferring it silently mangled pod names.
_PATTERNS: dict[str, tuple[str, re.Pattern, int]] = {
    "email": ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), 0),
    # Whole authority of a URL, so a hostname inside one is never masked twice.
    "url_host": ("URLHOST", re.compile(r"https?://([\w.-]+(?::\d+)?)"), 1),
    # k8s pod: <name>-<replicaset hash>-<suffix>. No capture group by design.
    "pod": ("POD", re.compile(r"\b[a-z0-9][-a-z0-9]{1,50}-[a-f0-9]{6,10}-[a-z0-9]{5}\b"), 0),
    "cluster": ("CLUSTER", re.compile(
        r"\b(?:arn:aws:eks:[a-z0-9-]+:\d{12}:cluster/[\w-]+"
        r"|[a-z0-9][\w-]*-(?:cluster|eks|gke|aks)[\w-]*"
        r"|(?:cluster|eks|gke|aks)-[\w-]+)\b", re.IGNORECASE), 0),
    "host": ("HOST", re.compile(
        r"\b(?:ip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}[\w.-]*"
        r"|[a-z0-9][\w-]*\.(?:internal|local|ec2\.internal|compute\.internal|svc\.cluster\.local))\b",
        re.IGNORECASE), 0),
    "ip": ("IP", re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"), 0),
    "account": ("ACCT", re.compile(r"\b\d{12}\b"), 0),
    "namespace": ("NS", re.compile(r"(?:namespace[=:\s\"\']+)([a-z0-9][-a-z0-9]{1,40})",
                                   re.IGNORECASE), 1),
    # Opt-in only — see the module docstring.
    "service": ("SVC", re.compile(r"(?:service[=:\s\"\']+)([a-z0-9][-a-z0-9]{1,40})",
                                  re.IGNORECASE), 1),
}

# Canonical substitution order, independent of however the settings string is
# written. Broad-authority classes run BEFORE the narrower ones they can contain
# (url_host before host, pod before host), otherwise tokens nest inside tokens and
# a single-pass unmask cannot restore them.
_CLASS_ORDER = ("email", "url_host", "pod", "cluster", "host", "ip",
                "account", "namespace", "service")

DEFAULT_CLASSES = ("pod", "host", "ip", "account", "cluster", "namespace", "email", "url_host")


class MaskingError(Exception):
    """Masking failed. Callers MUST abort the LLM call — never send raw data."""


class MaskSession:
    """Bidirectional, order-stable identifier map for one investigation."""

    def __init__(self, session_id: str, classes: Iterable[str] | None = None,
                 max_tokens: int | None = None) -> None:
        s = get_settings()
        self.session_id = session_id
        requested = set(classes) if classes is not None else {
            c.strip() for c in s.observability_mask_classes.split(",") if c.strip()
        }
        self.classes = tuple(c for c in _CLASS_ORDER if c in requested)
        self.max_tokens = max_tokens or s.observability_mask_max_tokens
        self._fwd: dict[str, str] = {}          # original -> token
        self._rev: dict[str, str] = {}          # token -> original
        self._counts: dict[str, int] = {}       # class -> next index
        self._alt: re.Pattern | None = None     # compiled longest-first alternation
        self._lock = threading.RLock()
        self.created_at = time.time()
        self.budget_exceeded = False

    # ── Introspection (counts only — never values) ───────────────────────────

    @property
    def token_count(self) -> int:
        return len(self._fwd)

    def counts_by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for token in self._rev:
            parts = token.split("_")
            if len(parts) >= 3:
                out[parts[1]] = out.get(parts[1], 0) + 1
        return out

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self._rev)

    # ── Minting ──────────────────────────────────────────────────────────────

    def _mint(self, original: str, cls: str) -> str | None:
        with self._lock:
            if original in self._fwd:
                return self._fwd[original]
            if len(self._fwd) >= self.max_tokens:
                self.budget_exceeded = True
                return None
            infix = _PATTERNS[cls][0]
            idx = self._counts.get(infix, 0) + 1
            self._counts[infix] = idx
            token = f"AURA_{infix}_{idx:02d}"
            self._fwd[original] = token
            self._rev[token] = original
            self._alt = None    # invalidate; rebuilt lazily
            return token

    def _alternation(self) -> re.Pattern | None:
        """Longest-first alternation.

        Ordering matters for correctness, not speed: 'prod-cluster' is a prefix of
        'prod-cluster-us-east-1', and naive ordering corrupts the longer one.
        """
        with self._lock:
            if self._alt is None and self._fwd:
                keys = sorted(self._fwd.keys(), key=len, reverse=True)
                self._alt = re.compile("|".join(re.escape(k) for k in keys))
            return self._alt

    # ── Mask / unmask ────────────────────────────────────────────────────────

    def mask(self, text: str) -> str:
        if not text:
            return text
        try:
            out = text
            for cls in self.classes:
                pat = _PATTERNS.get(cls)
                if not pat:
                    continue
                _, regex, gidx = pat

                def _sub(m: re.Match, _cls=cls, _g=gidx) -> str:
                    value = m.group(_g)
                    if value.startswith("AURA_"):
                        return m.group(0)          # already masked — never nest
                    token = self._mint(value, _cls)
                    if token is None:
                        return m.group(0)
                    return m.group(0).replace(value, token, 1)

                out = regex.sub(_sub, out)
            # Re-apply already-known mappings so identifiers first seen in another
            # field are masked consistently everywhere.
            alt = self._alternation()
            if alt is not None:
                out = alt.sub(lambda m: self._fwd.get(m.group(0), m.group(0)), out)
            return out
        except Exception as exc:  # noqa: BLE001
            raise MaskingError(f"masking failed: {exc}") from exc

    def mask_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.mask(obj)
        if isinstance(obj, dict):
            return {k: self.mask_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.mask_obj(v) for v in obj)
        return obj

    def unmask(self, text: str) -> tuple[str, bool]:
        """Restore originals. Returns (text, complete).

        Fail-OPEN, deliberately asymmetric with mask(): a visible AURA_POD_07 in the
        UI is a bug report; a leaked pod name is an incident.
        """
        if not text:
            return text, True
        try:
            orphans: list[str] = []

            def _sub(m: re.Match) -> str:
                tok = m.group(0)
                if tok in self._rev:
                    return self._rev[tok]
                orphans.append(tok)
                return tok

            out = text
            for _ in range(5):            # bounded: tokens can nest at most shallowly
                nxt = TOKEN_RE.sub(_sub, out)
                if nxt == out:
                    break
                out = nxt
            if orphans:
                # Log token NAMES only — never values.
                log.warning("Unmask incomplete for session %s: orphan tokens %s",
                            self.session_id, sorted(set(orphans)))
            return out, not orphans
        except Exception as exc:  # noqa: BLE001
            log.warning("unmask failed for session %s: %s", self.session_id, exc)
            return text, False

    def unmask_obj(self, obj: Any) -> tuple[Any, bool]:
        complete = True
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, dict):
            out_d = {}
            for k, v in obj.items():
                out_d[k], ok = self.unmask_obj(v)
                complete = complete and ok
            return out_d, complete
        if isinstance(obj, (list, tuple)):
            items = []
            for v in obj:
                item, ok = self.unmask_obj(v)
                items.append(item)
                complete = complete and ok
            return type(obj)(items), complete
        return obj, True

    # ── Persistence ──────────────────────────────────────────────────────────
    # The map is EXACTLY as sensitive as the raw data. It is written to the
    # investigation's DynamoDB record and must never go to S3 beside the redacted
    # artifact.

    def export(self) -> dict:
        return {"sessionId": self.session_id, "classes": list(self.classes),
                "mapping": dict(self._rev), "counts": dict(self._counts)}

    @classmethod
    def restore(cls, data: dict) -> "MaskSession":
        ms = cls(data.get("sessionId", ""), classes=data.get("classes"))
        ms._rev = dict(data.get("mapping") or {})
        ms._fwd = {v: k for k, v in ms._rev.items()}
        ms._counts = dict(data.get("counts") or {})
        return ms

    def state(self) -> dict:
        """The UI payload. Counts only — never values."""
        return {
            "enabled": True,
            "reversible": True,
            "policy": ",".join(self.classes),
            "totalTokens": self.token_count,
            "byType": self.counts_by_type(),
            "budgetExceeded": self.budget_exceeded,
        }


# ── Session store ────────────────────────────────────────────────────────────
# Process-local TTL cache PLUS write-through to DynamoDB. The persistence is a
# CORRECTNESS requirement, not an optimization: under `uvicorn --workers > 1` an
# in-memory-only map means a follow-up request routed to worker B holds tokens it
# cannot resolve.

_SESSIONS: dict[str, MaskSession] = {}
_LOCK = threading.RLock()


def _evict_expired() -> None:
    ttl = get_settings().observability_mask_ttl_s
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _SESSIONS.items() if now - v.created_at > ttl]:
            _SESSIONS.pop(k, None)


def get_session(investigation_id: str, classes: Iterable[str] | None = None) -> MaskSession:
    _evict_expired()
    with _LOCK:
        ms = _SESSIONS.get(investigation_id)
        if ms is not None:
            return ms
    restored = _load(investigation_id)
    ms = restored or MaskSession(investigation_id, classes=classes)
    with _LOCK:
        _SESSIONS[investigation_id] = ms
    return ms


def persist_session(ms: MaskSession) -> None:
    from src.database.dynamo_client import query_items, update_item
    try:
        rows = query_items("observability-investigations", "investigationId",
                           ms.session_id, limit=1)
        if rows:
            update_item("observability-investigations",
                        {"investigationId": ms.session_id, "createdAt": rows[0]["createdAt"]},
                        {"maskMapping": ms.export()})
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist mask session %s: %s", ms.session_id, exc)


def _load(investigation_id: str) -> MaskSession | None:
    from src.database.dynamo_client import query_items
    try:
        rows = query_items("observability-investigations", "investigationId",
                           investigation_id, limit=1)
        if rows and rows[0].get("maskMapping"):
            return MaskSession.restore(rows[0]["maskMapping"])
    except Exception:  # noqa: BLE001
        pass
    return None


def masking_enabled() -> bool:
    s = get_settings()
    if not s.observability_masking_enabled and s.app_env != "development":
        log.warning(
            "SECURITY: observability masking is DISABLED in a non-development "
            "environment (app_env=%s). Identifiers WILL reach external LLMs.",
            s.app_env,
        )
    return s.observability_masking_enabled


def scan_for_plaintext(text: str, forbidden: list[str]) -> list[str]:
    """Eval/test helper: which forbidden strings survived into a prompt."""
    return [f for f in forbidden if f and f in (text or "")]
