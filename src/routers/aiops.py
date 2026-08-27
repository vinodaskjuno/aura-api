"""AIOps API — live monitoring, alerts, RCA, WebSocket feed."""
from __future__ import annotations
import asyncio
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from src.routers.auth import get_current_user, require_permission
from src.database.dynamo_client import scan_items, put_item, get_item, update_item
from src.storage.s3_client import get_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aiops", tags=["aiops"])


# ── Models ────────────────────────────────────────────────────────────────────

class AlertRecord(BaseModel):
    alertId: str
    source: str
    severity: str        # critical | high | medium | low | ok
    service: str
    message: str
    timestamp: str
    state: str           # ALARM | OK | INSUFFICIENT_DATA
    rootCause: str = ""
    rcaReportUri: str = ""


class AcknowledgeRequest(BaseModel):
    alertId: str
    note: str = ""


# ── Live data helpers ─────────────────────────────────────────────────────────

def _fetch_cloudwatch_alarms() -> list[dict]:
    """Pull real CloudWatch alarms. Returns empty list gracefully if unavailable."""
    try:
        import boto3
        from src.config_settings import get_settings
        s = get_settings()
        client = boto3.client("cloudwatch", region_name=s.aws_region)
        resp = client.describe_alarms(MaxRecords=50)
        alarms = []
        for a in resp.get("MetricAlarms", []):
            severity = "critical" if a.get("StateValue") == "ALARM" else "ok"
            alarms.append({
                "alertId": a.get("AlarmArn", str(uuid.uuid4())),
                "source": "cloudwatch",
                "severity": severity,
                "service": _infer_service(a.get("AlarmName", "")),
                "message": a.get("AlarmDescription") or a.get("AlarmName", ""),
                "timestamp": a.get("StateUpdatedTimestamp", datetime.now(timezone.utc)).isoformat()
                             if not isinstance(a.get("StateUpdatedTimestamp"), str)
                             else a.get("StateUpdatedTimestamp"),
                "state": a.get("StateValue", "INSUFFICIENT_DATA"),
                "namespace": a.get("Namespace", ""),
                "metricName": a.get("MetricName", ""),
            })
        return alarms
    except Exception as exc:
        logger.debug("CloudWatch unavailable: %s", exc)
        return []


def _infer_service(alarm_name: str) -> str:
    name = alarm_name.lower()
    for keyword, svc in [("payment", "PaymentService"), ("user", "UserService"),
                          ("order", "OrderService"), ("lambda", "LambdaFunction"),
                          ("rds", "RDSDatabase"), ("dynamo", "DynamoDB"),
                          ("api", "APIGateway"), ("k8s", "Kubernetes"),
                          ("ecs", "ECSCluster"), ("s3", "S3Storage")]:
        if keyword in name:
            return svc
    return alarm_name.split("-")[0].title() if alarm_name else "UnknownService"


def _get_stored_alerts(limit: int = 100) -> list[dict]:
    """Pull alerts from DynamoDB logs table."""
    try:
        all_logs = scan_items("logs", limit=500)
        alerts = [l for l in all_logs if l.get("source") in ("cloudwatch", "prometheus", "manual")]
        alerts.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return alerts[:limit]
    except Exception:
        return []


def _get_agent_runs() -> list[dict]:
    """Recent orchestrator / agent runs for pipeline status."""
    try:
        return scan_items("agents", limit=50)
    except Exception:
        return []


# ── REST Endpoints ────────────────────────────────────────────────────────────

@router.get("/alerts")
def get_alerts(user: dict = Depends(require_permission("aiops"))):
    """Return combined live CloudWatch + stored DynamoDB alerts."""
    live = _fetch_cloudwatch_alarms()
    stored = _get_stored_alerts(limit=50)
    # Merge: live alerts take priority, then stored
    seen_ids = {a["alertId"] for a in live}
    merged = live + [s for s in stored if s.get("logId") not in seen_ids]
    merged.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return merged[:80]


@router.get("/kpis")
def get_kpis(user: dict = Depends(require_permission("aiops"))):
    """Return live KPI counts."""
    live = _fetch_cloudwatch_alarms()
    active_alarms = [a for a in live if a.get("state") == "ALARM"]
    stored = _get_stored_alerts(limit=200)
    pipelines = _get_agent_runs()
    return {
        "activeAlarms": len(active_alarms),
        "totalAlarms": len(live),
        "storedEvents": len(stored),
        "agentRuns": len(pipelines),
        "liveConnected": len(live) > 0,
        "sources": ["cloudwatch", "dynamodb"],
    }


