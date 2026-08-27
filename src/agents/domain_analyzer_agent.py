"""
Domain Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Identifies business domains, processes, and concepts from code structure,
naming conventions, and linked business documents. Produces BusinessDomain,
BusinessProcess, and BusinessRule nodes in the knowledge graph.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class DomainAnalyzerAgent(BaseAgent):
    name = "domain_analyzer"
    description = (
        "Identifies business domains, processes, and rules from code and "
        "documentation, mapping them to the ontology as BusinessDomain, "
        "BusinessProcess, and BusinessRule nodes."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")

        # Collect context from upstream agents
        scanner = context.prior_results.get("project_scanner")
        knowledge = context.prior_results.get("knowledge_analyzer")
        code_context = scanner.output if scanner else {}
        doc_context = knowledge.output if knowledge else {}

        result.log("Identifying business domains and processes")
        prompt = _build_prompt(code_context, doc_context, context.intent)

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
            domains = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for node in domains.get("nodes", []):
            neo4j.upsert_node_with_version(
                label=node["label"],
                external_id=node["id"],
                props=node.get("props", {}),
                version_id=version_id,
            )
            result.kg_updates.append(Triple(node["id"], "IS_A", node["label"]))
            nodes_added += 1

        for rel in domains.get("relationships", []):
            neo4j.upsert_relationship(
                source_external_id=rel["source"],
                target_external_id=rel["target"],
                rel_type=rel["type"],
                provenance={
                    "source": "domain_analysis",
                    "discoveredBy": self.name,
                    "confidence": rel.get("confidence", 0.75),
                    "factType": "inferred",
                },
            )
            rels_added += 1

        result.log(f"Domains: {nodes_added} nodes, {rels_added} relationships")
        result.output = {
            "domains": domains.get("domain_names", []),
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


def _build_prompt(code_ctx: dict, doc_ctx: dict, intent: str) -> str:
    return (
        "You are AURA's domain analyzer. Identify business domains, processes, "
        "and rules from the following project and documentation context.\n\n"
        f"Intent: {intent}\n"
        f"Code context: {json.dumps(code_ctx)[:1500]}\n"
        f"Doc context: {json.dumps(doc_ctx)[:1500]}\n\n"
        "Return JSON: {domain_names: [string], nodes: [{id, label, props: {name, description}}], "
        "relationships: [{source, target, type, confidence}]}\n"
        "Labels: BusinessDomain, BusinessProcess, BusinessRule, Policy, SOP, Requirement.\n"
        "Rel types: CONTAINS, GOVERNS, IMPLEMENTS_BUSINESS_RULE, SUPPORTS_PROCESS."
    )
