"""Guided migration — any source stack to any target stack.

Routes:
  GET  /api/migration/profiles                     Curated pairs + reachable targets
  GET  /api/migration/projects/{pid}/sessions      This project's migrations
  POST /api/migration/sessions                     Open one
  GET  /api/migration/sessions/{sid}               Poll (conversion is long-running)
  GET  /api/migration/sessions/{sid}/inferred      Component standards, from the estate
  PUT  /api/migration/sessions/{sid}/mapping       Confirm or override them
  POST /api/migration/sessions/{sid}/strategy      Propose / re-propose
  POST /api/migration/sessions/{sid}/answers       Answer questions, comment, revise
  POST /api/migration/sessions/{sid}/finalize      Lock the strategy
  POST /api/migration/sessions/{sid}/convert       Generate the target code
  GET  /api/migration/sessions/{sid}/download      Presigned link to the .zip
  POST /api/migration/sessions/{sid}/handoff       Register it for QualityMind
  WS   /api/migration/ws/{sid}                     Adjust it by talking

Everything is gated on `dev_workspace` — the same permission that gates Reverse
Engineering, where this flow starts.

`projectId` is a query parameter on the session routes rather than being looked up:
the sessions table is keyed (sessionId, projectId), and DynamoDB rejects a get with
only the partition key. The same trap is documented in routers/git_ops.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from src.migration import capabilities, session as sessions
from src.migration.profiles import known_pairs, known_targets, profile_for
from src.routers.auth import require_permission

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/migration", tags=["migration"])

_GUARD = require_permission("dev_workspace")


# ── Models ───────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    projectId: str
    source: str
    target: str


class MappingRow(BaseModel):
    capability: str
    technology: str = ""
    capabilityLabel: str = ""
    origin: str = ""
    services: int = 0
    totalServices: int = 0
    confidence: str = ""
    evidence: list[str] = []


class MappingRequest(BaseModel):
    mapping: list[MappingRow]
    conversionShape: dict | None = None


class AnswersRequest(BaseModel):
    answers: list[dict] = []      # [{questionId, question, answer}]
    comments: list[str] = []


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load(session_id: str, project_id: str) -> dict:
    found = sessions.get(session_id, project_id)
    if not found:
        raise HTTPException(404, f"No migration session {session_id!r} for that project.")
    return found


def _stage_guard(session: dict, *allowed: str) -> None:
    """Turn a sequencing problem into a 409, not a 400.

    The request was well formed; the migration simply is not at a point where it
    makes sense. A 400 would send the caller looking for a bad field.
    """
    try:
        sessions.require_stage(session, *allowed)
    except sessions.StageError as exc:
        raise HTTPException(409, str(exc))


def _project(project_id: str) -> dict:
    from src.database.dynamo_client import scan_items
    for row in scan_items("projects", limit=500):
        if row.get("projectId") == project_id:
            return row
    raise HTTPException(404, f"Project {project_id!r} not found.")


# ── Profiles ─────────────────────────────────────────────────────────────────

@router.get("/profiles")
def get_profiles(_: dict = Depends(_GUARD)):
    """What the UI puts in its dropdowns.

    `targets` is deliberately wider than `pairs`: the generic path reaches platforms
    nobody has curated, and a dropdown listing only curated ones would make the
    product look narrower than it is.
    """
    return {"pairs": known_pairs(), "targets": known_targets()}


# ── Sessions ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/sessions")
def list_sessions(project_id: str, _: dict = Depends(_GUARD)):
    return sessions.list_for_project(project_id)


@router.post("/sessions", status_code=201)
def start_session(body: StartRequest, user: dict = Depends(_GUARD)):
    project = _project(body.projectId)
    if not body.target.strip():
        raise HTTPException(400, "Choose a target platform.")

    created = sessions.create(
        project_id=body.projectId,
        project_name=project.get("name", body.projectId),
        source=body.source.strip().lower(),
        target=body.target.strip().lower(),
        actor=user["username"],
    )
    log.info("migration session %s opened: %s -> %s",
             created["sessionId"], created["source"], created["target"])
    return created


@router.get("/sessions/{session_id}")
def get_session(session_id: str, projectId: str = Query(...),
                _: dict = Depends(_GUARD)):
    return _load(session_id, projectId)


# ── Architecture: infer, then confirm ────────────────────────────────────────

@router.get("/sessions/{session_id}/inferred")
def get_inferred(session_id: str, projectId: str = Query(...),
                 _: dict = Depends(_GUARD)):
    """Component standards read out of the estate, each with its evidence.

    Every capability comes back, including ones nothing was found for — a missing
    row is indistinguishable from one nobody considered, and the user needs to see
    the gap to fill it.
    """
    session = _load(session_id, projectId)
    candidates = capabilities.infer(projectId, extra_evidence=_evidence_text(projectId))
    return {
        "candidates": candidates,
        "mapping": session.get("mapping") or capabilities.default_mapping(candidates),
        "capabilities": [{"id": c, "label": l} for c, l in capabilities.CAPABILITIES],
    }


def _evidence_text(project_id: str) -> str:
    """Extra text for the ambiguous signatures to match `hints` against.

    Without this, a signature needing corroboration (boto3 → Secrets Manager) can
    never fire, which is the intended conservative default rather than a bug.
    """
    from src.graph import neo4j_client as neo4j
    try:
        rows = neo4j.run_query(
            "MATCH (n) WHERE n.projectId = $pid AND n.source IS NOT NULL "
            "RETURN coalesce(n.name,'') + ' ' + coalesce(n.url,'') + ' ' "
            "     + coalesce(n.description,'') AS text LIMIT 400",
            {"pid": project_id})
    except Exception as exc:  # noqa: BLE001
        log.debug("evidence text unavailable for %s: %s", project_id, exc)
        return ""
    return " ".join(str(r.get("text") or "") for r in rows or []).lower()


@router.put("/sessions/{session_id}/mapping")
def put_mapping(session_id: str, body: MappingRequest, projectId: str = Query(...),
                _: dict = Depends(_GUARD)):
    session = _load(session_id, projectId)
    _stage_guard(session, "target", "architecture", "proposed", "revised")

    # _stage_guard above already decided this stage is allowed, so a StageError from
    # the write means the two disagree — a bug, but one the caller should still see as
    # a 409 it can act on rather than an opaque "Internal server error".
    try:
        updated = sessions.set_mapping(
            session, [row.model_dump() for row in body.mapping], origin="user")
        if body.conversionShape:
            updated = sessions.save(updated, conversionShape=body.conversionShape)
    except sessions.StageError as exc:
        raise HTTPException(409, str(exc))
    return updated


# ── Strategy ─────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/strategy")
async def run_strategy(session_id: str, projectId: str = Query(...),
                       user: dict = Depends(_GUARD)):
    """Propose a strategy. Callable again to re-propose after answers or comments."""
    session = _load(session_id, projectId)
    _stage_guard(session, "target", "architecture", "proposed", "revised")
    return await _propose(session, user, revised=session.get("stage") in ("proposed", "revised"))


@router.post("/sessions/{session_id}/answers")
async def submit_answers(session_id: str, body: AnswersRequest,
                         projectId: str = Query(...), user: dict = Depends(_GUARD)):
    """Fold answers and comments in, then re-propose.

    Answers accumulate rather than replace: a second round of questions must not
    discard what was said in the first.
    """
    session = _load(session_id, projectId)
    _stage_guard(session, "proposed", "revised")

    merged = list(session.get("answers") or [])
    by_id = {a.get("questionId"): i for i, a in enumerate(merged)}
    for answer in body.answers:
        qid = answer.get("questionId")
        if qid in by_id:
            merged[by_id[qid]] = answer
        else:
            merged.append(answer)

    session = sessions.save(
        session,
        answers=merged,
        comments=list(session.get("comments") or []) + list(body.comments),
    )
    return await _propose(session, user, revised=True)


def _source_facts(session: dict) -> dict:
    """What the application actually contains, read from the working copy.

    Without this the strategy agent sees only the knowledge graph — and for a
    platform with no parser the graph holds no components at all, so it correctly
    refuses to list any and asks for file paths instead. That is the agent being
    honest about a missing input, not a prompt problem.
    """
    from src.migration import source
    from src.migration.profiles import profile_for

    profile = profile_for(session.get("source", ""), session.get("target", ""))
    try:
        return source.inventory(session["projectId"], profile.detect)
    except Exception as exc:  # noqa: BLE001 — the graph path still works without it
        log.warning("source inventory failed for %s: %s", session["projectId"], exc)
        return {}


async def _propose(session: dict, user: dict, revised: bool) -> dict:
    from src.agents.base_agent import AgentContext
    from src.graph import neo4j_client as neo4j
    from src.orchestrator.agent_registry import get_agent

    agent = get_agent("migration_strategy_agent")
    if agent is None:
        raise HTTPException(503, "Migration strategy agent is not available.")

    # 2 hops: enough to reach a process's tasks and their dependencies without
    # pulling the whole estate into a prompt.
    try:
        graph = neo4j.get_project_subgraph(session.get("projectName", ""), hops=2)
    except Exception as exc:  # noqa: BLE001 — a strategy without the graph beats none
        log.warning("subgraph unavailable for %s: %s", session.get("projectName"), exc)
        graph = {"nodes": [], "links": []}

    context = AgentContext(
        user_id=user.get("userId", ""),
        username=user["username"],
        role=user.get("role", ""),
        intent=f"Migrate {session['source']} to {session['target']}",
        project_id=session["projectId"],
        session_id=session["sessionId"],
        kg_snapshot=graph,
        extra={
            "source": session["source"],
            "target": session["target"],
            "mapping": session.get("mapping") or [],
            "conversionShape": session.get("conversionShape") or {},
            "answers": session.get("answers") or [],
            "comments": session.get("comments") or [],
            # Read the tree every time rather than caching on the session: the
            # user may have re-uploaded between attempts, and a stale inventory
            # would plan a migration of code that is no longer there.
            "facts": _source_facts(session),
        },
    )

    from src.graph import provenance
    with provenance.trace_run(
        provenance.PIPELINE_MIGRATION,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"],
        actorId=user.get("userId", ""),
        source="migration",
        sourceDetail=f"{session['source']} → {session['target']}",
        projectId=session["projectId"],
        sessionId=session["sessionId"],
        writtenBy="migration.strategy",
    ):
        result = await agent.run(context)

    strategy = result.output or {}
    if result.status == "failed":
        raise HTTPException(502, strategy.get("error") or "Could not produce a strategy.")

    return sessions.save(
        session,
        strategy=strategy,
        questions=strategy.get("questions") or [],
        components=strategy.get("components") or [],
        stage="revised" if revised else "proposed",
    )


@router.post("/sessions/{session_id}/finalize")
def finalize(session_id: str, projectId: str = Query(...), _: dict = Depends(_GUARD)):
    """Lock the strategy so conversion can start."""
    session = _load(session_id, projectId)
    _stage_guard(session, "proposed", "revised")
    if not (session.get("strategy") or {}).get("components"):
        raise HTTPException(400, "There is no strategy to finalize yet.")
    return sessions.save(session, stage="finalized")


# ── Convert ──────────────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/convert", status_code=202)
def start_conversion(session_id: str, projectId: str = Query(...),
                     user: dict = Depends(_GUARD)):
    """Generate the target code. Returns immediately; poll the session for progress.

    202, not 200: the work has been accepted, not completed. Conversion is many model
    calls and a synchronous response would hold the request open for minutes.
    """
    session = _load(session_id, projectId)
    # `converting` is allowed so an interrupted run can be resumed — `run` skips
    # components already recorded rather than paying for them twice.
    _stage_guard(session, "finalized", "converting", "failed")

    from src.graph import provenance
    from src.migration import convert

    # spawn_traced, not a bare Thread: a plain thread starts with an empty context
    # and everything it wrote would land unattributed. Documented in provenance.py.
    provenance.spawn_traced(convert.run, session_id, projectId, user["username"])
    return {"accepted": True, "sessionId": session_id,
            "components": len((session.get("strategy") or {}).get("components") or [])}


@router.get("/sessions/{session_id}/download")
def download(session_id: str, projectId: str = Query(...), _: dict = Depends(_GUARD)):
    """A short-lived link to the artifact.

    Presigned rather than streamed through the API: the zip can be large, and
    proxying it would tie up a worker for the whole transfer.
    """
    session = _load(session_id, projectId)
    key = session.get("artifactKey")
    if not key:
        raise HTTPException(409, "Nothing has been generated for this migration yet.")

    from src.storage import s3_client
    url = s3_client.presigned_url("exports", key, expires=900)
    if not url:
        raise HTTPException(503, "Could not produce a download link.")
    return {"url": url, "expiresInSeconds": 900,
            "files": session.get("conversionFileCount") or 0}


@router.post("/sessions/{session_id}/handoff")
def handoff(session_id: str, projectId: str = Query(...), user: dict = Depends(_GUARD)):
    """Register the converted output as a project, so QualityMind can test it.

    QA Mind lists any project whose status is `analyzed`, so this IS the handoff —
    no QA-side change was needed. The new project points at the same artifact rather
    than copying files: the zip in S3 is already the deliverable.
    """
    from datetime import datetime, timezone
    import uuid as _uuid

    from src.database.dynamo_client import put_item

    session = _load(session_id, projectId)
    _stage_guard(session, "converted")
    if session.get("convertedProjectId"):
        return {"projectId": session["convertedProjectId"], "created": False}

    now = datetime.now(timezone.utc).isoformat()
    new_id = str(_uuid.uuid4())
    source_name = session.get("projectName") or projectId
    put_item("projects", {
        "projectId": new_id,
        "userId": user["userId"],
        "username": user["username"],
        "name": f"{source_name} → {session['target']}",
        "description": (f"Migrated from {session['source']} to {session['target']} "
                        f"by Aura. Reviewable output — see MIGRATION.md for the "
                        f"decisions left open."),
        "environment": "migration",
        # `analyzed` is what puts it in QualityMind's list. Anything else and the
        # project exists but is invisible to the screen it was created for.
        "status": "analyzed",
        "createdAt": now,
        "updatedAt": now,
        "repoCount": 0,
        "mcpEndpoints": [],
        "migratedFrom": projectId,
        "migrationSessionId": session_id,
        "artifactKey": session.get("artifactKey", ""),
    })
    sessions.save(session, convertedProjectId=new_id)
    log.info("migration %s handed off as project %s", session_id, new_id)
    return {"projectId": new_id, "created": True}



# ── Chat: propose, then confirm ──────────────────────────────────────────────
#
# Same protocol as the ontology maintainer chat: stream tokens, emit a proposal
# carrying a changeId, wait for a confirmation naming that id. Nothing the model
# suggests takes effect until a person accepts it.
#
# Auth is by `?token=` because a browser cannot set headers on a WebSocket — the
# same reason every other WS in this codebase does it that way.

@router.websocket("/ws/{session_id}")
async def migration_chat(ws: WebSocket, session_id: str,
                         projectId: str = Query(...), token: str = Query("")):
    from src.migration import chat as migration_chat_mod
    from src.services.auth_service import verify_token

    user = verify_token(token or "")

    # Accept before closing — closing an un-accepted socket raises in Starlette.
    await ws.accept()
    if not user or "dev_workspace" not in (user.get("permissions") or []):
        await ws.close(code=4003)
        return

    session = sessions.get(session_id, projectId)
    if not session:
        await ws.send_json({"type": "error", "message": "No such migration session."})
        await ws.close(code=4004)
        return

    await ws.send_json({"type": "connected", "username": user["username"],
                        "stage": session.get("stage")})

    # changeId -> the described changes awaiting a decision. Per-connection, so a
    # stale id from a previous socket cannot be replayed.
    pending: dict[str, list[dict]] = {}

    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")

            if kind == "chat":
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                # Re-read: the mapping may have changed via REST since connect, and
                # proposing against stale state produces diffs that do not apply.
                session = sessions.get(session_id, projectId) or session
                async for event in migration_chat_mod.propose(session, text, user):
                    await ws.send_json(event)
                    if event.get("type") == "proposal":
                        pending[event["changeId"]] = event["changes"]

            elif kind == "confirm":
                change_id = msg.get("changeId")
                changes = pending.pop(change_id, None)
                if changes is None:
                    await ws.send_json({
                        "type": "error",
                        "message": "That proposal has already been answered."})
                    continue

                if not msg.get("approved"):
                    await ws.send_json({"type": "applied", "changeId": change_id,
                                        "applied": 0, "rejected": len(changes)})
                    continue

                accept = msg.get("accept")
                from src.graph import provenance
                with provenance.trace_run(
                    provenance.PIPELINE_MIGRATION,
                    trigger=provenance.TRIGGER_MANUAL,
                    actor=user["username"],
                    source="migration",
                    sourceDetail="adjusted in chat",
                    projectId=projectId,
                    sessionId=session_id,
                    writtenBy="migration.chat",
                    record=False,
                ):
                    session = migration_chat_mod.apply(session, changes, accept)

                await ws.send_json({
                    "type": "applied",
                    "changeId": change_id,
                    "applied": len(accept) if accept is not None else len(changes),
                    "session": session,
                })

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("migration chat WS error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001 — socket already gone
            pass
