from __future__ import annotations
import json
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref, Triple


class ReverseEngineeringAgent(BaseAgent):
    name = "reverse_engineering_agent"
    description = "Reverse-engineer legacy code into structured knowledge graph with relationships and correlations"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("ReverseEngineeringAgent: Building knowledge graph from code analysis")

        project_id = context.project_id or "default"
        prior = context.prior_results.get("code_analysis_agent")

        analysis = prior.output if prior else {}
        tech_stack = analysis.get("tech_stack", [])
        services = analysis.get("services", [])
        languages = list(analysis.get("languages", {}).keys())

        result.log(f"Tech stack: {', '.join(tech_stack[:8])}")
        result.log(f"Languages: {', '.join(languages)}")

        # Build knowledge graph JSON
        knowledge_graph: dict = {
            "project_id": project_id,
            "code": {
                "languages": languages,
                "services": services,
                "tech_stack": tech_stack,
            },
            "infra": analysis.get("cloud_resources", []),
            "db_servers": analysis.get("db_references", []),
            "correlations": [],
            "relationships": [],
        }

        # Use Bedrock to enrich with semantic relationships
        # Guard: only call Bedrock if we have real services — prevents fake lang/tech correlations
        if not services:
            result.log("No services detected — skipping Bedrock correlation inference")
            result.log("Tip: verify local paths are correct and re-run analysis")
        else:
            try:
                import boto3
                from src.config_settings import get_settings
                s = get_settings()
                client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)

                infra_names = [i.get("name", i) if isinstance(i, dict) else i
                               for i in knowledge_graph.get("infra", [])][:8]
                db_names    = [d.get("name", d) if isinstance(d, dict) else d
                               for d in knowledge_graph.get("db_servers", [])][:6]

                prompt = f"""Infer architectural service relationships for this project.
IMPORTANT: Only use items from the Services list as 'from' values.
DO NOT use language names (python, typescript, yaml, mule, java) as from/to values.

Services: {', '.join(services[:20])}
Databases: {', '.join(db_names) or 'none'}
Infrastructure: {', '.join(infra_names) or 'none'}

Return JSON: {{"correlations": [{{"from": "ServiceName", "to": "OtherServiceOrDbOrInfraName", "relationship": "depends_on|calls|deployed_on|uses|contains"}}]}}"""

                body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                        "messages": [{"role": "user", "content": prompt}]}
                resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                           contentType="application/json", accept="application/json")
                text = json.loads(resp["body"].read())["content"][0]["text"]
                start = text.find("{")
                end   = text.rfind("}") + 1
                if start >= 0 and end > start:
                    enriched  = json.loads(text[start:end])
                    raw_corrs = enriched.get("correlations", [])
                    lang_set  = set(l.lower() for l in languages)
                    clean_corrs = [
                        c for c in raw_corrs
                        if c.get("from") and c.get("to")
                        and c.get("from") not in ("...", "")
                        and c.get("to")   not in ("...", "")
                        and c.get("from", "").lower() not in lang_set
                        and c.get("to", "").lower()   not in lang_set
                        and "|" not in c.get("relationship", "")
                        and len(c.get("relationship", "")) < 50
                    ]
                    knowledge_graph["correlations"] = clean_corrs
                    result.log(f"Inferred {len(clean_corrs)} relationships ({len(raw_corrs)-len(clean_corrs)} filtered)")
            except Exception as exc:
                result.log(f"Bedrock enrichment warning: {exc}")

        # Generate RDF triples for Neptune
        base = f"http://ontology.aura.com/project#{project_id}"
        for tech in tech_stack:
            result.kg_updates.append(Triple(
                subject=base,
                predicate="http://ontology.aura.com/core#usesTechnology",
                obj=f"http://ontology.aura.com/tech#{tech.replace(' ', '_')}",
            ))
        for corr in knowledge_graph.get("correlations", []):
            result.kg_updates.append(Triple(
                subject=f"http://ontology.aura.com/project#{corr.get('from','').replace(' ', '_')}",
                predicate=f"http://ontology.aura.com/core#{corr.get('relationship','relatedTo')}",
                obj=f"http://ontology.aura.com/project#{corr.get('to','').replace(' ', '_')}",
            ))

        result.log(f"Generated {len(result.kg_updates)} RDF triples for knowledge graph")

        # Persist to S3
        try:
            from src.storage.s3_client import put_json
            uri = put_json("analysis", f"{project_id}/knowledge_graph.json", knowledge_graph)
            result.artifacts.append(S3Ref(bucket="aura-analysis", key=f"{project_id}/knowledge_graph.json", uri=uri))
            result.log(f"Saved knowledge graph to {uri}")
        except Exception as exc:
            result.log(f"S3 save warning: {exc}")

        result.output = knowledge_graph
        return result.finish("success")
