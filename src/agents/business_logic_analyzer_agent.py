"""
Business Logic Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Extracts business rules, calculations, validations, decisions, and workflow
steps from source code. Links code functions to BusinessRule and BusinessProcess
nodes identified by domain_analyzer.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class BusinessLogicAnalyzerAgent(BaseAgent):
    name = "business_logic_analyzer"
    description = (
        "Extracts business rules, calculations, validations, and workflow steps "
        "from source code and links them to BusinessRule / BusinessProcess nodes."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")

        file_result = context.prior_results.get("file_analyzer")
        domain_result = context.prior_results.get("domain_analyzer")
        files_ctx = file_result.output if file_result else {}
        domain_ctx = domain_result.output if domain_result else {}

        result.log("Extracting business logic and rules from code")
        prompt = _build_prompt(files_ctx, domain_ctx, context.intent)

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
            logic = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for rule in logic.get("rules", []):
            neo4j.upsert_node_with_version(
                label="BusinessRule",
                external_id=rule["id"],
                props={
                    "name": rule.get("name"),
                    "description": rule.get("description"),
                    "ruleType": rule.get("rule_type", "validation"),
                    "confidence": rule.get("confidence", 0.8),
                },
                version_id=version_id,
            )
            result.kg_updates.append(Triple(rule["id"], "IS_A", "BusinessRule"))
            nodes_added += 1

            if rule.get("implemented_by"):
                neo4j.link_nodes_by_eid(
                    from_eid=rule["implemented_by"],
                    to_eid=rule["id"],
                    rel_type="IMPLEMENTS",
                    provenance_props={
                        "source": "business_logic_analysis",
                        "discoveredBy": self.name,
                        "confidence": rule.get("confidence", 0.8),
                        "factType": "inferred",
                        "evidence": rule.get("evidence", []),
                    },
                )
                rels_added += 1

        result.log(f"Business logic: {nodes_added} rules, {rels_added} links")
        result.output = {
            "rules_found": nodes_added,
            "rules": [r.get("name") for r in logic.get("rules", [])],
            "nodes_added": nodes_added,
            "rels_added": rels_added,
        }
        return result.finish("success")


def _build_prompt(files_ctx: dict, domain_ctx: dict, intent: str) -> str:
    return (
        "You are AURA's business logic analyzer. Find business rules, validations, "
        "calculations, and decisions hidden in the code.\n\n"
        f"Intent: {intent}\n"
        f"Files context: {json.dumps(files_ctx)[:1500]}\n"
        f"Domains: {json.dumps(domain_ctx)[:1000]}\n\n"
        "Return JSON: {rules: [{id, name, description, rule_type, confidence, "
        "implemented_by (function_id), evidence: [file:line]}]}\n"
        "rule_type options: calculation, validation, decision, workflow, constraint"
    )
