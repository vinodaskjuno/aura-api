"""A migration session — the state a guided conversion moves through.

Why state at all, rather than a request that returns a result: the strategy is
amended at least once by design, the user answers questions between steps, and
conversion is many LLM calls. All of that needs somewhere to live between clicks,
and a demo that stalls needs to be resumable rather than restarted.

Stages advance in one direction, with one exception. Switching the target platform
archives the strategy and drops back to `target`, because a component mapping chosen
for Airflow is not valid for Step Functions and silently carrying it over would
generate code against a target nobody agreed to.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

TABLE = "migration-sessions"

# The stage order. `index` on this list is what enforces "forwards only".
STAGES: tuple[str, ...] = (
    "target",        # source detected, target chosen
    "architecture",  # component mapping inferred and confirmed
    "proposed",      # strategy generated, questions open
    "revised",       # answers folded in
    "finalized",     # strategy locked
    "converting",    # codegen running
    "converted",     # artifact ready
    "failed",
)


class StageError(Exception):
    """An action was attempted from a stage that does not permit it.

    Distinct from a validation error because the caller should report it as a
    sequencing problem — "finalize the strategy first" — rather than as bad input.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


def require_stage(session: dict, *allowed: str) -> None:
    """Refuse an action the session is not ready for."""
    stage = session.get("stage", "")
    if stage not in allowed:
        raise StageError(
            f"This migration is at '{stage}'. "
            f"That step needs it to be at {' or '.join(repr(a) for a in allowed)}.")


def create(project_id: str, project_name: str, source: str, target: str,
           actor: str) -> dict:
    """Open a session. Nothing is analysed yet."""
    from src.database.dynamo_client import put_item
    from src.migration.profiles import profile_for

    profile = profile_for(source, target)
    session: dict[str, Any] = {
        "sessionId": str(uuid.uuid4()),
        "projectId": project_id,
        "projectName": project_name,
        "stage": "target",
        "source": source,
        "target": target,
        "profileId": profile.id,
        "curated": profile.curated,
        "mapping": [],
        # How the generated output is shaped. Defaults chosen to be the least
        # surprising reading of a legacy app, and changeable from chat.
        "conversionShape": {
            "granularity": "one-per-process",   # or "consolidated"
            "extractShared": True,
            "repoLayout": "standard",
        },
        "strategy": {},
        "archivedStrategies": [],
        "questions": [],
        "answers": [],
        "chatLog": [],
        "components": [],
        "artifactKey": "",
        "convertedProjectId": "",
        "errors": [],
        "actor": actor,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    try:
        put_item(TABLE, session)
    except Exception as exc:  # noqa: BLE001
        log.error("could not persist migration session: %s", exc)
        raise
    return session


def get(session_id: str, project_id: str) -> dict | None:
    from src.database.dynamo_client import get_item
    try:
        return get_item(TABLE, {"sessionId": session_id, "projectId": project_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read migration session %s: %s", session_id, exc)
        return None


def save(session: dict, **changes: Any) -> dict:
    """Apply changes and persist. Refuses to move backwards.

    The guard is here rather than at each call site because there are a dozen of
    them and one forgetting would let a late poll response overwrite a later stage —
    the kind of bug that only shows up under the timing of a live demo.
    """
    from src.database.dynamo_client import update_item

    new_stage = changes.get("stage")
    if new_stage and new_stage not in ("failed",):
        if _index(new_stage) < _index(session.get("stage", "")):
            raise StageError(
                f"Refusing to move a migration back from "
                f"'{session.get('stage')}' to '{new_stage}'.")

    changes["updatedAt"] = _now()
    merged = {**session, **changes}
    try:
        update_item(TABLE,
                    {"sessionId": session["sessionId"], "projectId": session["projectId"]},
                    changes)
    except Exception as exc:  # noqa: BLE001
        log.error("could not update migration session %s: %s", session["sessionId"], exc)
        raise
    return merged


def reset_for_new_target(session: dict, target: str) -> dict:
    """Switch target platform: archive the strategy, return to `target`.

    The one backwards move, and the only chat action that discards work. The old
    strategy is kept rather than deleted so the user can see what they gave up —
    and so a demo that switches target by accident is recoverable.
    """
    from src.migration.profiles import profile_for
    from src.database.dynamo_client import update_item

    archived = list(session.get("archivedStrategies") or [])
    if session.get("strategy"):
        archived.append({
            "target": session.get("target"),
            "archivedAt": _now(),
            "strategy": session["strategy"],
            "mapping": session.get("mapping") or [],
        })

    profile = profile_for(session.get("source", ""), target)
    changes = {
        "stage": "target",
        "target": target,
        "profileId": profile.id,
        "curated": profile.curated,
        "strategy": {},
        "questions": [],
        "answers": [],
        "components": [],
        "artifactKey": "",
        "archivedStrategies": archived,
        "updatedAt": _now(),
    }
    update_item(TABLE,
                {"sessionId": session["sessionId"], "projectId": session["projectId"]},
                changes)
    return {**session, **changes}


def list_for_project(project_id: str, limit: int = 20) -> list[dict]:
    """Sessions for one project, newest first."""
    from src.database.dynamo_client import query_items
    try:
        rows = query_items(TABLE, pk_name="projectId", pk_value=project_id,
                           index_name="projectId-createdAt-index", limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list migration sessions for %s: %s", project_id, exc)
        return []
    return sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)


def set_mapping(session: dict, mapping: list[dict], origin: str = "user") -> dict:
    """Record the confirmed component mapping.

    `origin` is stamped on any row the caller changed, so the strategy can later say
    whether Vault was inferred from the estate, asked for in chat, or typed by hand.
    Rows that still match what was inferred keep their `inferred` origin.
    """
    previous = {r.get("capability"): r for r in (session.get("mapping") or [])}
    stamped: list[dict] = []
    any_changed = False
    for row in mapping:
        cap = row.get("capability")
        before = previous.get(cap) or {}
        changed = row.get("technology") != before.get("technology")
        any_changed = any_changed or changed
        stamped.append({
            **row,
            "origin": origin if changed else (before.get("origin") or row.get("origin") or "unset"),
        })

    # Only ADVANCE to architecture. Confirming the mapping is legitimate at
    # `proposed` and `revised` too — changing where secrets or logging land is the
    # point of the architecture step, and an organisation swapping in Vault after
    # reading the strategy is the expected flow, not a mistake. Hardcoding
    # `stage="architecture"` made that a backwards move, which `save` refuses, so
    # the Save button on the mapping table raised StageError once a strategy
    # existed. The endpoint's own guard already permits those stages; this was the
    # write contradicting the guard.
    current = session.get("stage", "")
    stage = "architecture" if _index(current) < _index("architecture") else current

    changes: dict[str, Any] = {"mapping": stamped, "stage": stage}
    # The strategy was computed against the old mapping, so it no longer describes
    # what would be generated. Say so rather than leaving a stale document looking
    # current — the alternative is converting against a mapping the strategy never
    # saw.
    if any_changed and session.get("strategy"):
        changes["strategyStale"] = True

    return save(session, **changes)
