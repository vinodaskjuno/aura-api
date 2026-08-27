"""
Tour Builder Agent — Stage 6 of the AURA understanding pipeline.
Generates structured guided learning paths ("tours") through the knowledge
graph for different personas (developer, architect, business analyst).
"""
from __future__ import annotations

import json

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class TourBuilderAgent(BaseAgent):
    name = "tour_builder"
    description = (
        "Generates guided learning paths ('tours') through the knowledge graph "
        "for developer, architect, and business analyst personas."
    )

    _PERSONAS = ["developer", "architect", "business_analyst", "security_engineer", "onboarding"]

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        builder = context.prior_results.get("graph_builder")
        reviewer = context.prior_results.get("graph_reviewer")

        graph_summary = builder.output if builder else {}
        quality = reviewer.output.get("quality_score", 0.5) if reviewer else 0.5
        result.log(f"Building tours (graph quality: {quality:.0%})")

        # Gather key nodes to anchor tours
        try:
            anchor_query = """
            MATCH (n)
            WHERE n.node_type IN ['Application', 'Service', 'BusinessDomain', 'Repository']
            RETURN n.externalId AS id, n.name AS name, n.node_type AS type
            LIMIT 20
            """
            anchors = [dict(r) for r in neo4j.run_query(anchor_query)]
        except Exception as exc:  # noqa: BLE001
            result.log(f"Anchor query failed: {exc}")
            anchors = []

        tours: list[dict] = []
        for persona in self._PERSONAS:
            prompt = _build_prompt(persona, anchors, graph_summary, context.intent)
            try:
                client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
                body = json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                })
                resp = client.invoke_model(
                    modelId=s.bedrock_model_id,
                    contentType="application/json",
                    accept="application/json",
                    body=body,
                )
                raw = json.loads(resp["body"].read())
                tour = json.loads(raw["content"][0]["text"])
                tour["persona"] = persona
                tours.append(tour)
                result.log(f"Built {persona} tour: {len(tour.get('steps', []))} steps")
            except Exception as exc:  # noqa: BLE001
                result.log(f"Tour generation failed for {persona}: {exc}")

        result.output = {
            "tours": tours,
            "personas": [t["persona"] for t in tours],
            "total_steps": sum(len(t.get("steps", [])) for t in tours),
        }
        return result.finish("success")


def _build_prompt(persona: str, anchors: list[dict], graph_summary: dict, intent: str) -> str:
    return (
        f"You are AURA's tour builder. Create a guided tour for a {persona}.\n\n"
        f"Intent: {intent}\n"
        f"Key nodes available: {json.dumps(anchors[:10])}\n"
        f"Graph summary: {json.dumps(graph_summary)[:1000]}\n\n"
        "Return JSON: {title: string, description: string, estimated_minutes: int, "
        "steps: [{step: int, title: string, node_id: string, description: string, "
        "key_insight: string, next_step_hint: string}]}"
    )
