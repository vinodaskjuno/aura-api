"""
API Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Discovers API endpoints from OpenAPI specs, FastAPI route decorators, Express
handlers, etc. Adds API nodes and EXPOSES / CALLS relationships.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class ApiAnalyzerAgent(BaseAgent):
    name = "api_analyzer"
    description = (
        "Extracts API endpoints, contracts, and dependencies from OpenAPI specs "
        "and code. Adds API nodes with method, path, and auth information."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")
        scanner = context.prior_results.get("project_scanner")
        file_res = context.prior_results.get("file_analyzer")
        project_ctx = scanner.output if scanner else {}
        file_ctx = file_res.output if file_res else {}

        result.log("Discovering API endpoints and contracts")
        prompt = _build_prompt(project_ctx, file_ctx, context.intent)

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
            apis = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for endpoint in apis.get("endpoints", []):
            neo4j.upsert_node_with_version(
                label="API",
                external_id=endpoint["id"],
                props={
                    "name": endpoint.get("name"),
                    "method": endpoint.get("method"),
                    "path": endpoint.get("path"),
                    "authType": endpoint.get("auth_type"),
                    "description": endpoint.get("description"),
                    "confidence": endpoint.get("confidence", 0.9),
                },
                version_id=version_id,
            )
            result.kg_updates.append(Triple(endpoint["id"], "IS_A", "API"))
            nodes_added += 1

            if endpoint.get("service_id"):
                neo4j.upsert_relationship(
                    source_external_id=endpoint["service_id"],
                    target_external_id=endpoint["id"],
                    rel_type="EXPOSES",
                    provenance={
                        "source": "api_analysis",
                        "discoveredBy": self.name,
                        "confidence": 0.95,
                        "factType": "known",
                    },
                )
                rels_added += 1

        result.log(f"APIs: {nodes_added} endpoints, {rels_added} relationships")
        result.output = {"endpoints_found": nodes_added, "nodes_added": nodes_added, "rels_added": rels_added}
        return result.finish("success")


def _build_prompt(project_ctx: dict, file_ctx: dict, intent: str) -> str:
    return (
        "You are AURA's API analyzer. Discover all API endpoints in this project.\n\n"
        f"Intent: {intent}\n"
        f"Project: {json.dumps(project_ctx)[:1500]}\n"
        f"Files: {json.dumps(file_ctx)[:1000]}\n\n"
        "Return JSON: {endpoints: [{id, name, method, path, auth_type, description, "
        "service_id, confidence}]}\n"
        "auth_type options: bearer, api_key, basic, none, oauth2"
    )
