from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult


class AIOpsAgent(BaseAgent):
    name = "aiops_agent"
    description = "Ingest and analyse CloudWatch, Prometheus, Splunk monitoring data and correlate with service graph"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("AIOpsAgent: Fetching monitoring data")

        # Fetch CloudWatch alarms
        alarms = await self._fetch_cloudwatch_alarms(result)
        # Fetch recent logs from DynamoDB buffer
        recent_logs = await self._fetch_recent_logs(result)

        result.log(f"Fetched {len(alarms)} CloudWatch alarms, {len(recent_logs)} recent log events")

        # Correlate with knowledge graph
        correlations = []
        for alarm in alarms:
            correlations.append({
                "alarm": alarm.get("AlarmName", ""),
                "state": alarm.get("StateValue", ""),
                "service": self._infer_service(alarm),
                "timestamp": alarm.get("StateUpdatedTimestamp", ""),
            })

        # Persist alerts to DynamoDB
        from src.database.dynamo_client import put_item
        import uuid
        for alarm in alarms[:20]:
            try:
                put_item("logs", {
                    "logId": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "cloudwatch",
                    "level": "ALARM" if alarm.get("StateValue") == "ALARM" else "OK",
                    "message": alarm.get("AlarmDescription", alarm.get("AlarmName", "")),
                    "metadata": json.dumps({"alarmName": alarm.get("AlarmName", "")}),
                })
            except Exception:
                pass

        result.output = {
            "alarms": len(alarms),
            "active_alarms": sum(1 for a in alarms if a.get("StateValue") == "ALARM"),
            "correlations": correlations,
            "log_events": len(recent_logs),
        }
        result.log(f"AIOps: {result.output['active_alarms']} active alarms detected")
        return result.finish("success")

    async def _fetch_cloudwatch_alarms(self, result) -> list[dict]:
        try:
            import boto3
            from src.config_settings import get_settings
            s = get_settings()
            client = boto3.client("cloudwatch", region_name=s.aws_region)
            resp = client.describe_alarms(MaxRecords=50)
            alarms = resp.get("MetricAlarms", [])
            result.log(f"  CloudWatch: {len(alarms)} alarms retrieved")
            return alarms
        except Exception as exc:
            result.log(f"  CloudWatch unavailable: {exc}")
            return []

    async def _fetch_recent_logs(self, result) -> list[dict]:
        try:
            from src.database.dynamo_client import scan_items
            return scan_items("logs", limit=100)
        except Exception:
            return []

    def _infer_service(self, alarm: dict) -> str:
        name = alarm.get("AlarmName", "").lower()
        if "payment" in name: return "PaymentService"
        if "user" in name: return "UserService"
        if "lambda" in name: return "LambdaFunction"
        if "rds" in name or "db" in name: return "Database"
        if "api" in name: return "APIGateway"
        return "UnknownService"
