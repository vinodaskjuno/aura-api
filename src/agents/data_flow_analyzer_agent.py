"""
Data Flow Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Traces how data moves through the system: source → transformation → destination.
Produces DataFlow, DataElement, and FLOWS_TO / TRANSFORMS relationships.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class DataFlowAnalyzerAgent(BaseAgent):
    name = "data_flow_analyzer"
    description = (
        "Traces data flow through the system: source → transformation → destination. "
        "Adds DataFlow and DataElement nodes with FLOWS_TO, TRANSFORMS, READS_FROM relationships."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")

        db_result = context.prior_results.get("database_analyzer")
        file_result = context.prior_results.get("file_analyzer")
        db_ctx = db_result.output if db_result else {}
        file_ctx = file_result.output if file_result else {}

        result.log("Tracing data flows through the system")
        prompt = _build_prompt(db_ctx, file_ctx, context.intent)

        try:
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 3000,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = client.invoke_model(
                modelId=s.bedrock_model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            raw = json.loads(resp["body"].read())
            flows = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for flow in flows.get("flows", []):
            neo4j.upsert_node_with_version(
                label="DataFlow",
                external_id=flow["id"],
                props={
                    "name": flow.get("name"),
                    "description": flow.get("description"),
                    "sensitivity": flow.get("sensitivity", "internal"),
                },
                version_id=version_id,
            )
            result.kg_updates.append(Triple(flow["id"], "IS_A", "DataFlow"))
            nodes_added += 1

            for step in flow.get("steps", []):
                if step.get("source") and step.get("target"):
                    neo4j.link_nodes_by_eid(
                        from_eid=step["source"],
                        to_eid=step["target"],
                        rel_type=step.get("type", "FLOWS_TO"),
                        provenance_props={
                            "source": "data_flow_analysis",
                            "discoveredBy": self.name,
                            "confidence": step.get("confidence", 0.75),
                            "factType": "inferred",
                        },
                    )
                    rels_added += 1

        result.log(f"Data flows: {nodes_added} flows, {rels_added} steps")
        result.output = {"flows_found": nodes_added, "nodes_added": nodes_added, "rels_added": rels_added}
        return result.finish("success")


def _build_prompt(db_ctx: dict, file_ctx: dict, intent: str) -> str:
    return (
        "You are AURA's data flow analyzer. Trace how data moves through this system.\n\n"
        f"Intent: {intent}\n"
        f"Database context: {json.dumps(db_ctx)[:1500]}\n"
        f"Code context: {json.dumps(file_ctx)[:1500]}\n\n"
        "Return JSON: {flows: [{id, name, description, sensitivity, "
        "steps: [{source, target, type, confidence}]}]}\n"
        "Rel types: FLOWS_TO, TRANSFORMS, READS_FROM, WRITES_TO, PUBLISHES_TO, CONSUMES_FROM"
    )
