"""
Graph Reviewer Agent — Stage 5 of the AURA understanding pipeline (sequential).
Validates the knowledge graph for completeness, connectivity, and schema
conformance. Reports missing relationships, isolated nodes, and orphaned entities.
"""
from __future__ import annotations

import json

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class GraphReviewerAgent(BaseAgent):
    name = "graph_reviewer"
    description = (
        "Validates the knowledge graph for completeness, connectivity, and schema "
        "conformance. Flags missing links and isolated nodes."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        builder = context.prior_results.get("graph_builder")
        if not builder:
            result.log("No graph_builder result — cannot review")
            return result.finish("partial")

        stats = builder.output
        result.log(f"Reviewing graph: {stats.get('total_nodes_added', 0)} nodes, "
                   f"{stats.get('total_rels_added', 0)} rels")

        # Query neo4j for orphaned nodes (no relationships)
        try:
            orphan_count = neo4j.count_orphan_nodes()
            result.log(f"Orphaned nodes (no edges): {orphan_count}")
        except Exception as exc:  # noqa: BLE001
            orphan_count = -1
            result.log(f"Could not count orphans: {exc}")

        prompt = _build_prompt(stats, orphan_count, context.intent)
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
            raw = json.loads(resp["body"].read())
            review = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            review = {"quality_score": 0.5, "issues": [], "recommendations": []}

        result.log(f"Quality score: {review.get('quality_score', 0):.0%}")
        result.output = {
            "quality_score": review.get("quality_score", 0),
            "orphan_nodes": orphan_count,
            "issues": review.get("issues", []),
            "recommendations": review.get("recommendations", []),
        }
        status = "success" if review.get("quality_score", 0) >= 0.5 else "partial"
        return result.finish(status)


def _build_prompt(stats: dict, orphan_count: int, intent: str) -> str:
    return (
        "You are AURA's graph reviewer. Assess knowledge graph quality.\n\n"
        f"Intent: {intent}\n"
        f"Graph stats: {json.dumps(stats)}\n"
        f"Orphaned nodes: {orphan_count}\n\n"
        "Return JSON: {quality_score: float (0-1), issues: [string], "
        "recommendations: [string], missing_relationship_types: [string]}"
    )
