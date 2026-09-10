"""Propose how to migrate one application onto a different platform.

Reads the knowledge graph plus the parsed source facts and returns a strategy: which
components map onto what, which cannot move, what the risks are, what nobody knows
yet, and what it needs to ask before any code is written.

Two things this agent is prompted hard about, because both are easy to get wrong in
a way that looks like success:

  * **What cannot move.** A migration report that lists only what ports is the report
    a customer stops trusting the moment they find the first thing it missed. `drop`
    and `manual` are first-class verdicts, and an empty risk list is treated as a
    failed run rather than a clean bill of health — see `_looks_too_optimistic`.

  * **The confirmed component mapping.** If the user said Vault, generated code must
    reach for Vault. The mapping is passed in and repeated in the prompt rather than
    left for the model to infer from the estate a second time.
"""
from __future__ import annotations

import json
import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent

log = logging.getLogger(__name__)

VERDICTS = ("migrate", "rewrite", "drop", "manual")

_SYSTEM = """You are a migration architect. You are given a legacy application's \
knowledge graph, its parsed source facts, a target platform, and the component \
standards the customer has already confirmed.

Produce a migration strategy as JSON. Be specific to THIS application — generic \
platform advice is worthless here.

Rules you must follow:

1. Every component gets a verdict:
   - "migrate"  mechanical translation, behaviour preserved
   - "rewrite"  same intent, but the target needs a different design
   - "drop"     should not be carried across at all
   - "manual"   a human must decide or hand-build this
2. You MUST identify things that cannot be migrated. If you genuinely find none,
   say so explicitly in `unknowns` and explain why — do not return an empty list to
   look agreeable.
3. Risks must be concrete and about THIS application. "Migrations carry risk" is
   not a risk. Name the component and what breaks.
4. Ask questions only where the answer would change the strategy. State why each
   one matters.
5. Use the confirmed component standards for anything cross-cutting. If the customer
   confirmed HashiCorp Vault for secrets, the strategy targets Vault, not whatever
   the source used.

Return ONLY this JSON shape:
{
  "summary": "2-3 sentences a technical lead would accept",
  "components": [
    {"name": "...", "sourceType": "...", "targetType": "...",
     "verdict": "migrate|rewrite|drop|manual", "confidence": 0.0-1.0,
     "note": "why this verdict"}
  ],
  "risks":    [{"severity": "high|medium|low", "text": "..."}],
  "unknowns": ["..."],
  "questions":[{"id": "q1", "text": "...", "why": "..."}],
  "effort":   {"components": 0, "automatable": 0, "manual": 0}
}"""


def _looks_too_optimistic(strategy: dict) -> str:
    """Is this strategy suspiciously agreeable?

    Returned as a warning on the result rather than an exception: the strategy is
    still shown, but the UI can mark it as needing a second look. A run that found
    nothing hard is far more likely to be a weak run than an easy application.
    """
    components = strategy.get("components") or []
    risks = strategy.get("risks") or []
    hard = [c for c in components if c.get("verdict") in ("drop", "manual")]
    if components and not risks:
        return ("No risks were identified across "
                f"{len(components)} components. Treat this strategy as incomplete.")
    if len(components) >= 5 and not hard:
        return (f"All {len(components)} components were marked migrate or rewrite, "
                "with nothing needing a human. That is unusual — review before trusting it.")
    return ""


def _normalise(strategy: dict) -> dict:
    """Coerce the model's output into the shape the UI and tests rely on."""
    out = {
        "summary": str(strategy.get("summary") or ""),
        "components": [],
        "risks": [],
        "unknowns": [str(u) for u in (strategy.get("unknowns") or [])],
        "questions": [],
        "effort": {},
    }

    for i, c in enumerate(strategy.get("components") or []):
        verdict = str(c.get("verdict") or "").lower()
        out["components"].append({
            "name": str(c.get("name") or f"component-{i + 1}"),
            "sourceType": str(c.get("sourceType") or ""),
            "targetType": str(c.get("targetType") or ""),
            # An unrecognised verdict becomes "manual", never "migrate". Guessing in
            # the permissive direction is how something unportable ends up converted.
            "verdict": verdict if verdict in VERDICTS else "manual",
            "confidence": float(c.get("confidence") or 0.0),
            "note": str(c.get("note") or ""),
        })

    for r in strategy.get("risks") or []:
        if isinstance(r, str):
            out["risks"].append({"severity": "medium", "text": r})
        else:
            sev = str(r.get("severity") or "medium").lower()
            out["risks"].append({
                "severity": sev if sev in ("high", "medium", "low") else "medium",
                "text": str(r.get("text") or ""),
            })

    for i, q in enumerate(strategy.get("questions") or []):
        if isinstance(q, str):
            out["questions"].append({"id": f"q{i + 1}", "text": q, "why": ""})
        else:
            out["questions"].append({
                "id": str(q.get("id") or f"q{i + 1}"),
                "text": str(q.get("text") or ""),
                "why": str(q.get("why") or ""),
            })

    components = out["components"]
    effort = strategy.get("effort") or {}
    out["effort"] = {
        "components": int(effort.get("components") or len(components)),
        "automatable": int(effort.get("automatable")
                           or len([c for c in components if c["verdict"] == "migrate"])),
        "manual": int(effort.get("manual")
                      or len([c for c in components
                              if c["verdict"] in ("manual", "rewrite", "drop")])),
    }
    return out


