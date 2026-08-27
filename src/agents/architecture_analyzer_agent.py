"""
Architecture Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Identifies architectural components, layers, service boundaries, and design
patterns from project structure and code. Adds Application, Service, Module,
and Layer nodes to the knowledge graph.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class ArchitectureAnalyzerAgent(BaseAgent):
    name = "architecture_analyzer"
    description = (
        "Identifies architectural layers, service boundaries, design patterns, "
        "and component relationships from the codebase structure."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        scanner = context.prior_results.get("project_scanner")
        if not scanner:
            result.log("No project_scanner result — cannot analyze architecture")
            return result.finish("partial")

        manifest = scanner.output
        version_id = context.extra.get("version_id")
        result.log("Analyzing architectural structure")

        prompt = _build_prompt(manifest, context.intent)
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
            arch = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for component in arch.get("components", []):
            neo4j.upsert_node_with_version(
                label=component.get("label", "Application"),
                external_id=component["id"],
                props=component.get("props", {}),
                version_id=version_id,
            )
            result.kg_updates.append(Triple(component["id"], "IS_A", component.get("label", "Application")))
            nodes_added += 1

        for rel in arch.get("relationships", []):
            neo4j.upsert_relationship(
                source_external_id=rel["source"],
                target_external_id=rel["target"],
                rel_type=rel["type"],
                provenance={
                    "source": "architecture_analysis",
                    "discoveredBy": self.name,
                    "confidence": rel.get("confidence", 0.80),
                    "factType": "inferred",
                },
            )
            rels_added += 1

        result.log(f"Architecture: {nodes_added} components, {rels_added} relationships")
        result.output = {
            "layers": arch.get("layers", []),
            "patterns": arch.get("patterns", []),
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


def _build_prompt(manifest: dict[str, Any], intent: str) -> str:
    return (
        "You are AURA's architecture analyzer. Analyze this project manifest and "
        "identify architectural components, layers, patterns, and relationships.\n\n"
        f"Intent: {intent}\n"
        f"Manifest: {json.dumps(manifest, indent=2)[:3000]}\n\n"
        "Return JSON: {components: [{id, label, props: {name, layer, pattern}}], "
        "relationships: [{source, target, type, confidence}], "
        "layers: [string], patterns: [string]}\n"
        "Labels: Application, Service, Module, Repository.\n"
        "Rel types: DEPENDS_ON, CALLS, CONTAINS, IMPLEMENTS."
    )
