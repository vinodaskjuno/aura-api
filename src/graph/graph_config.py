"""Which engine is read from, and which are written to — switchable at runtime.

This deliberately does not live in Settings. `get_settings()` is lru_cached with a
module-level singleton, so anything read from there is frozen at import and could
only be changed by restarting the backend. The whole point of this control is that
an operator can switch the read source from the UI, live, in front of a client.

Stored in DynamoDB rather than in process memory so the setting is shared: the
backend runs a single task today (infra/ecs.tf:263), but an in-process toggle would
silently diverge per task the moment it scales.

Reads are cached for a few seconds. That bounds how stale a task's view can be
without making every graph call a DynamoDB round trip.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_TABLE = "graph-config"
_CONFIG_ID = "global"
_TTL_SECONDS = 10.0

_cache: tuple[float, "GraphConfig"] | None = None
_lock = threading.Lock()


@dataclass(frozen=True)
class GraphConfig:
    read_source: str
    write_targets: tuple[str, ...]
    updated_at: str = ""
    updated_by: str = ""

    def as_dict(self) -> dict:
        return {
            "readSource": self.read_source,
            "writeTargets": list(self.write_targets),
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }


def _default() -> GraphConfig:
    """Whatever is configured, with the conventional default preferred.

    Derived from the configured backends rather than hard-coded, so a
    Memgraph-only deployment works with no configuration row at all — which is
    what a client install looks like.
    """
    from src.graph import backends
    names = backends.configured_names()
    if not names:
        return GraphConfig(read_source="", write_targets=())
    primary = backends.DEFAULT_BACKEND if backends.DEFAULT_BACKEND in names else names[0]
    return GraphConfig(read_source=primary, write_targets=tuple(names))


def get_config(refresh: bool = False) -> GraphConfig:
    global _cache
    now = time.monotonic()
    if not refresh and _cache and (now - _cache[0]) < _TTL_SECONDS:
        return _cache[1]

    config = _default()
    try:
        from src.database import dynamo_client as db
        row = db.get_item(_TABLE, {"configId": _CONFIG_ID})
        if row:
            from src.graph import backends
            known = set(backends.configured_names())
            # An engine named in the row but absent from this deployment is
            # ignored, not honoured: a stale row must never route traffic at a
            # backend that does not exist here.
            source = row.get("readSource") or config.read_source
            targets = [t for t in (row.get("writeTargets") or []) if t in known]
            if source not in known:
                log.warning("configured read source %r is not available here; using %r",
                            source, config.read_source)
                source = config.read_source
            config = GraphConfig(
                read_source=source,
                write_targets=tuple(targets or config.write_targets),
                updated_at=row.get("updatedAt", ""),
                updated_by=row.get("updatedBy", ""),
            )
    except Exception as exc:  # noqa: BLE001 — a missing table must not break the graph
        log.debug("graph config read failed, using defaults: %s", exc)

    with _lock:
        _cache = (now, config)
    return config


def set_config(read_source: str, write_targets: list[str], actor: str) -> GraphConfig:
    """Persist a new routing choice. Validation belongs to the caller (the router),
    which can return a 400; this layer only writes."""
    global _cache
    row = {
        "configId": _CONFIG_ID,
        "readSource": read_source,
        "writeTargets": list(write_targets),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedBy": actor,
    }
    from src.database import dynamo_client as db
    db.put_item(_TABLE, row)
    with _lock:
        _cache = None          # next read reflects the change immediately here
    return get_config(refresh=True)


def invalidate():
    global _cache
    with _lock:
        _cache = None
