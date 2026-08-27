"""
Knowledge Validator Agent — Stage 5 of the AURA understanding pipeline.
Identifies contradictions, stale facts, and low-confidence relationships.
Works with graph_reviewer to produce a validated, confident knowledge graph.
"""
from __future__ import annotations

import json

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


_CONTRADICTION_QUERY = """
MATCH (a)-[r1]->(c)<-[r2]-(b)
WHERE r1.confidence < 0.5 AND r2.confidence < 0.5
  AND type(r1) = type(r2)
RETURN a.externalId AS source_a, b.externalId AS source_b,
       c.externalId AS target, type(r1) AS rel_type,
       r1.confidence AS conf_a, r2.confidence AS conf_b
LIMIT 20
"""


class KnowledgeValidatorAgent(BaseAgent):
    name = "knowledge_validator"
    description = (
        "Identifies contradictions, low-confidence facts, and stale relationships "
        "in the knowledge graph. Recommends corrections or flags for human review."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        reviewer = context.prior_results.get("graph_reviewer")
        if not reviewer:
            result.log("No graph_reviewer result — running standalone validation")

        result.log("Querying for low-confidence and contradictory relationships")
        contradictions: list[dict] = []
        try:
            raw = neo4j.run_query(_CONTRADICTION_QUERY)
            contradictions = [dict(r) for r in raw]
            result.log(f"Found {len(contradictions)} potential contradiction(s)")
        except Exception as exc:  # noqa: BLE001
            result.log(f"Contradiction query failed: {exc}")

        issues_from_reviewer = reviewer.output.get("issues", []) if reviewer else []
        prompt = _build_prompt(contradictions, issues_from_reviewer, context.intent)

        try:
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = client.invoke_model(
                modelId=s.bedrock_model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            raw_resp = json.loads(resp["body"].read())
            validation = json.loads(raw_resp["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            validation = {"verdict": "partial", "actions": [], "human_review_needed": contradictions}

        result.log(f"Validation verdict: {validation.get('verdict', 'unknown')}")
        result.output = {
            "contradictions_found": len(contradictions),
            "verdict": validation.get("verdict", "partial"),
            "auto_fixed": len(validation.get("actions", [])),
            "human_review_needed": len(validation.get("human_review_needed", [])),
            "actions": validation.get("actions", []),
        }
        return result.finish("success")


def _build_prompt(contradictions: list[dict], issues: list[str], intent: str) -> str:
    return (
        "You are AURA's knowledge validator. Review potential contradictions and issues.\n\n"
        f"Intent: {intent}\n"
        f"Contradictions: {json.dumps(contradictions[:10])}\n"
        f"Graph issues: {json.dumps(issues)}\n\n"
        "Return JSON: {verdict: 'clean'|'partial'|'needs_review', "
        "actions: [{type: 'retire'|'update_confidence'|'merge', entity_id, reason}], "
        "human_review_needed: [entity_id]}"
    )