@router.get("/pipelines")
def get_pipelines(user: dict = Depends(require_permission("aiops"))):
    """Return recent agent pipeline runs."""
    runs = _get_agent_runs()
    return [
        {
            "runId": r.get("agentRunId", ""),
            "intent": r.get("intent", ""),
            "agents": r.get("agentsRun", []),
            "status": r.get("status", "unknown"),
            "completedAt": r.get("completedAt", ""),
        }
        for r in runs[:20]
    ]


@router.get("/rca/{project_id}")
def get_rca_reports(project_id: str, user: dict = Depends(require_permission("aiops"))):
    """List RCA reports for a project from S3."""
    try:
        from src.storage.s3_client import list_objects, presigned_url
        objects = list_objects("exports", prefix=f"rca/{project_id}/")
        return [
            {
                "key": o["key"],
                "filename": o["key"].split("/")[-1],
                "size": o["size"],
                "lastModified": o["last_modified"],
                "url": presigned_url("exports", o["key"]),
            }
            for o in objects
        ]
    except Exception:
        return []


@router.post("/rca/trigger")
async def trigger_rca(user: dict = Depends(require_permission("aiops"))):
    """Trigger AIOpsAgent + RCAAgent orchestration."""
    from src.agents.base_agent import AgentContext
    from src.agents.aiops_agent import AIOpsAgent
    from src.agents.rca_agent import RCAAgent
    from src.agents.knowledge_graph_agent import KnowledgeGraphAgent

    session_id = str(uuid.uuid4())
    context = AgentContext(
        user_id=user["userId"], username=user["username"], role=user["role"],
        intent="Perform AIOps root cause analysis",
        session_id=session_id,
    )
    results = {}
    for AgentClass in [AIOpsAgent, RCAAgent, KnowledgeGraphAgent]:
        agent = AgentClass()
        try:
            res = await agent.run(context)
            results[agent.name] = {"status": res.status, "log": res.activity_log[-3:]}
            context.prior_results[agent.name] = res
        except Exception as exc:
            results[agent.name] = {"status": "failed", "error": str(exc)}
    return {"session_id": session_id, "results": results}


@router.post("/alerts/acknowledge")
def acknowledge_alert(req: AcknowledgeRequest, user: dict = Depends(require_permission("aiops"))):
    """Mark an alert as acknowledged in DynamoDB."""
    try:
        put_item("logs", {
            "logId": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ack",
            "level": "INFO",
            "message": f"Alert {req.alertId} acknowledged by {user['username']}: {req.note}",
            "alertId": req.alertId,
            "acknowledgedBy": user["username"],
        })
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── WebSocket: live alert feed ─────────────────────────────────────────────────

@router.websocket("/ws/live")
async def ws_live_alerts(ws: WebSocket):
    """
    Streams live alert data every 15s.
    Client sends: {"token": "...", "interval": 15}
    Server sends: {"type": "alerts", "data": [...]} | {"type": "kpis", ...} | heartbeat
    """
    await ws.accept()
    try:
        data = await ws.receive_json()
        from src.services.auth_service import verify_token
        user = verify_token(data.get("token", ""))
        if not user:
            await ws.send_json({"type": "error", "message": "Unauthorized"})
            return

        interval = max(10, min(60, int(data.get("interval", 15))))
        await ws.send_json({"type": "connected", "message": f"Live feed started — polling every {interval}s"})

        tick = 0
        while True:
            # Send alerts
            live = _fetch_cloudwatch_alarms()
            stored = _get_stored_alerts(limit=30)
            all_alerts = live + stored
            all_alerts.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

            await ws.send_json({
                "type": "alerts",
                "data": all_alerts[:40],
                "tick": tick,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            # Send KPIs every 3rd tick
            if tick % 3 == 0:
                pipelines = _get_agent_runs()
                await ws.send_json({
                    "type": "kpis",
                    "activeAlarms": len([a for a in live if a.get("state") == "ALARM"]),
                    "totalAlerts": len(all_alerts),
                    "agentRuns": len(pipelines),
                    "liveConnected": len(live) > 0,
                    "tick": tick,
                })

            tick += 1
            await asyncio.sleep(interval)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("ws_live_alerts error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
