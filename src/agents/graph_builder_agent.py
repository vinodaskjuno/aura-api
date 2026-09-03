"""
Graph Builder Agent — Stage 4 of the AURA understanding pipeline (sequential).
Receives the merged outputs from all Stage 2 + 3 agents, normalizes entity IDs,
resolves duplicates, and writes the final consolidated knowledge graph to Neo4j.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j
from src.services.ontology_version_service import finish_version_record


_STAGE_AGENTS = [
    "file_analyzer", "architecture_analyzer", "domain_analyzer",
    "business_logic_analyzer", "data_flow_analyzer", "api_analyzer",
    "database_analyzer", "dependency_analyzer", "infrastructure_analyzer",
    "knowledge_analyzer", "article_analyzer",
]


class GraphBuilderAgent(BaseAgent):
    name = "graph_builder"
    description = (
        "Consolidates outputs from all analysis agents, resolves duplicate entities, "
        "and writes the final normalized knowledge graph to Neo4j."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")

        # Gather stats from all prior agents
        total_nodes = 0
        total_rels = 0
        agent_summaries: list[dict] = []
        all_kg_updates: list[Triple] = []

        for agent_name in _STAGE_AGENTS:
            prior = context.prior_results.get(agent_name)
            if prior and prior.status != "failed":
                n = prior.output.get("nodes_added", 0)
                r = prior.output.get("rels_added", 0)
                total_nodes += n
                total_rels += r
                all_kg_updates.extend(prior.kg_updates)
                agent_summaries.append({"agent": agent_name, "nodes": n, "rels": r,
                                        "status": prior.status})
                result.log(f"{agent_name}: {n} nodes, {r} rels")

        result.log(f"Total from all agents: {total_nodes} nodes, {total_rels} rels")

        # Resolve entity conflicts via LLM if there are cross-agent duplicates
        prompt = _dedup_prompt(agent_summaries, context.intent)
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
            dedup = json.loads(raw["content"][0]["text"])
            merges = dedup.get("merges", [])
            result.log(f"Deduplication: {len(merges)} entity merges identified")

            for merge in merges:
                if merge.get("keep") and merge.get("remove"):
                    # Re-point all edges from `remove` to `keep`
                    neo4j.link_nodes_by_eid(
                        from_eid=merge["keep"],
                        to_eid=merge["remove"],
                        rel_type="SAME_AS",
                        provenance_props={"source": "deduplication", "discoveredBy": self.name,
                                    "confidence": merge.get("confidence", 0.9), "factType": "inferred"},
                    )
        except Exception as exc:  # noqa: BLE001
            result.log(f"Dedup LLM failed (non-fatal): {exc}")

        # Finalize the version record
        if version_id:
            try:
                finish_version_record(
                    version_id=version_id,
                    status="success",
                    stats={"nodesAdded": total_nodes, "relsAdded": total_rels,
                           "totalNodes": neo4j.count_nodes()},
                    diff_summary={s["agent"]: s["nodes"] for s in agent_summaries},
                )
                result.log(f"Version record {version_id} finalized")
            except Exception as exc:  # noqa: BLE001
                result.log(f"Failed to finalize version record: {exc}")

        result.kg_updates = all_kg_updates
        result.output = {
            "total_nodes_added": total_nodes,
            "total_rels_added": total_rels,
            "agent_summaries": agent_summaries,
            "version_id": version_id,
        }
        return result.finish("success")


def _dedup_prompt(summaries: list[dict], intent: str) -> str:
    return (
        "You are AURA's graph builder. Multiple agents have added nodes to Neo4j. "
        "Identify obvious duplicates that should be merged.\n\n"
        f"Intent: {intent}\n"
        f"Agent summaries: {json.dumps(summaries)}\n\n"
        "Return JSON: {merges: [{keep: id, remove: id, confidence: float, reason: string}]}\n"
        "Only include high-confidence merges (>0.85). Empty array is fine."
    )
