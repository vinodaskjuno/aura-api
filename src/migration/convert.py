"""Convert a finalized migration, one component at a time.

Why per-component rather than one call: `CodeGenerationAgent` caps at 4096 output
tokens, and a real application will not come back in one response. Iterating also
means progress is reportable ("12 of 18") and a run that dies halfway can be resumed
instead of restarted — which matters most in the situation where it is most likely to
happen, namely a live demo.

What this does NOT claim: that the output runs untouched. Generated files carry
`TODO(aura)` markers wherever the strategy said a human has to decide. A migration
tool that silently guesses at those is a tool whose output nobody can review, and
"reviewable" was the whole point of the scope we agreed.

Components with a `drop` verdict are not converted at all, and components marked
`manual` produce a stub plus a note rather than invented logic. Both appear in the
manifest so the reader can see what was deliberately not done.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Files per batch of one component. A component that needs more than this is almost
# certainly one that should have been marked `manual`.
MAX_FILES_PER_COMPONENT = 6

_SYSTEM = """You convert one component of a legacy application into its equivalent on \
a target platform. You are given the component, the migration strategy's verdict for \
it, the confirmed component standards, and the shape the output should take.

Rules:

1. Use the CONFIRMED STANDARDS for anything cross-cutting. If secrets are Vault, read
   secrets from Vault — never hardcode one, and never substitute a different service.
2. Where the strategy says a human must decide, emit a `TODO(aura): <question>`
   comment at that exact point rather than guessing. A wrong guess is worse than a
   marked gap, because a gap gets reviewed.
3. Produce idiomatic target-platform code. Do not transliterate the source's
   structure if the target has its own way of expressing the same thing.
4. Keep behaviour, not implementation. Retries, error handling and scheduling should
   express the same INTENT using the target's own mechanisms.
5. Include a short header comment naming the source component this came from.

