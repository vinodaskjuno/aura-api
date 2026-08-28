"""
Remediation Agent — on-demand agent triggered by incidents or security findings.
Traces the root cause through the knowledge graph and recommends (or generates)
remediation steps, PRs, or runbook entries.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


_INCIDENT_CONTEXT_QUERY = """
MATCH (incident)-[:HAS_FINDING|AFFECTS|RELATED_TO*1..3]-(context)
WHERE incident.externalId = $incident_id
RETURN context.externalId AS id, context.name AS name,
       context.node_type AS type, context.description AS description
LIMIT 30
"""


class RemediationAgent(BaseAgent):
    name = "remediation_agent"
    description = (
        "Traces incidents and security findings through the knowledge graph to "
        "identify root causes and generate remediation steps or PR suggestions."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        incident_id = context.extra.get("incident_id", "")
        finding_id = context.extra.get("finding_id", "")
        target_id = incident_id or finding_id

        if not target_id:
            result.log("No incident_id or finding_id provided")
            return result.finish("partial")

        result.log(f"Remediating: {target_id}")

        # Get related context from graph
        related_ctx: list[dict] = []
        try:
            related_ctx = [dict(r) for r in neo4j.run_query(
                _INCIDENT_CONTEXT_QUERY, {"incident_id": target_id}
            )]
            result.log(f"Graph context: {len(related_ctx)} related entities")
        except Exception as exc:  # noqa: BLE001
            result.log(f"Graph query failed: {exc}")

        # Get RCA result if available — either the legacy AIOps rca_agent or the
        # observability investigation pipeline's obs_root_cause.
        rca = (context.prior_results.get("rca_agent")
               or context.prior_results.get("obs_root_cause"))
        rca_ctx = rca.output if rca else {}

        prompt = _build_prompt(target_id, related_ctx, rca_ctx, context.intent)
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
            remediation = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            remediation = {"steps": [], "pr_suggestions": [], "runbook_updates": []}

        result.log(f"Generated {len(remediation.get('steps', []))} remediation steps")
        result.output = {
            "incident_id": target_id,
            "root_cause": remediation.get("root_cause", "Unknown"),
            "steps": remediation.get("steps", []),
            "pr_suggestions": remediation.get("pr_suggestions", []),
            "runbook_updates": remediation.get("runbook_updates", []),
            "estimated_resolution_minutes": remediation.get("estimated_resolution_minutes", 0),
            "severity": remediation.get("severity", "medium"),
        }
        return result.finish("success")


def _build_prompt(
    incident_id: str,
    related_ctx: list[dict],
    rca_ctx: dict,
    intent: str,
) -> str:
    return (
        "You are AURA's remediation agent. Generate actionable remediation steps.\n\n"
        f"Incident/Finding: {incident_id}\nIntent: {intent}\n"
        f"Root cause analysis: {json.dumps(rca_ctx)[:1000]}\n"
        f"Related entities: {json.dumps(related_ctx[:15])}\n\n"
        "Return JSON: {root_cause: string, severity: 'low'|'medium'|'high'|'critical', "
        "steps: [{step: int, action: string, owner: string, estimated_minutes: int}], "
        "pr_suggestions: [{file: string, change: string, reason: string}], "
        "runbook_updates: [{section: string, new_entry: string}], "
        "estimated_resolution_minutes: int}"
    )
