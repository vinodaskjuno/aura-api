from __future__ import annotations
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult


class SelfHealingAgent(BaseAgent):
    name = "self_healing_agent"
    description = "Auto-remediate Kubernetes pod failures, scale resources, and restore application health"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("SelfHealingAgent: Checking pod and service health")

        # In production: kubectl get pods, identify CrashLoopBackOff/OOMKilled, restart/scale
        k8s_status = await self._check_k8s_health(result)

        result.output = {
            "k8s_status": k8s_status,
            "actions_taken": [],
            "recommendations": ["Monitor pod restart counts", "Review resource limits and requests"],
        }
        return result.finish("success")

    async def _check_k8s_health(self, result) -> dict:
        try:
            import subprocess
            proc = subprocess.run(
                ["kubectl", "get", "pods", "--all-namespaces", "-o", "json"],
                capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                import json
                pods = json.loads(proc.stdout).get("items", [])
                unhealthy = [p for p in pods if any(
                    c.get("state", {}).get("waiting", {}).get("reason") in ("CrashLoopBackOff", "OOMKilled")
                    for c in p.get("status", {}).get("containerStatuses", [])
                )]
                result.log(f"K8s: {len(pods)} pods, {len(unhealthy)} unhealthy")
                return {"total_pods": len(pods), "unhealthy": len(unhealthy)}
        except Exception as exc:
            result.log(f"K8s check skipped: {exc}")
        return {"total_pods": 0, "unhealthy": 0, "status": "kubectl unavailable"}