Return ONLY JSON:
{
  "files": [{"filename": "dags/x.py", "language": "python", "content": "..."}],
  "notes": ["anything the reviewer must know"],
  "todos": ["each decision you left to a human"]
}"""


def _prompt(component: dict, session: dict, source_text: str) -> str:
    mapping = [m for m in (session.get("mapping") or []) if m.get("technology")]
    profile_layout = _layout(session)

    parts = [
        f"SOURCE PLATFORM: {session.get('source')}",
        f"TARGET PLATFORM: {session.get('target')}",
        "",
        "COMPONENT TO CONVERT:",
        json.dumps(component, indent=2),
    ]

    if mapping:
        parts += ["", "CONFIRMED STANDARDS — target these exactly:"]
        parts += [f"  {m.get('capabilityLabel') or m['capability']}: {m['technology']}"
                  for m in mapping]

    shape = session.get("conversionShape") or {}
    if shape:
        parts += ["", f"OUTPUT SHAPE: {json.dumps(shape)}"]
    if profile_layout:
        parts += ["", f"FILE LAYOUT (use these paths): {json.dumps(profile_layout)}"]

    if source_text:
        parts += ["", "SOURCE (truncated):", source_text[:12000]]

    verdict = component.get("verdict")
    if verdict == "manual":
        parts += ["", "This component is marked MANUAL. Produce a stub with the right "
                      "signature and a TODO(aura) explaining what a human must decide. "
                      "Do not invent the logic."]
    elif verdict == "rewrite":
        parts += ["", "This component is marked REWRITE. The target needs a different "
                      "design — do not transliterate the source."]

    return "\n".join(parts)


def _layout(session: dict) -> dict:
    from src.migration.profiles import profile_for
    return dict(profile_for(session.get("source", ""), session.get("target", "")).target_layout)


def _source_text(project_id: str, component: dict) -> str:
    """The component's own source, if we can find it on the workspace.

    Best effort. Converting from the strategy's description alone produces something
    plausible rather than something equivalent, so it is worth looking — but a
    missing file must not stop the run.
    """
    from pathlib import Path

    name = str(component.get("name") or "").strip()
    if not name:
        return ""
    try:
        from src.database.dynamo_client import scan_items
        root = ""
        for row in scan_items("projects", limit=500):
            if row.get("projectId") == project_id:
                root = row.get("clonedPath") or ""
                break
        if not root or not Path(root).exists():
            return ""
        base = Path(name).name
        for found in Path(root).rglob(base):
            if found.is_file() and found.stat().st_size < 400_000:
                return found.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("source lookup failed for %s: %s", name, exc)
    return ""


async def convert_one(component: dict, session: dict) -> dict:
    """Convert one component. Never raises — a failure is recorded and the run goes on.

    One bad component must not abandon the other seventeen. The manifest records what
    failed so the reader is not left to notice an absence.
    """
    import boto3
    from src.config_settings import get_settings

    name = component.get("name", "component")
    if component.get("verdict") == "drop":
        return {"component": name, "status": "skipped", "files": [],
                "notes": ["Marked `drop` in the strategy — deliberately not converted."],
                "todos": []}

    try:
        s = get_settings()
        client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": _prompt(
                component, session, _source_text(session["projectId"], component))}],
        }
        resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                   contentType="application/json", accept="application/json")
        text = json.loads(resp["body"].read())["content"][0]["text"]

        start, end = text.find("{"), text.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("model returned no JSON object")

        data = json.loads(text[start:end])
        files = [f for f in (data.get("files") or [])
                 if f.get("filename") and f.get("content")][:MAX_FILES_PER_COMPONENT]
        return {
            "component": name,
            "status": "converted" if files else "empty",
            "files": files,
            "notes": [str(n) for n in (data.get("notes") or [])],
            "todos": [str(t) for t in (data.get("todos") or [])],
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("conversion failed for %s: %s", name, exc)
        return {"component": name, "status": "failed", "files": [],
                "notes": [f"Conversion failed: {exc}"], "todos": []}


def package(session: dict, results: list[dict]) -> tuple[str, int]:
    """Zip the generated files into the exports bucket. Returns (key, file count).

    Includes a MIGRATION.md manifest, because a folder of generated code with no
    account of what was skipped, what failed and what still needs a decision is not
    reviewable — and reviewable is what was promised.
    """
    from src.migration import runtime
    from src.storage import s3_client

    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for result in results:
            for f in result.get("files") or []:
                path = str(f["filename"]).lstrip("/")
                # Two components generating the same path would silently overwrite;
                # suffixing keeps both and makes the collision visible.
                if path in seen:
                    stem, _, ext = path.rpartition(".")
                    path = f"{stem}__{result['component']}.{ext}" if ext else f"{path}__{result['component']}"
                seen.add(path)
                zf.writestr(path, str(f["content"]))
                written += 1
        zf.writestr("MIGRATION.md", _manifest(session, results))

        # A runnable stack, where the target has one. Without it the download is
        # reviewable but not testable — nothing in it starts, so the last step of the
        # migration story ("test it") has nothing to act on. Files already generated
        # win: a converter that emitted its own compose file meant to.
        stack = runtime.for_target(session.get("target", ""))
        if stack:
            for name, body in stack.files.items():
                if name not in seen:
                    zf.writestr(name, body)
                    written += 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = (f"migrations/{session['projectId']}/"
           f"{session['source']}-to-{session['target']}-{stamp}.zip")
    s3_client.put_object("exports", key, buf.getvalue(), "application/zip")
    return key, written


def _manifest(session: dict, results: list[dict]) -> str:
    strategy = session.get("strategy") or {}
    mapping = [m for m in (session.get("mapping") or []) if m.get("technology")]

    converted = [r for r in results if r["status"] == "converted"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] in ("failed", "empty")]
    todos = [(r["component"], t) for r in results for t in r.get("todos") or []]

    lines = [
        f"# {session['source']} → {session['target']}",
        "",
        "Generated by Aura. **Reviewable, not runnable** — every `TODO(aura)` marker "
        "is a decision left to a person, on purpose.",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Project: {session.get('projectName') or session['projectId']}",
        f"- Converted: {len(converted)} · Skipped: {len(skipped)} · "
        f"Needs attention: {len(failed)}",
        "",
    ]

    if strategy.get("summary"):
        lines += ["## Strategy", "", strategy["summary"], ""]

    if mapping:
        lines += ["## Component standards used", "",
                  "| Capability | Technology | Chosen |", "|---|---|---|"]
        origin = {"inferred": "inferred from your estate", "chat": "asked for in chat",
                  "user": "set by hand", "unset": "not set"}
        lines += [f"| {m.get('capabilityLabel') or m['capability']} | {m['technology']} "
                  f"| {origin.get(m.get('origin'), m.get('origin', ''))} |"
                  for m in mapping]
        lines.append("")

    if todos:
        lines += ["## Decisions left to you", ""]
        lines += [f"- **{comp}** — {todo}" for comp, todo in todos]
        lines.append("")

    if skipped:
        lines += ["## Deliberately not converted", ""]
        lines += [f"- **{r['component']}** — {'; '.join(r.get('notes') or [])}"
                  for r in skipped]
        lines.append("")

    if failed:
        lines += ["## Needs attention", "",
                  "These produced nothing usable. Treat them as unconverted.", ""]
        lines += [f"- **{r['component']}** — {'; '.join(r.get('notes') or []) or 'no output'}"
                  for r in failed]
        lines.append("")

    risks = strategy.get("risks") or []
    if risks:
        lines += ["## Risks carried from the strategy", ""]
        lines += [f"- **{r.get('severity', 'medium')}** — {r.get('text', '')}"
                  for r in risks]
        lines.append("")

    return "\n".join(lines)


def run(session_id: str, project_id: str, actor: str) -> None:
    """Convert every component, then package. Designed to run on a worker thread.

    Resumable: components already recorded as converted are skipped, so a second
    call finishes an interrupted run rather than paying for it twice.
    """
    import asyncio

    from src.graph import provenance
    from src.migration import session as sessions

    session = sessions.get(session_id, project_id)
    if not session:
        log.error("conversion: no session %s", session_id)
        return

    components = (session.get("strategy") or {}).get("components") or []
    done = {r["component"] for r in (session.get("conversionResults") or [])}
    results = list(session.get("conversionResults") or [])

    with provenance.trace_run(
        provenance.PIPELINE_MIGRATION,
        trigger=provenance.TRIGGER_MANUAL,
        actor=actor,
        source="migration",
        sourceDetail=f"convert {session['source']} → {session['target']}",
        projectId=project_id,
        sessionId=session_id,
        writtenBy="migration.convert",
    ) as run_ctx:
        try:
            session = sessions.save(session, stage="converting",
                                    conversionProgress={"done": len(done),
                                                        "total": len(components),
                                                        "current": ""})
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            for component in components:
                name = component.get("name", "")
                if name in done:
                    continue
                session = sessions.save(session, conversionProgress={
                    "done": len(results), "total": len(components), "current": name})

                result = loop.run_until_complete(convert_one(component, session))
                results.append(result)
                if result["status"] == "failed":
                    run_ctx.fail(f"{name}: {'; '.join(result.get('notes') or [])}")

                # Persisted after EVERY component, not at the end: that is what makes
                # an interrupted run resumable rather than lost.
                session = sessions.save(session, conversionResults=results)

            key, file_count = package(session, results)
            sessions.save(session, stage="converted", artifactKey=key,
                          conversionProgress={"done": len(results),
                                              "total": len(components), "current": ""},
                          conversionFileCount=file_count)
            log.info("migration %s converted: %d files", session_id, file_count)

        except Exception as exc:  # noqa: BLE001
            log.exception("conversion run failed for %s", session_id)
            sessions.save(session, stage="failed",
                          errors=list(session.get("errors") or []) + [str(exc)])
