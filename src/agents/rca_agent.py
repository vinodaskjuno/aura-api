from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref


class RCAAgent(BaseAgent):
    name = "rca_agent"
    description = "Perform AI-powered root cause analysis correlating alerts, logs, source code and dependencies"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("RCAAgent: Starting root cause analysis")

        # Gather context from AIOps
        aiops_prior = context.prior_results.get("aiops_agent")
        alarms = aiops_prior.output.get("correlations", []) if aiops_prior else []
        active_alarms = [a for a in alarms if a.get("state") == "ALARM"]

        if not active_alarms:
            result.log("No active alarms to analyse")
            return result.finish("partial")

        result.log(f"Analysing {len(active_alarms)} active alarms")

        rca_reports = []
        try:
            import boto3
            from src.config_settings import get_settings
            s = get_settings()
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)

            for alarm in active_alarms[:3]:  # top 3
                # Fetch recent logs for this service from DynamoDB
                service = alarm.get("service", "UnknownService")
                prompt = f"""Perform root cause analysis for this incident.

Alarm: {alarm.get('alarm', '')}
Service: {service}
State: {alarm.get('state', '')}
Timestamp: {alarm.get('timestamp', '')}
Project context: {context.intent}

Analyse likely root causes and provide:
1. Primary root cause
2. Contributing factors
3. Affected downstream services
4. Immediate remediation steps
5. Long-term fix recommendations

Return JSON: {{"rca": {{"alarm": "...", "service": "...", "root_cause": "...", "contributing_factors": [], "affected_services": [], "immediate_actions": [], "long_term_fix": "...", "confidence": 0.0}}}}"""

                body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                        "messages": [{"role": "user", "content": prompt}]}
                resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                           contentType="application/json", accept="application/json")
                text = json.loads(resp["body"].read())["content"][0]["text"]
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    data = json.loads(text[start:end])
                    rca = data.get("rca", {})
                    rca_reports.append(rca)
                    result.log(f"  RCA for {service}: {rca.get('root_cause', '')[:80]}")
        except Exception as exc:
            result.log(f"Bedrock RCA warning: {exc}")
            rca_reports = [{"alarm": a.get("alarm"), "service": a.get("service"),
                            "root_cause": "Analysis pending — Bedrock unavailable",
                            "confidence": 0.0} for a in active_alarms[:3]]

        # Save RCA reports to S3
        project_id = context.project_id or "default"
        report_id = str(uuid.uuid4())[:8]
        report = {"report_id": report_id, "project_id": project_id,
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "rca_reports": rca_reports}
        try:
            from src.storage.s3_client import put_json
            uri = put_json("exports", f"rca/{project_id}/{report_id}.json", report)
            result.artifacts.append(S3Ref(bucket="aura-exports", key=f"rca/{project_id}/{report_id}.json", uri=uri))
            result.log(f"RCA report saved: {uri}")
        except Exception as exc:
            result.log(f"S3 save warning: {exc}")

        result.output = report
        result.log(f"Generated {len(rca_reports)} RCA reports")
        return result.finish("success")
