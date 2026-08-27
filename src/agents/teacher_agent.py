"""
Teacher Agent — Stage 6 of the AURA understanding pipeline.
Responds to user questions about the knowledge graph using the graph as
grounding context. Powers the /teach command and the ontology maintainer chat.
"""
from __future__ import annotations

import json

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class TeacherAgent(BaseAgent):
    name = "teacher_agent"
    description = (
        "Answers user questions about the enterprise knowledge graph. Uses the "
        "graph as grounding context to provide accurate, sourced explanations."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        question = context.intent
        result.log(f"Teaching: {question[:80]}")

        # Retrieve relevant graph context
        try:
            search_query = """
            MATCH (n)
            WHERE toLower(n.name) CONTAINS toLower($term)
               OR toLower(n.description) CONTAINS toLower($term)
            RETURN n.externalId AS id, n.name AS name, n.node_type AS type,
                   n.description AS description
            LIMIT 10
            """
            term = question.split()[0] if question.split() else question
            graph_ctx = [dict(r) for r in neo4j.run_query(search_query, {"term": term})]
            result.log(f"Retrieved {len(graph_ctx)} relevant graph nodes")
        except Exception as exc:  # noqa: BLE001
            result.log(f"Graph query failed: {exc}")
            graph_ctx = []

        # Include tour builder context if available
        tours_ctx = {}
        tour_result = context.prior_results.get("tour_builder")
        if tour_result:
            tours_ctx = {"tours_available": tour_result.output.get("personas", [])}

        prompt = _build_prompt(question, graph_ctx, tours_ctx)
        try:
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = client.invoke_model(
                modelId=s.bedrock_model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            raw = json.loads(resp["body"].read())
            answer = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            answer = {"explanation": "Unable to generate explanation.", "sources": [], "confidence": 0}

        result.log(f"Answer confidence: {answer.get('confidence', 0):.0%}")
        result.output = {
            "question": question,
            "explanation": answer.get("explanation", ""),
            "sources": answer.get("sources", []),
            "related_nodes": answer.get("related_nodes", []),
            "confidence": answer.get("confidence", 0),
            "suggested_next_questions": answer.get("suggested_next_questions", []),
        }
        return result.finish("success")


def _build_prompt(question: str, graph_ctx: list[dict], tours_ctx: dict) -> str:
    return (
        "You are AURA's teacher. Answer the user's question using the knowledge graph "
        "as grounding. Be specific, cite node IDs and relationship types as evidence.\n\n"
        f"Question: {question}\n"
        f"Relevant graph nodes: {json.dumps(graph_ctx)}\n"
        f"Available tours: {json.dumps(tours_ctx)}\n\n"
        "Return JSON: {explanation: string, sources: [{node_id, label, relevance}], "
        "related_nodes: [node_id], confidence: float, "
        "suggested_next_questions: [string]}"
    )
