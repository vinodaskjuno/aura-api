"""
Database Analyzer Agent — Stage 2 of the AURA understanding pipeline.
Introspects databases to extract tables, columns, relationships, stored
procedures, and data lineage. Adds Database, Table, and Column nodes.
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.config_settings import settings as s
from src.graph import neo4j_client as neo4j


class DatabaseAnalyzerAgent(BaseAgent):
    name = "database_analyzer"
    description = (
        "Introspects databases and ORM models to extract table schemas, column "
        "definitions, foreign keys, and stored procedure logic."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        version_id = context.extra.get("version_id")
        file_res = context.prior_results.get("file_analyzer")
        scanner = context.prior_results.get("project_scanner")
        file_ctx = file_res.output if file_res else {}
        project_ctx = scanner.output if scanner else {}

        result.log("Analyzing database schemas and models")
        prompt = _build_prompt(file_ctx, project_ctx, context.intent)

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
            schema = json.loads(raw["content"][0]["text"])
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM failed: {exc}")
            return result.finish("partial")

        nodes_added = 0
        rels_added = 0
        for db in schema.get("databases", []):
            neo4j.upsert_node_with_version(
                label="Database",
                external_id=db["id"],
                props={"name": db.get("name"), "dbType": db.get("type"), "host": db.get("host")},
                version_id=version_id,
            )
            result.kg_updates.append(Triple(db["id"], "IS_A", "Database"))
            nodes_added += 1

            for table in db.get("tables", []):
                table_id = f"{db['id']}.{table['name']}"
                neo4j.upsert_node_with_version(
                    label="Table",
                    external_id=table_id,
                    props={"name": table["name"], "rowEstimate": table.get("row_estimate")},
                    version_id=version_id,
                )
                neo4j.upsert_relationship(
                    source_external_id=db["id"],
                    target_external_id=table_id,
                    rel_type="CONTAINS",
                    provenance={"source": "database_analysis", "discoveredBy": self.name,
                                "confidence": 0.98, "factType": "known"},
                )
                nodes_added += 1
                rels_added += 1

        result.log(f"Database: {nodes_added} nodes, {rels_added} relationships")
        result.output = {"databases": [d.get("name") for d in schema.get("databases", [])],
                         "nodes_added": nodes_added, "rels_added": rels_added}
        return result.finish("success")


def _build_prompt(file_ctx: dict, project_ctx: dict, intent: str) -> str:
    return (
        "You are AURA's database analyzer. Extract database schemas, tables, and relationships.\n\n"
        f"Intent: {intent}\n"
        f"Project: {json.dumps(project_ctx)[:1000]}\n"
        f"Code files context: {json.dumps(file_ctx)[:1500]}\n\n"
        "Return JSON: {databases: [{id, name, type, host, tables: [{name, row_estimate, "
        "columns: [{name, type, nullable, pk, fk_to}]}]}]}\n"
        "type options: postgresql, mysql, mongodb, dynamodb, redis, elasticsearch, oracle, mssql"
    )
