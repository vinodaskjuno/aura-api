"""Whether a project's telemetry is actually arriving — and if not, why not.

`/otlp/*` ALWAYS returns 200, deliberately: a non-2xx makes an exporter retry in a
loop and surfaces telemetry errors inside the caller's own application. The cost of
that contract is that the sender cannot tell the difference between

    "my app has not made an LLM call yet"          and
    "my key was refused and nothing will ever arrive"

and neither can the person watching an empty Traces tab. That ambiguity is the single
most expensive thing about pointing someone's app at a collector, because the failure
is indistinguishable from the feature simply not being used yet.

`qatest/queue.py` solved exactly this once, for the runner: `runners:_unauthorized`
counts rejected polls so "no runner connected" and "runner connected, key refused" stop
looking identical. This is that pattern, for ingest.

Written on the hot path, so it is one cheap update per accepted batch and per rejection,
never a read. Failures here are swallowed: a status counter must not be able to break
the ingest it describes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

#: Shares the graph-config table, like `online_eval`'s own config does — this is a
#: handful of small rows, not a new storage concern.
_TABLE = "graph-config"
_PK = "configId"
_ROW = "aiobs-ingest-status"

#: How long without a span before "connected" becomes "quiet". Long enough that an
#: idle app is not reported as broken, short enough to be useful while someone watches.
QUIET_AFTER_S = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict:
    try:
        from src.database.dynamo_client import get_item
        return get_item(_TABLE, {_PK: _ROW}) or {}
    except Exception as exc:                                  # noqa: BLE001
        log.debug("ingest status read failed: %s", exc)
        return {}


def _write(patch: dict) -> None:
    try:
        from src.database.dynamo_client import update_item
        update_item(_TABLE, {_PK: _ROW}, patch)
    except Exception as exc:                                  # noqa: BLE001
        log.debug("ingest status write failed: %s", exc)


def _safe(key: str) -> str:
    """A DynamoDB attribute name from a caller-supplied project id."""
    return "p_" + "".join(c if c.isalnum() or c in "-_" else "-" for c in str(key))[:100]


def record_spans(project_id: str, count: int, tenant_id: str = "") -> None:
    """A batch landed. One flat attribute per project, so two projects writing at the
    same moment touch disjoint attributes and cannot clobber each other."""
    if not project_id or count <= 0:
        return
    _write({_safe(project_id): {"projectId": str(project_id)[:120],
                               "lastSpanAt": _now(),
                               "lastSpanCount": int(count),
                               "tenantId": str(tenant_id or "")[:120]}})


def record_rejected(key_hint: str = "", reason: str = "") -> None:
    """A batch was refused. Keyed by the last four characters of the presented key —
    the same hint the key record stores — because the credential itself must not be
    written anywhere, and "some key ending 9f2c was refused 41 times" is what actually
    lets someone find which one."""
    hint = str(key_hint or "unknown")[-4:]
    row = _read().get("rejected") or {}
    entry = row.get(hint) or {}
    _write({"rejected": {**row, hint: {
        "count": int(entry.get("count") or 0) + 1,
        "lastAt": _now(),
        "reason": str(reason or "")[:200],
    }}})


def status_for_many(project_ids: list[str]) -> dict:
    """{projectId: state} for a whole estate, from ONE read.

    `status_for` is a read per project. That is right for a single panel and wrong for
    a landing page that asks about every project a user has — which is how a 29-second
    test suite became a 52-second one.
    """
    row = _read()
    rejected = bool(row.get("rejected"))
    try:
        from src.config_settings import get_settings
        if not bool(getattr(get_settings(), "otlp_enabled", True)):
            return {pid: "disabled" for pid in project_ids}
    except Exception:                                         # noqa: BLE001
        pass

    out: dict = {}
    for pid in project_ids:
        mine = row.get(_safe(pid)) or {}
        if mine.get("lastSpanAt"):
            out[pid] = "connected"
        elif rejected:
            out[pid] = "key-refused"
        else:
            out[pid] = "no-spans-yet"
    return out


def status_for(project_id: str) -> dict:
    """What to tell someone waiting for their first span.

    Four states, and the distinction between the middle two is the whole point:
      disabled     — the receiver is switched off on this server
      key-refused  — something presented a credential we rejected
      no-spans-yet — nothing has arrived, and nothing has been refused either
      connected    — spans have arrived (`quiet` says whether recently)
    """
    from src.config_settings import get_settings

    if not bool(getattr(get_settings(), "otlp_enabled", True)):
        return {"state": "disabled",
                "detail": "OTLP ingest is switched off on this server."}

    row = _read()
    mine = row.get(_safe(project_id)) or {}
    rejected = row.get("rejected") or {}

    last = str(mine.get("lastSpanAt") or "")
    if last:
        age = _age_seconds(last)
        return {
            "state": "connected",
            "lastSpanAt": last,
            "lastSpanCount": int(mine.get("lastSpanCount") or 0),
            "quiet": age > QUIET_AFTER_S,
            "detail": (f"No spans for {int(age // 60)} minutes."
                       if age > QUIET_AFTER_S else "Spans are arriving."),
        }

    if rejected:
        worst = max(rejected.items(), key=lambda kv: int((kv[1] or {}).get("count") or 0))
        hint, entry = worst
        return {
            "state": "key-refused",
            "rejectedCount": int((entry or {}).get("count") or 0),
            "lastAt": (entry or {}).get("lastAt", ""),
            "detail": (f"A credential ending …{hint} was refused "
                       f"{int((entry or {}).get('count') or 0)} time(s). Its spans were "
                       f"dropped — the exporter was told 200 so it would not retry in a "
                       f"loop inside your application."),
        }

    return {"state": "no-spans-yet",
            "detail": ("Nothing has arrived yet, and nothing has been refused. If your "
                       "app has not made a model call since it started, that is the "
                       "expected state.")}


def _age_seconds(iso: str) -> float:
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()
    except Exception:                                         # noqa: BLE001
        return 0.0
