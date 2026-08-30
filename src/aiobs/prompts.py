"""Versioned prompts, and a playground for comparing them.

Prompts in this codebase live inline in agent modules, so changing one leaves no
record of what it used to say or what that change did to quality. A prompt is
config, not code: versioning it is what makes an experiment comparison meaningful
("v3 scored 0.82, v4 scored 0.71") instead of archaeology through git.

Versions are immutable and monotonic. Editing creates a new version rather than
mutating one, because an experiment result that points at a version which has
since changed underneath it is worse than no result at all.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TABLE = "ai-prompts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_key(n: int) -> str:
    # Zero-padded so lexicographic sort order matches numeric order — DynamoDB
    # sorts strings, and "v10" < "v9" would silently return the wrong latest.
    return f"v{n:06d}"


def save(prompt_id: str, template: str, actor: str, project_id: str = "",
         description: str = "") -> dict:
    """Append a new version. Never mutates an existing one."""
    from src.database import dynamo_client as db

    existing = versions(prompt_id)
    if existing:
        latest = existing[-1]
        if latest.get("template") == template:
            # Saving an unchanged prompt would create a version that means nothing.
            return latest
        next_n = int(str(latest.get("version", "v000000"))[1:]) + 1
    else:
        next_n = 1

    record = {
        "promptId": prompt_id,
        "version": _version_key(next_n),
        "template": template[:100_000],
        "hash": hashlib.sha256(template.encode("utf-8")).hexdigest()[:16],
        "projectId": project_id,
        "description": description,
        "createdAt": _now(),
        "createdBy": actor,
    }
    db.put_item(TABLE, record)
    return record


def versions(prompt_id: str, limit: int = 200) -> list[dict]:
    from src.database import dynamo_client as db
    rows = db.query_items(TABLE, "promptId", prompt_id, limit=limit) or []
    return sorted(rows, key=lambda r: r.get("version", ""))


def latest(prompt_id: str) -> dict | None:
    found = versions(prompt_id)
    return found[-1] if found else None


def get_version(prompt_id: str, version: str) -> dict | None:
    from src.database import dynamo_client as db
    return db.get_item(TABLE, {"promptId": prompt_id, "version": version})


def list_prompts(project_id: str = "", limit: int = 100) -> list[dict]:
    """Latest version of each prompt."""
    from src.database import dynamo_client as db
    rows = db.scan_items(TABLE, limit=limit * 20) or []
    if project_id:
        rows = [r for r in rows if r.get("projectId") == project_id]
    newest: dict[str, dict] = {}
    for row in rows:
        pid = row.get("promptId", "")
        if pid and (pid not in newest or row.get("version", "") > newest[pid].get("version", "")):
            newest[pid] = row
    return sorted(newest.values(), key=lambda r: r.get("createdAt", ""), reverse=True)[:limit]


def render(template: str, variables: dict | None = None) -> str:
    """Fill {placeholders}. A missing variable is left visible rather than raising,
    so the playground shows what is unfilled instead of erroring."""
    out = template or ""
    for key, value in (variables or {}).items():
        out = out.replace("{" + str(key) + "}", str(value))
    return out


def run_playground(template: str, variables: dict | None = None,
                   system: str = "", run_id: str = "playground") -> dict:
    """Execute one prompt and return output with cost — the compare loop.

    Goes through the same masked LLM seam as judges and agents, so a prompt tried
    here costs and behaves exactly as it will in production.
    """
    rendered = render(template, variables)
    from src.aiobs.judges import _invoke  # shared async bridge  # noqa: PLC2701
    try:
        text, call = _invoke(system or "You are a helpful assistant.",
                             rendered, "playground", run_id)
    except Exception as exc:  # noqa: BLE001
        return {"output": "", "error": str(exc)[:300], "rendered": rendered}
    return {
        "output": text,
        "rendered": rendered,
        "model": getattr(call, "model_id", ""),
        "inputTokens": getattr(call, "input_tokens", 0),
        "outputTokens": getattr(call, "output_tokens", 0),
        "costUsd": float(getattr(call, "cost_usd", 0.0) or 0.0),
        "latencyMs": getattr(call, "latency_ms", 0),
        "error": getattr(call, "error", None),
    }
