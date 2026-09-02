from __future__ import annotations
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
            # Routed through the shared LLM seam rather than an inline
            # boto3.client("bedrock-runtime"). That seam is where cost metering,
            # Opik span emission and the test override all live — this agent used to
            # bypass all three, so its spend was invisible and the suite could bill
            # a real call.
            from src.observability.llm import invoke_masked, parse_json_block

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

                text, record = await invoke_masked(
                    system="You are a site-reliability engineer performing root cause analysis.",
                    user=prompt,
                    # Not an investigation, so masking stays off exactly as it was
                    # before this call was moved; the slot scopes cost and the Opik
                    # thread instead. Passing a real investigation id here would
                    # switch masking on — deliberate future change, not a silent one.
                    investigation_id="",
                    agent=self.name, max_tokens=2048,
                    user_id=context.user_id,
                    **self.llm_kwargs(context))
                result.opik_trace_id = record.opik_trace_id or result.opik_trace_id
                if record.error:
                    result.log(f"  RCA for {service} unavailable: {record.error[:80]}")
                    continue
                data = parse_json_block(text)
                if isinstance(data, dict):
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
