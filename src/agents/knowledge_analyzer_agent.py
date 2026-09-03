"""
Knowledge Analyzer Agent — Stage 1 of the AURA understanding pipeline.
Fetches and indexes documents from Confluence, SharePoint, wikis, SOPs,
and policies. Produces Document, WikiArticle, SOP, and Policy nodes.
"""
from __future__ import annotations

import json
from typing import Any

import boto3
import httpx

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class KnowledgeAnalyzerAgent(BaseAgent):
    name = "knowledge_analyzer"
    description = (
        "Fetches documents from Confluence, SharePoint, and wikis to extract "
        "policies, SOPs, business rules, and domain knowledge into the graph."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")
        sources: list[dict] = context.extra.get("knowledge_sources", [])

        if not sources:
            result.log("No knowledge sources configured — using intent only")
            sources = [{"type": "intent", "content": context.intent}]

        result.log(f"Processing {len(sources)} knowledge source(s)")
        all_docs: list[dict] = []

        for src in sources:
            src_type = src.get("type", "unknown")
            if src_type in ("confluence", "sharepoint", "wiki", "url"):
                docs = await _fetch_docs(src)
                all_docs.extend(docs)
                result.log(f"Fetched {len(docs)} docs from {src_type}")
            elif src_type == "intent":
                all_docs.append({"title": "intent", "content": src.get("content", ""), "url": ""})

        if not all_docs:
            result.log("No documents retrieved")
            return result.finish("partial")

        prompt = _build_prompt(all_docs, context.intent)
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
            knowledge = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for doc_node in knowledge.get("nodes", []):
            neo4j.upsert_node_with_version(
                label=doc_node["label"],
                external_id=doc_node["id"],
                props=doc_node.get("props", {}),
                version_id=version_id,
            )
            result.kg_updates.append(Triple(doc_node["id"], "IS_A", doc_node["label"]))
            nodes_added += 1

        for rel in knowledge.get("relationships", []):
            neo4j.link_nodes_by_eid(
                from_eid=rel["source"],
                to_eid=rel["target"],
                rel_type=rel["type"],
                provenance_props={"source": rel.get("source_system", "wiki"),
                            "discoveredBy": self.name, "confidence": rel.get("confidence", 0.8),
                            "factType": "known"},
            )
            rels_added += 1

        result.log(f"Knowledge: {nodes_added} nodes, {rels_added} relationships")
        result.output = {
            "docs_processed": len(all_docs),
            "topics": knowledge.get("topics", []),
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


async def _fetch_docs(source: dict) -> list[dict]:
    url = source.get("url", "")
    if not url:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {}
            if source.get("token"):
                headers["Authorization"] = f"Bearer {source['token']}"
            resp = await client.get(url, headers=headers)
            if resp.is_success:
                data = resp.json()
                if isinstance(data, list):
                    return [{"title": d.get("title", ""), "content": str(d)[:2000], "url": url}
                            for d in data[:10]]
                return [{"title": "doc", "content": str(data)[:2000], "url": url}]
    except Exception:  # noqa: BLE001
        pass
    return []


def _build_prompt(docs: list[dict], intent: str) -> str:
    doc_text = "\n\n".join(f"=== {d['title']} ===\n{d['content'][:1000]}" for d in docs[:5])
    return (
        "You are AURA's knowledge analyzer. Extract business knowledge from these documents.\n\n"
        f"Intent: {intent}\n"
        f"Documents:\n{doc_text[:5000]}\n\n"
        "Return JSON: {topics: [string], nodes: [{id, label, props: {name, description, url}}], "
        "relationships: [{source, target, type, confidence, source_system}]}\n"
        "Labels: Document, WikiArticle, SOP, Policy, ADR, TechnicalSpec, Runbook.\n"
        "Rel types: GOVERNS, DESCRIBES, REFERENCES, SUPERSEDES, IMPLEMENTS."
    )
