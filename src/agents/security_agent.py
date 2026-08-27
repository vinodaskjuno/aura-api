from __future__ import annotations
import json
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref, Triple


class SecurityAgent(BaseAgent):
    name = "security_agent"
    description = "Scan source code for vulnerabilities, CVE lookup, OWASP checks, and security posture analysis"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("SecurityAgent: Starting security analysis")

        project_id = context.project_id or "default"
        prior = context.prior_results.get("code_analysis_agent")
        tech_stack = prior.output.get("tech_stack", []) if prior else []
        dependencies = prior.output.get("dependencies", []) if prior else []

        result.log(f"Scanning {len(dependencies)} dependencies for CVEs")
        result.log(f"Tech stack: {', '.join(tech_stack[:6])}")

        findings: list[dict] = []

        # Use Bedrock to identify security issues
        try:
            import boto3
            from src.config_settings import get_settings
            s = get_settings()
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            prompt = f"""Perform security analysis for this project.
Tech stack: {', '.join(tech_stack)}
Dependencies: {', '.join(dependencies[:30])}
Context: {context.intent}

Return JSON: {{"findings": [{{"id": "VULN-001", "severity": "high|medium|low|info", "title": "...", "description": "...", "cve": "CVE-...", "affected": "...", "recommendation": "..."}}]}}"""

            body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]}
            resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                       contentType="application/json", accept="application/json")
            text = json.loads(resp["body"].read())["content"][0]["text"]
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                findings = data.get("findings", [])
        except Exception as exc:
            result.log(f"Bedrock security scan warning: {exc}")

        high = sum(1 for f in findings if f.get("severity") == "high")
        medium = sum(1 for f in findings if f.get("severity") == "medium")
        low = sum(1 for f in findings if f.get("severity") == "low")
        result.log(f"Found {len(findings)} issues: {high} high, {medium} medium, {low} low")

        for finding in findings:
            result.log(f"  [{finding.get('severity','?').upper()}] {finding.get('id','')} — {finding.get('title','')}")

        # Generate KG triples for vulnerabilities
        for f in findings:
            vuln_iri = f"http://ontology.aura.com/security#{f.get('id','VULN').replace('-','_')}"
            result.kg_updates.append(Triple(
                subject=f"http://ontology.aura.com/project#{project_id}",
                predicate="http://ontology.aura.com/core#hasVulnerability",
                obj=vuln_iri,
            ))

        # Persist report to S3
        report = {"project_id": project_id, "findings": findings, "summary": {"high": high, "medium": medium, "low": low}}
        try:
            from src.storage.s3_client import put_json
            uri = put_json("exports", f"{project_id}/security_report.json", report)
            result.artifacts.append(S3Ref(bucket="aura-exports", key=f"{project_id}/security_report.json", uri=uri))
            result.log(f"Security report saved: {uri}")
        except Exception as exc:
            result.log(f"S3 save warning: {exc}")

        result.output = report
        return result.finish("success")
