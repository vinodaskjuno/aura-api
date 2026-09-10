"""Conversational adjustment of a migration — propose, then confirm.

The chat is the input; the mapping table is the state. Nothing the model suggests
takes effect until a person accepts it, and every accepted change is stamped with
where it came from, so the strategy can still answer "why is this targeting Vault?"

This mirrors the protocol already proven for the ontology maintainer chat
(`routers/ontology_universe.py`): stream tokens, emit a proposal carrying a
`changeId`, wait for a confirmation naming that id. Reusing the shape matters more
than it might seem — "an agent proposes N changes and a human accepts a subset" is
fiddly to get right, and it is already right once in this codebase.

Four kinds of change, matching what a user actually asks for mid-migration:

    mapping   swap the technology filling a capability      ("we use Vault")
    shape     change the shape of the generated output      ("one DAG per process")
    verdict   override Aura's call on one component         ("don't port the OCR bot")
    target    switch target platform                        ("make it Step Functions")

`target` is destructive — it discards the strategy — so it is flagged as such and the
UI must warn before applying it. The others are additive and safe to accept casually.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

KINDS = ("mapping", "shape", "verdict", "target")

_SYSTEM = """You help a user adjust an in-progress platform migration by talking to \
them. You do not perform the migration; you propose changes to its configuration.

You will be given the current state: source and target platform, the confirmed \
component mapping, the conversion shape, and the strategy's components if one exists.

Decide whether the user's message asks for a change. If it does, return proposals. If \
it is a question, answer it and return no proposals.

The four kinds of change you may propose:

  mapping  — a capability should be filled by a different technology.
             Needs: capability (one of the given ids), to (the technology).
  shape    — the generated output should be structured differently.
             Needs: field (granularity|extractShared|repoLayout), to (the value).
  verdict  — one component's verdict is wrong.
             Needs: component (its exact name), to (migrate|rewrite|drop|manual).
  target   — the whole migration should aim at a different platform.
             Needs: to (the platform). WARNING: this discards the strategy. Only
             propose it when the user clearly asks to change platform.

Rules:
- Never invent a capability id or a component name. Use only what you were given.
- One proposal per distinct change. Do not bundle two swaps into one.
- `reason` is what the user told you, restated — not your own justification.
- If the user is vague about which capability they mean, ask instead of guessing.

