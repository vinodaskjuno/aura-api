"""SOP (Standard Operating Procedure) router."""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from src.routers.auth import get_current_user, require_permission
from src.database.dynamo_client import put_item, scan_items, update_item
from src.models.sop import SOPDocument, SOPStep, SOPChecklistItem, STAGE_META

router = APIRouter(prefix="/api/sop", tags=["sop"])

# ── Permission helpers ─────────────────────────────────────────────────────────

def _can_approve(role: str, stage: str) -> bool:
    return role in STAGE_META.get(stage, {}).get("approveRoles", [])

def _get_sop(project_id: str, stage: str) -> dict | None:
    all_sops = scan_items("sops", limit=500)
    matches  = [s for s in all_sops if s.get("projectId") == project_id and s.get("stage") == stage]
    if not matches:
        return None
    matches.sort(key=lambda x: x.get("generatedAt", ""), reverse=True)
    return matches[0]

def _deserialize(raw: dict) -> dict:
    """Parse steps JSON string back to list if stored as string."""
    if raw and isinstance(raw.get("steps"), str):
        try:
            raw["steps"] = json.loads(raw["steps"])
        except Exception:
            raw["steps"] = []
    return raw

# ── Models ────────────────────────────────────────────────────────────────────

class StepUpdateRequest(BaseModel):
    stepId: str
    status: str | None = None
    notes: str | None = None

class BulkStepsRequest(BaseModel):
    steps: list[StepUpdateRequest]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/{project_id}/{stage}")
async def generate_sop(project_id: str, stage: str,
                        user: dict = Depends(get_current_user)):
    """Generate (or regenerate) a SOP for a project + stage using SOPAgent."""
    if stage not in STAGE_META:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage}. Must be dev|qa|aiops|reverse_engineering")

    from src.agents.sop_agent import SOPAgent
    from src.agents.base_agent import AgentContext
    from src.database.dynamo_client import get_item_by_pk

    # Load project KG for context injection
    kg = {}
    try:
        project = get_item_by_pk("projects", project_id)
        if project and project.get("knowledgeGraph"):
            kg = json.loads(project["knowledgeGraph"])
        project_name = (project or {}).get("name", project_id)
    except Exception:
        project_name = project_id

    context = AgentContext(
        user_id=user["userId"],
        username=user["username"],
        role=user["role"],
        intent=f"Generate {stage} SOP",
        project_id=project_id,
        session_id=str(uuid.uuid4()),
        extra={"stage": stage, "project_name": project_name, "kg": kg},
    )

    agent  = SOPAgent()
    result = await agent.run(context)

    if result.status == "failed":
        raise HTTPException(status_code=500, detail="SOP generation failed")

    return result.output


@router.get("/{project_id}/{stage}")
def get_sop(project_id: str, stage: str,
            user: dict = Depends(get_current_user)):
    """Fetch the latest SOP for a project + stage."""
    raw = _get_sop(project_id, stage)
    if not raw:
        return None
    return _deserialize(raw)


@router.put("/{project_id}/{stage}/steps")
def update_steps(project_id: str, stage: str, req: BulkStepsRequest,
                 user: dict = Depends(get_current_user)):
    """Bulk update step statuses and notes."""
    raw = _get_sop(project_id, stage)
    if not raw:
        raise HTTPException(status_code=404, detail="SOP not found")
    _deserialize(raw)

    update_map = {u.stepId: u for u in req.steps}
    for step in raw.get("steps", []):
        if step["id"] in update_map:
            upd = update_map[step["id"]]
            if upd.status is not None:
                step["status"] = upd.status
            if upd.notes is not None:
                step["notes"] = upd.notes

    update_item("sops", {"sopId": raw["sopId"], "projectId": project_id}, {
        "steps": json.dumps(raw["steps"]),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    })
    return raw


