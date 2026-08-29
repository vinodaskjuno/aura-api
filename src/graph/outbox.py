"""Writes that failed on a secondary engine, queued for retry.

The alternative designs both lose. Failing the whole request when a shadow engine
is down means a store nobody reads from can take production writes offline.
Logging and moving on lets the two stores diverge silently, and you find out at the
worst possible moment — when you switch the read source in front of a client and
the data is not there.

So: the primary write commits, the failed secondary write is queued, and the
backlog is visible. Switching the read source is refused while it is non-empty,
because reading from a store that is known to be behind is the one thing this is
supposed to prevent.

This is not a distributed transaction and must not be described as one. The primary
has already committed by the time the secondary is attempted.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_TABLE = "graph-outbox"
MAX_DRAIN = 500


def enqueue(backend: str, cypher: str, params: dict, error: str) -> None:
    from src.database import dynamo_client as db
    try:
        db.put_item(_TABLE, {
            "backend": backend,
            # Sorts chronologically, so a drain replays in the order the writes
            # were made. Replaying out of order could resurrect a deleted node.
            "outboxId": f"{datetime.now(timezone.utc).isoformat()}#{uuid.uuid4().hex[:8]}",
            "cypher": cypher,
            "params": _jsonable(params),
            "error": str(error)[:500],
            "attempts": 0,
        })
    except Exception as exc:  # noqa: BLE001 — nothing left to fall back to
        log.error("Could not queue failed %s write; it is LOST: %s", backend, exc)


def _jsonable(params: dict) -> dict:
    """Params must survive a DynamoDB round trip to be replayable."""
    import json
    out = {}
    for key, value in (params or {}).items():
        try:
            json.dumps(value)
            out[key] = value
        except (TypeError, ValueError):
            out[key] = str(value)
    return out


def depth(backend: str | None = None) -> dict[str, int]:
    """Pending writes per backend. This is the divergence signal the UI shows."""
    from src.database import dynamo_client as db
    from src.graph import backends as reg
    counts: dict[str, int] = {}
    for name in ([backend] if backend else reg.configured_names()):
        try:
            rows = db.query_items(_TABLE, "backend", name, limit=MAX_DRAIN)
            counts[name] = len(rows)
        except Exception:  # noqa: BLE001
            counts[name] = 0
    return counts


def drain(backend: str, limit: int = MAX_DRAIN) -> dict:
    """Replay queued writes in order. Stops at the first failure.

    Stopping rather than skipping is deliberate: the queue is ordered, and skipping
    a failed write to apply a later one can leave the store in a state neither
    engine ever had.
    """
    from src.database import dynamo_client as db
    from src.graph import backends as reg

    target = reg.get_backend(backend)
    if target is None or not target.is_available():
        return {"backend": backend, "replayed": 0, "remaining": depth(backend).get(backend, 0),
                "error": f"backend {backend!r} is not available"}

    rows = sorted(db.query_items(_TABLE, "backend", backend, limit=limit),
                  key=lambda r: r.get("outboxId", ""))
    replayed, failed = 0, None
    for row in rows:
        try:
            with target.session() as s:
                s.run(row["cypher"], row.get("params") or {})
            db.delete_item(_TABLE, {"backend": backend, "outboxId": row["outboxId"]})
            replayed += 1
        except Exception as exc:  # noqa: BLE001
            failed = str(exc)[:200]
            try:
                db.update_item(_TABLE, {"backend": backend, "outboxId": row["outboxId"]},
                               {"attempts": int(row.get("attempts", 0)) + 1,
                                "error": failed})
            except Exception:  # noqa: BLE001
                pass
            break

    remaining = depth(backend).get(backend, 0)
    result = {"backend": backend, "replayed": replayed, "remaining": remaining}
    if failed:
        result["error"] = failed
    log.info("outbox drain %s: replayed=%d remaining=%d", backend, replayed, remaining)
    return result
