"""
Article Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Reads individual documents fetched by knowledge_analyzer and extracts
named entities, business claims, and relationships between them.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class ArticleAnalyzerAgent(BaseAgent):
    name = "article_analyzer"
    description = (
        "Reads individual documents and articles to extract named entities, "
        "business claims, and relationships between document nodes."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")
        knowledge = context.prior_results.get("knowledge_analyzer")
        if not knowledge:
            result.log("No knowledge_analyzer result — skipping")
            return result.finish("partial")

        topics = knowledge.output.get("topics", [])
        docs_processed = knowledge.output.get("docs_processed", 0)
        result.log(f"Analyzing articles covering {len(topics)} topics from {docs_processed} docs")

        prompt = _build_prompt(knowledge.output, context.intent)
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
            analysis = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for entity in analysis.get("entities", []):
            neo4j.upsert_node_with_version(
                label=entity["label"],
                external_id=entity["id"],
                props=entity.get("props", {}),
                version_id=version_id,
            )
            result.kg_updates.append(Triple(entity["id"], "IS_A", entity["label"]))
            nodes_added += 1

        for rel in analysis.get("relationships", []):
            neo4j.upsert_relationship(
                source_external_id=rel["source"],
                target_external_id=rel["target"],
                rel_type=rel["type"],
                provenance={"source": "article_analysis", "discoveredBy": self.name,
                            "confidence": rel.get("confidence", 0.75), "factType": "inferred",
                            "evidence": rel.get("evidence", [])},
            )
            rels_added += 1

        result.log(f"Articles: {nodes_added} entities, {rels_added} relationships")
        result.output = {
            "entities_found": nodes_added,
            "claims": analysis.get("claims", []),
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


def _build_prompt(knowledge_ctx: dict, intent: str) -> str:
    return (
        "You are AURA's article analyzer. Extract named entities and relationships "
        "from the knowledge base context.\n\n"
        f"Intent: {intent}\n"
        f"Knowledge context: {json.dumps(knowledge_ctx)[:3000]}\n\n"
        "Return JSON: {entities: [{id, label, props: {name, description, confidence}}], "
        "relationships: [{source, target, type, confidence, evidence: [string]}], "
        "claims: [string]}\n"
        "Labels: BusinessDomain, BusinessProcess, BusinessRule, Team, User, Policy, Requirement.\n"
        "Rel types: OWNS, GOVERNS, REQUIRES, REFERENCES, SUPERSEDES, IMPLEMENTS."
    )
