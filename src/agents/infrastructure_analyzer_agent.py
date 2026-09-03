"""
Infrastructure Analyzer Agent — Stage 1 of the AURA understanding pipeline.
Discovers infrastructure resources from Terraform, Kubernetes manifests, AWS
CloudFormation, and CI/CD config. Adds Server, Container, CloudResource,
KubernetesCluster, and BuildPipeline nodes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j

_INFRA_PATTERNS = ["*.tf", "*.yaml", "*.yml", "Dockerfile*", "docker-compose*",
                   "Jenkinsfile", ".github/workflows/*", "*.json"]  # simplified


class InfrastructureAnalyzerAgent(BaseAgent):
    name = "infrastructure_analyzer"
    description = (
        "Scans IaC files (Terraform, K8s manifests, Dockerfiles, CI/CD) to "
        "discover infrastructure resources and their relationships."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")
        project_path = context.extra.get("project_path", ".")

        result.log(f"Scanning infrastructure files in {project_path}")
        infra_files = _collect_infra_files(project_path)
        result.log(f"Found {len(infra_files)} infrastructure-related files")

        if not infra_files:
            result.log("No infrastructure files found")
            return result.finish("partial")

        file_excerpts = {}
        for f in infra_files[:20]:
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    file_excerpts[str(f)] = fh.read(2000)
            except OSError:
                pass

        prompt = _build_prompt(file_excerpts, context.intent)
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
            infra = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for resource in infra.get("resources", []):
            neo4j.upsert_node_with_version(
                label=resource["label"],
                external_id=resource["id"],
                props=resource.get("props", {}),
                version_id=version_id,
            )
            result.kg_updates.append(Triple(resource["id"], "IS_A", resource["label"]))
            nodes_added += 1

        for rel in infra.get("relationships", []):
            neo4j.link_nodes_by_eid(
                from_eid=rel["source"],
                to_eid=rel["target"],
                rel_type=rel["type"],
                provenance_props={"source": "iac_analysis", "discoveredBy": self.name,
                            "confidence": rel.get("confidence", 0.9), "factType": "known"},
            )
            rels_added += 1

        result.log(f"Infrastructure: {nodes_added} resources, {rels_added} relationships")
        result.output = {
            "resource_types": list({r["label"] for r in infra.get("resources", [])}),
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


def _collect_infra_files(root: str) -> list[Path]:
    results = []
    skip = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fname in filenames:
            p = Path(dirpath) / fname
            s_lower = fname.lower()
            if (s_lower.endswith((".tf", ".tfvars")) or
                    "dockerfile" in s_lower or
                    "docker-compose" in s_lower or
                    "jenkinsfile" in s_lower or
                    ".github" in str(p) or
                    (s_lower.endswith((".yaml", ".yml")) and any(
                        k in str(p).lower() for k in ["k8s", "kubernetes", "helm", "deploy"]))):
                results.append(p)
    return results[:50]


def _build_prompt(file_excerpts: dict[str, str], intent: str) -> str:
    ctx = "\n\n".join(f"=== {k} ===\n{v}" for k, v in list(file_excerpts.items())[:10])
    return (
        "You are AURA's infrastructure analyzer. Extract infrastructure resources.\n\n"
        f"Intent: {intent}\n"
        f"Files:\n{ctx[:5000]}\n\n"
        "Return JSON: {resources: [{id, label, props: {name, region, env, provider}}], "
        "relationships: [{source, target, type, confidence}]}\n"
        "Labels: Server, VM, Container, KubernetesCluster, CloudResource, Network, "
        "BuildPipeline, DeploymentEnvironment.\n"
        "Rel types: RUNS_ON, DEPLOYED_TO, CONTAINS, CONNECTS_TO, BUILT_BY."
    )
