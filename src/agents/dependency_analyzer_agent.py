"""
Dependency Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Reads dependency manifests (requirements.txt, package.json, pom.xml, go.mod)
and identifies external libraries, CVE exposure, and transitive dependencies.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class DependencyAnalyzerAgent(BaseAgent):
    name = "dependency_analyzer"
    description = (
        "Parses dependency manifests to identify external libraries, their versions, "
        "CVE risk, and DEPENDS_ON relationships in the knowledge graph."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")
        scanner = context.prior_results.get("project_scanner")
        if not scanner:
            result.log("No project_scanner — cannot read dependency files")
            return result.finish("partial")

        dep_files = scanner.output.get("dependency_manifests", [])
        result.log(f"Reading {len(dep_files)} dependency manifests")

        raw_deps: dict[str, str] = {}
        for dep_file in dep_files:
            try:
                with open(dep_file, encoding="utf-8", errors="ignore") as f:
                    raw_deps[dep_file] = f.read(4000)
            except OSError:
                pass

        if not raw_deps:
            result.log("No readable dependency files found")
            return result.finish("partial")

        prompt = _build_prompt(raw_deps, context.intent)
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
            deps = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for dep in deps.get("dependencies", []):
            dep_id = f"dep:{dep['name']}:{dep.get('version', 'any')}"
            neo4j.upsert_node_with_version(
                label="Dependency",
                external_id=dep_id,
                props={
                    "name": dep["name"],
                    "version": dep.get("version"),
                    "ecosystem": dep.get("ecosystem", "unknown"),
                    "cveRisk": dep.get("cve_risk", "unknown"),
                    "isTransitive": dep.get("is_transitive", False),
                },
                version_id=version_id,
            )
            result.kg_updates.append(Triple(dep_id, "IS_A", "Dependency"))
            nodes_added += 1

            if dep.get("used_by"):
                neo4j.link_nodes_by_eid(
                    from_eid=dep["used_by"],
                    to_eid=dep_id,
                    rel_type="DEPENDS_ON",
                    provenance_props={"source": "dependency_manifest", "discoveredBy": self.name,
                                "confidence": 0.99, "factType": "known"},
                )
                rels_added += 1

        result.log(f"Dependencies: {nodes_added} packages analyzed")
        result.output = {
            "total_deps": nodes_added,
            "high_risk": [d.get("name") for d in deps.get("dependencies", []) if d.get("cve_risk") == "high"],
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


def _build_prompt(raw_deps: dict[str, str], intent: str) -> str:
    manifests = "\n\n".join(f"=== {k} ===\n{v}" for k, v in raw_deps.items())
    return (
        "You are AURA's dependency analyzer. Analyze these dependency manifests.\n\n"
        f"Intent: {intent}\n"
        f"Manifests:\n{manifests[:4000]}\n\n"
        "Return JSON: {dependencies: [{name, version, ecosystem, cve_risk, is_transitive, used_by}]}\n"
        "ecosystem: npm, pypi, maven, go, cargo, rubygems\n"
        "cve_risk: high, medium, low, unknown"
    )