class MigrationStrategyAgent(BaseAgent):
    name = "migration_strategy_agent"
    description = ("Propose a migration strategy from one platform to another: "
                   "component mapping, verdicts, risks, unknowns and open questions")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        extra = context.extra or {}
        source = extra.get("source", "")
        target = extra.get("target", "")
        result.log(f"Planning migration {source} → {target}")

        try:
            from src.config_settings import get_settings
            from src.migration.profiles import profile_for
            import boto3

            profile = profile_for(source, target)
            user_prompt = self._build_prompt(context, profile)

            s = get_settings()
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 8192,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                       contentType="application/json",
                                       accept="application/json")
            text = json.loads(resp["body"].read())["content"][0]["text"]

            start, end = text.find("{"), text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ValueError("model returned no JSON object")

            strategy = _normalise(json.loads(text[start:end]))
            warning = _looks_too_optimistic(strategy)
            if warning:
                strategy["warning"] = warning
                result.log(f"Optimism check: {warning}")

            result.output = strategy
            result.log(f"{len(strategy['components'])} components, "
                       f"{len(strategy['risks'])} risks, "
                       f"{len(strategy['questions'])} questions")
            return result.finish("success")

        except Exception as exc:  # noqa: BLE001 — a failed plan must not break the session
            log.warning("migration strategy failed: %s", exc)
            result.output = {"error": str(exc), "components": [], "risks": [],
                             "unknowns": [], "questions": [], "effort": {}}
            result.log(f"MigrationStrategyAgent failed: {exc}")
            return result.finish("failed")

    def _build_prompt(self, context: AgentContext, profile) -> str:
        extra = context.extra or {}
        parts: list[str] = [
            f"SOURCE PLATFORM: {extra.get('source', 'unknown')}",
            f"TARGET PLATFORM: {extra.get('target', 'unknown')}",
        ]

        if profile.curated:
            parts.append(
                "\nKNOWN COMPONENT MAPPING for this pair (authoritative — use it):\n"
                + "\n".join(f"  {k} -> {v}" for k, v in profile.component_map.items()))
            parts.append(
                "\nKNOWN RISKS for this pair (include any that apply, and look for more):\n"
                + "\n".join(f"  - {r}" for r in profile.known_risks))
        else:
            # Said out loud so the model does not assume a mapping it was not given.
            parts.append(
                "\nNo curated mapping exists for this pair. Derive the component "
                "mapping from the source facts and the target platform's own idioms.")

        mapping = extra.get("mapping") or []
        confirmed = [m for m in mapping if m.get("technology")]
        if confirmed:
            parts.append(
                "\nCONFIRMED COMPONENT STANDARDS — the customer has already chosen "
                "these. Target them, and do not substitute alternatives:\n"
                + "\n".join(f"  {m.get('capabilityLabel') or m['capability']}: "
                            f"{m['technology']}" for m in confirmed))

        shape = extra.get("conversionShape") or {}
        if shape:
            parts.append(f"\nCONVERSION SHAPE: {json.dumps(shape)}")

        answers = extra.get("answers") or []
        if answers:
            parts.append(
                "\nANSWERS the customer has already given — these override your "
                "assumptions:\n"
                + "\n".join(f"  Q: {a.get('question', '')}\n  A: {a.get('answer', '')}"
                            for a in answers))

        comments = extra.get("comments") or []
        if comments:
            parts.append(
                "\nCUSTOMER FEEDBACK on your previous strategy — revise accordingly:\n"
                + "\n".join(f"  - {c}" for c in comments))

        facts = extra.get("facts") or {}
        if facts:
            parts.append(
                f"\nSOURCE TREE — {facts.get('fileCount', 0)} files at "
                f"{facts.get('root', '')}\n"
                f"By extension: {json.dumps(facts.get('byExtension', {}))}\n"
                f"Files:\n" + "\n".join(f"  {f}" for f in (facts.get('files') or [])[:250]))
            if facts.get("listingTruncated"):
                parts.append("  (listing truncated — there are more files than shown)")

            for ex in facts.get("excerpts") or []:
                parts.append(
                    f"\n----- {ex['path']}"
                    f"{' (truncated)' if ex.get('truncated') else ''} -----\n"
                    f"{ex['content']}")

            parts.append(
                "\nThe files above ARE the application. Enumerate its components from "
                "them — one entry per process, flow, job, script or module you can see. "
                "Do not ask for file paths: you have them.")
        else:
            # Said explicitly. Left unsaid, the agent assumes the application is
            # empty and returns a strategy with no components and no explanation.
            parts.append(
                "\nNO SOURCE FILES WERE AVAILABLE to read. Work from the knowledge "
                "graph alone. If it too is empty, say so in `summary`, put what you "
                "need in `questions`, and return an empty `components` list — do not "
                "invent components.")

        graph = context.kg_snapshot or {}
        if graph:
            nodes = graph.get("nodes") or []
            links = graph.get("links") or []
            parts.append(
                f"\nKNOWLEDGE GRAPH: {len(nodes)} nodes, {len(links)} relationships\n"
                + json.dumps({"nodes": nodes[:120], "links": links[:200]})[:8000])

        if profile.questions:
            parts.append(
                "\nQUESTIONS THIS PAIR USUALLY NEEDS ANSWERED (ask any still open):\n"
                + "\n".join(f"  - {q}" for q in profile.questions))

        return "\n".join(parts)