Return ONLY JSON:
{
  "reply": "one or two sentences to the user",
  "proposals": [
    {"kind": "mapping", "capability": "secrets", "to": "HashiCorp Vault",
     "reason": "customer standardises on Vault"}
  ]
}"""


def _state_for_prompt(session: dict) -> str:
    mapping = session.get("mapping") or []
    components = session.get("components") or []
    return json.dumps({
        "source": session.get("source"),
        "target": session.get("target"),
        "stage": session.get("stage"),
        "capabilities": [
            {"id": m.get("capability"), "label": m.get("capabilityLabel"),
             "current": m.get("technology") or None}
            for m in mapping
        ],
        "conversionShape": session.get("conversionShape") or {},
        # Names only. Sending full notes would crowd out the mapping, and the model
        # only needs the names to reference a component in a verdict proposal.
        "components": [c.get("name") for c in components][:80],
    }, indent=2)


def _validate(proposal: dict, session: dict) -> tuple[bool, str]:
    """Reject a proposal that names something that does not exist.

    A model that invents a capability id produces a change which silently applies to
    nothing, and the user sees an accepted proposal that did not do anything — worse
    than a rejection, because it looks like it worked.
    """
    kind = proposal.get("kind")
    if kind not in KINDS:
        return False, f"unknown change kind {kind!r}"

    target_value = str(proposal.get("to") or "").strip()
    if not target_value:
        return False, "proposal has no target value"

    if kind == "mapping":
        ids = {m.get("capability") for m in (session.get("mapping") or [])}
        if proposal.get("capability") not in ids:
            return False, f"no such capability {proposal.get('capability')!r}"

    elif kind == "shape":
        if proposal.get("field") not in ("granularity", "extractShared", "repoLayout"):
            return False, f"no such shape field {proposal.get('field')!r}"

    elif kind == "verdict":
        names = {c.get("name") for c in (session.get("components") or [])}
        if proposal.get("component") not in names:
            return False, f"no component named {proposal.get('component')!r}"
        if target_value not in ("migrate", "rewrite", "drop", "manual"):
            return False, f"{target_value!r} is not a verdict"

    return True, ""


def _describe(proposal: dict, session: dict) -> dict:
    """Turn a proposal into the before/after the UI renders as a diff."""
    kind = proposal["kind"]
    to = str(proposal.get("to") or "")

    if kind == "mapping":
        row = next((m for m in (session.get("mapping") or [])
                    if m.get("capability") == proposal.get("capability")), {})
        return {"kind": kind, "capability": proposal["capability"],
                "label": row.get("capabilityLabel") or proposal["capability"],
                "from": row.get("technology") or "(not set)", "to": to,
                "reason": proposal.get("reason", ""), "destructive": False}

    if kind == "shape":
        shape = session.get("conversionShape") or {}
        field = proposal["field"]
        return {"kind": kind, "field": field, "label": f"Output shape · {field}",
                "from": str(shape.get(field, "(unset)")), "to": to,
                "reason": proposal.get("reason", ""), "destructive": False}

    if kind == "verdict":
        comp = next((c for c in (session.get("components") or [])
                     if c.get("name") == proposal.get("component")), {})
        return {"kind": kind, "component": proposal["component"],
                "label": f"Verdict · {proposal['component']}",
                "from": comp.get("verdict") or "(none)", "to": to,
                "reason": proposal.get("reason", ""), "destructive": False}

    # target
    return {"kind": kind, "label": "Target platform",
            "from": session.get("target") or "(none)", "to": to,
            "reason": proposal.get("reason", ""),
            # The UI must warn before applying this one.
            "destructive": True,
            "warning": ("Switching target discards the current strategy and returns "
                        "to the start. The old strategy is archived, not deleted.")}


async def propose(session: dict, text: str, user: dict) -> AsyncIterator[dict]:
    """Stream a reply, then a single proposal frame carrying every valid change.

    One frame rather than one per change: the user accepts or rejects as a set, or a
    subset by index, which is how the ontology chat already behaves.
    """
    try:
        import boto3
        from src.config_settings import get_settings

        s = get_settings()
        client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
        prompt = f"CURRENT STATE:\n{_state_for_prompt(session)}\n\nUSER SAID:\n{text}"

        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": prompt}]}
        resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                   contentType="application/json", accept="application/json")
        raw = json.loads(resp["body"].read())["content"][0]["text"]

        start, end = raw.find("{"), raw.rfind("}") + 1
        if start < 0 or end <= start:
            yield {"type": "token", "text": raw.strip()[:800]}
            yield {"type": "done"}
            return

        parsed = json.loads(raw[start:end])
        reply = str(parsed.get("reply") or "")
        if reply:
            yield {"type": "token", "text": reply}

        changes: list[dict] = []
        rejected: list[str] = []
        for p in parsed.get("proposals") or []:
            ok, why = _validate(p, session)
            if ok:
                changes.append(_describe(p, session))
            else:
                # Logged and reported rather than dropped: a silently discarded
                # proposal looks to the user like the model ignored them.
                log.info("migration chat rejected a proposal: %s", why)
                rejected.append(why)

        if rejected:
            yield {"type": "note",
                   "text": "Some suggestions were discarded because they named "
                           "something that does not exist: " + "; ".join(rejected)}

        if changes:
            yield {
                "type": "proposal",
                "changeId": str(uuid.uuid4()),
                "changes": changes,
                "destructive": any(c.get("destructive") for c in changes),
                "summary": f"{len(changes)} change"
                           f"{'' if len(changes) == 1 else 's'} proposed",
            }
        yield {"type": "done"}

    except Exception as exc:  # noqa: BLE001 — a chat failure must not kill the session
        log.warning("migration chat failed: %s", exc)
        yield {"type": "error", "message": str(exc)}


def apply(session: dict, changes: list[dict], accept: list[int] | None = None) -> dict:
    """Apply the accepted subset. Returns the updated session.

    `accept` is a list of indices into `changes`; None means all of them. Applying a
    subset is the point of the confirmation step — a user who wants one of three
    suggested swaps should not have to reject all three and retype.
    """
    from src.migration import session as sessions

    chosen = changes if accept is None else [
        changes[i] for i in accept if 0 <= i < len(changes)]
    if not chosen:
        return session

    # Target switch is handled first and alone: it resets the session, so applying
    # anything else in the same batch would write into state about to be discarded.
    for change in chosen:
        if change.get("kind") == "target":
            return sessions.reset_for_new_target(session, str(change["to"]))

    mapping = [dict(m) for m in (session.get("mapping") or [])]
    shape = dict(session.get("conversionShape") or {})
    components = [dict(c) for c in (session.get("components") or [])]
    strategy = dict(session.get("strategy") or {})
    touched_mapping = touched_shape = touched_verdict = False

    for change in chosen:
        kind = change.get("kind")

        if kind == "mapping":
            for row in mapping:
                if row.get("capability") == change.get("capability"):
                    row["technology"] = change["to"]
                    # Stamped so the strategy can say the user asked for this in
                    # chat rather than it having been inferred from the estate.
                    row["origin"] = "chat"
                    touched_mapping = True

        elif kind == "shape":
            field = change.get("field")
            value: Any = change["to"]
            if field == "extractShared":
                value = str(value).strip().lower() in ("true", "yes", "1")
            shape[field] = value
            touched_shape = True

        elif kind == "verdict":
            for comp in components:
                if comp.get("name") == change.get("component"):
                    comp["verdict"] = change["to"]
                    touched_verdict = True

    updates: dict[str, Any] = {}
    if touched_mapping:
        updates["mapping"] = mapping
    if touched_shape:
        updates["conversionShape"] = shape
    if touched_verdict:
        updates["components"] = components
        # Keep the strategy's own copy in step, or the table and the strategy
        # disagree about a verdict the user just changed.
        if strategy.get("components"):
            strategy["components"] = components
            updates["strategy"] = strategy

    return sessions.save(session, **updates) if updates else session
