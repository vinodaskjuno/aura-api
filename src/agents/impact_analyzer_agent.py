"""
Impact Analyzer Agent — on-demand agent triggered by /understand-impact.
Given a change (file, PR, function, config), it traverses the knowledge
graph to identify all downstream technical and business impacts.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


_DOWNSTREAM_QUERY = """
MATCH path = (start)-[:CALLS|IMPORTS|DEPENDS_ON|RUNS_ON|DEPLOYED_TO|IMPLEMENTS*1..5]->(impact)
WHERE start.externalId = $entity_id
RETURN impact.externalId AS id, impact.name AS name,
       impact.node_type AS type, length(path) AS distance
ORDER BY distance
LIMIT 50
"""

_BUSINESS_IMPACT_QUERY = """
MATCH (start)-[:CALLS|IMPORTS|IMPLEMENTS*1..3]->(biz)
WHERE start.externalId = $entity_id
  AND biz.node_type IN ['BusinessRule', 'BusinessProcess', 'BusinessApplication', 'SOP']
RETURN biz.externalId AS id, biz.name AS name, biz.node_type AS type
LIMIT 20
"""


class ImpactAnalyzerAgent(BaseAgent):
    name = "impact_analyzer"
    description = (
        "Analyzes the technical and business impact of a proposed change by "
        "traversing downstream relationships in the knowledge graph."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        entity_id = context.extra.get("entity_id", "")
        change_type = context.extra.get("change_type", "modify")

        if not entity_id:
            result.log("No entity_id provided — cannot analyze impact")
            return result.finish("partial")

        result.log(f"Analyzing impact of {change_type} on: {entity_id}")

        # Technical impact
        tech_impacts: list[dict] = []
        biz_impacts: list[dict] = []
        try:
            tech_impacts = [dict(r) for r in neo4j.run_query(_DOWNSTREAM_QUERY, {"entity_id": entity_id})]
            biz_impacts = [dict(r) for r in neo4j.run_query(_BUSINESS_IMPACT_QUERY, {"entity_id": entity_id})]
            result.log(f"Downstream: {len(tech_impacts)} tech, {len(biz_impacts)} business impacts")
        except Exception as exc:  # noqa: BLE001
            result.log(f"Graph traversal failed: {exc}")

        # LLM synthesis
        prompt = _build_prompt(entity_id, change_type, tech_impacts, biz_impacts, context.intent)
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
            impact_report = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM synthesis failed: {exc}")
            impact_report = {"risk_level": "unknown", "summary": str(exc), "recommendations": []}

        result.log(f"Impact risk level: {impact_report.get('risk_level', 'unknown')}")
        result.output = {
            "entity_id": entity_id,
            "change_type": change_type,
            "tech_impact_count": len(tech_impacts),
            "business_impact_count": len(biz_impacts),
            "tech_impacts": tech_impacts[:20],
            "business_impacts": biz_impacts,
            "risk_level": impact_report.get("risk_level", "unknown"),
            "summary": impact_report.get("summary", ""),
            "recommendations": impact_report.get("recommendations", []),
            "requires_human_review": impact_report.get("requires_human_review", False),
        }
        return result.finish("success")


def _build_prompt(
    entity_id: str, change_type: str,
    tech: list[dict], biz: list[dict], intent: str,
) -> str:
    return (
        "You are AURA's impact analyzer. Synthesize the impact of a change.\n\n"
        f"Entity: {entity_id}\nChange type: {change_type}\nIntent: {intent}\n"
        f"Technical downstream ({len(tech)}): {json.dumps(tech[:15])}\n"
        f"Business impacts ({len(biz)}): {json.dumps(biz[:10])}\n\n"
        "Return JSON: {risk_level: 'low'|'medium'|'high'|'critical', "
        "summary: string, recommendations: [string], requires_human_review: bool, "
        "blast_radius: {services: int, business_processes: int, customers: int}}"
    )