@router.post("/{project_id}/{stage}/step/{step_id}/check/{item_id}")
def toggle_checklist(project_id: str, stage: str, step_id: str, item_id: str,
                     user: dict = Depends(get_current_user)):
    """Toggle a single checklist item completed/incomplete."""
    raw = _get_sop(project_id, stage)
    if not raw:
        raise HTTPException(status_code=404, detail="SOP not found")
    _deserialize(raw)

    toggled = False
    for step in raw.get("steps", []):
        if step["id"] == step_id:
            for item in step.get("checklist", []):
                if item["id"] == item_id:
                    item["completed"] = not item["completed"]
                    toggled = True
                    break

    if not toggled:
        raise HTTPException(status_code=404, detail="Step or checklist item not found")

    # Auto-advance step status if all checklist items completed
    for step in raw.get("steps", []):
        if step["id"] == step_id:
            all_done = all(i.get("completed", False) for i in step.get("checklist", []))
            if all_done and step["status"] not in ("completed", "skipped"):
                step["status"] = "completed"

    update_item("sops", {"sopId": raw["sopId"], "projectId": project_id}, {
        "steps": json.dumps(raw["steps"]),
    })
    return raw


@router.post("/{project_id}/{stage}/approve")
def approve_sop(project_id: str, stage: str,
                user: dict = Depends(get_current_user)):
    """Approve the SOP — role-gated by stage."""
    if not _can_approve(user["role"], stage):
        allowed = STAGE_META[stage]["approveRoles"]
        raise HTTPException(status_code=403,
            detail=f"Approving {stage} SOP requires one of: {', '.join(allowed)}")

    raw = _get_sop(project_id, stage)
    if not raw:
        raise HTTPException(status_code=404, detail="SOP not found — generate it first")

    now = datetime.now(timezone.utc).isoformat()
    update_item("sops", {"sopId": raw["sopId"], "projectId": project_id}, {
        "status":     "approved",
        "approvedBy": user["username"],
        "approvedAt": now,
    })
    raw.update({"status": "approved", "approvedBy": user["username"], "approvedAt": now})
    return raw


@router.get("/{project_id}/{stage}/export")
def export_sop(project_id: str, stage: str,
               user: dict = Depends(get_current_user)):
    """Return the SOP as a Markdown string for download."""
    raw = _get_sop(project_id, stage)
    if not raw:
        raise HTTPException(status_code=404, detail="SOP not found")
    _deserialize(raw)

    lines = [
        f"# {raw.get('title', 'SOP')}",
        f"\n**Stage:** {STAGE_META.get(stage, {}).get('label', stage)}  ",
        f"**Status:** {raw.get('status', 'draft').capitalize()}  ",
        f"**Generated:** {raw.get('generatedAt', '')[:19].replace('T', ' ')}  ",
    ]
    if raw.get("approvedBy"):
        lines.append(f"**Approved by:** {raw['approvedBy']}  ")
        lines.append(f"**Approved at:** {raw.get('approvedAt', '')[:19].replace('T', ' ')}  ")

    lines += ["", f"> {raw.get('summary', '')}", ""]

    for step in raw.get("steps", []):
        status_icon = {"completed": "✅", "in_progress": "🔄", "skipped": "⏭", "pending": "⬜"}.get(step.get("status", "pending"), "⬜")
        lines.append(f"## {status_icon} Step {step.get('order', '')}: {step.get('title', '')}")
        lines.append(f"\n{step.get('description', '')}\n")
        for item in step.get("checklist", []):
            tick = "- [x]" if item.get("completed") else "- [ ]"
            lines.append(f"{tick} {item.get('text', '')}")
        if step.get("notes"):
            lines.append(f"\n**Notes:** {step['notes']}")
        lines.append("")

    lines += ["---", f"*Generated by AURA AI Dev Agent Platform*"]

    # Track export time
    try:
        update_item("sops", {"sopId": raw["sopId"], "projectId": project_id}, {
            "exportedAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    return {"markdown": "\n".join(lines), "filename": f"SOP_{stage}_{project_id[:8]}.md"}
