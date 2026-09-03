"""
File Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Reads source files discovered by project_scanner and extracts code entities
(Function, Class, Module, CodeFile) with relationships (CALLS, IMPORTS,
IMPLEMENTS, EXTENDS) to populate the Neo4j ontology.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j
from src.services.ontology_version_service import create_version_record, finish_version_record


class FileAnalyzerAgent(BaseAgent):
    name = "file_analyzer"
    description = (
        "Parses source files to extract code entities (Function, Class, Module) "
        "and their relationships, writing them to the Neo4j knowledge graph."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        scanner = context.prior_results.get("project_scanner")
        if not scanner:
            result.log("No project_scanner result — skipping")
            return result.finish("partial")

        files: list[str] = scanner.output.get("file_sample", [])
        version_id = context.extra.get("version_id")
        result.log(f"Analyzing {len(files)} files")

        nodes_added = 0
        rels_added = 0
        for filepath in files[:200]:
            try:
                entities = await _analyze_file(filepath, context)
                for entity in entities.get("nodes", []):
                    neo4j.upsert_node_with_version(
                        label=entity["label"],
                        external_id=entity["id"],
                        props=entity.get("props", {}),
                        version_id=version_id,
                    )
                    result.kg_updates.append(
                        Triple(entity["id"], "IS_A", entity["label"])
                    )
                    nodes_added += 1
                for rel in entities.get("relationships", []):
                    neo4j.link_nodes_by_eid(
                        from_eid=rel["source"],
                        to_eid=rel["target"],
                        rel_type=rel["type"],
                        provenance_props={
                            "source": "file_analysis",
                            "discoveredBy": self.name,
                            "confidence": rel.get("confidence", 0.85),
                            "factType": "inferred",
                            "evidence": [filepath],
                        },
                    )
                    rels_added += 1
            except Exception as exc:  # noqa: BLE001
                result.log(f"Failed to analyze {filepath}: {exc}")

        result.log(f"Added {nodes_added} nodes, {rels_added} relationships")
        result.output = {"nodes_added": nodes_added, "rels_added": rels_added}
        return result.finish("success")


async def _analyze_file(filepath: str, context: AgentContext) -> dict[str, Any]:
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            source = f.read(8000)
    except OSError:
        return {}

    prompt = (
        "Extract code entities from this file. Return JSON: "
        "{nodes: [{id, label, props: {name, language, filePath, confidence}}], "
        "relationships: [{source, target, type, confidence}]}. "
        "Labels: Function, Class, Module, CodeFile, API, Dependency. "
        "Rel types: CALLS, IMPORTS, IMPLEMENTS, EXTENDS.\n\n"
        f"File: {filepath}\n```\n{source}\n```"
    )
    client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    })
    resp = client.invoke_model(
        modelId=s.bedrock_model_id,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    raw = json.loads(resp["body"].read())
    return json.loads(raw["content"][0]["text"])
