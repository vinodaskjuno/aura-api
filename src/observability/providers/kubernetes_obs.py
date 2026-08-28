"""Kubernetes — cluster events, pod state, and container logs.

Uses the Kubernetes API, never a `kubectl` subprocess. `self_healing_agent.py` shells
out to kubectl from a FastAPI worker; that cannot work in ECS (no binary, no
kubeconfig) and is not a pattern to copy.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timezone

from src.observability.base import ObservabilityProvider, ProviderNotConfigured
from src.observability.types import (
    EventQuery, EventRecord, LogPage, LogQuery, LogRecord, ProviderHealth, Signal,
)

log = logging.getLogger(__name__)


class KubernetesProvider(ObservabilityProvider):
    provider_type = "kubernetes"
    display_name = "Kubernetes"
    capabilities: frozenset[Signal] = frozenset({"logs", "events"})

    def _load(self):
        try:
            from kubernetes import client, config as k8s_config
        except ImportError as exc:  # optional dep — fails this provider only
            raise ProviderNotConfigured(
                "kubernetes client library is not installed", provider_id=self.provider_id
            ) from exc

        api_server = self._cfg("api_server", "apiServer")
        token = self._cfg("token", "service_account_token", "serviceAccountToken")
        if api_server and token:
            cfg = client.Configuration()
            cfg.host = api_server
            cfg.api_key = {"authorization": f"Bearer {token}"}
            ca = self._cfg("ca_cert", "caCert")
            cfg.verify_ssl = bool(ca)
            if ca:
                cfg.ssl_ca_cert = ca
            return client.ApiClient(cfg)

        kube_path = self._cfg("kubeconfig_path", "kubeconfigPath")
        context = self._cfg("context") or None
        if kube_path:
            k8s_config.load_kube_config(config_file=kube_path, context=context)
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:  # noqa: BLE001
                k8s_config.load_kube_config(context=context)
        return client.ApiClient()

    @property
    def namespace(self) -> str:
        return self._cfg("namespace")

    async def health(self) -> ProviderHealth:
        import time
        t0 = time.monotonic()

        def _probe():
            from kubernetes import client
            v1 = client.CoreV1Api(self._load())
            return len(v1.list_namespace(limit=1).items)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _probe)
            return ProviderHealth(self.provider_id, self.provider_type, "connected",
                                  latency_ms=int((time.monotonic() - t0) * 1000))
        except ProviderNotConfigured as exc:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.provider_id, self.provider_type, "failed",
                                  latency_ms=int((time.monotonic() - t0) * 1000),
                                  message=str(exc)[:200])

    # ── Events (the high-value signal: OOMKilled, CrashLoopBackOff, scaling) ──

    def _fetch_events(self, q: EventQuery) -> list[EventRecord]:
        from kubernetes import client
        api = self._load()
        v1 = client.CoreV1Api(api)
        ns = self.namespace
        evs = (v1.list_namespaced_event(ns).items if ns
               else v1.list_event_for_all_namespaces().items)

        out: list[EventRecord] = []
        for e in evs:
            ts = e.last_timestamp or e.event_time or e.first_timestamp
            ts_iso = ts.astimezone(timezone.utc).isoformat() if ts else ""
            if ts_iso and not q.window.contains(ts_iso):
                continue
            obj = e.involved_object
            name = getattr(obj, "name", "") or ""
            if q.service and q.service.lower() not in name.lower():
                continue
            reason = e.reason or ""
            kind = ("scale" if reason in ("ScalingReplicaSet", "SuccessfulRescale")
                    else "deploy" if reason in ("ScalingReplicaSet", "Created")
                    else "k8s_event")
            out.append(EventRecord.make(
                self.provider_id, self.provider_type, kind, ts_iso,
                q.service or name, f"{reason}: {name}",
                description=(e.message or "")[:500],
                labels={"reason": reason, "namespace": getattr(obj, "namespace", "") or "",
                        "kind": getattr(obj, "kind", "") or "",
                        "type": e.type or "", "count": str(e.count or 1)},
            ))
        return out

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        try:
            return await asyncio.get_event_loop().run_in_executor(None, self._fetch_events, q)
        except Exception as exc:  # noqa: BLE001
            log.debug("kubernetes events failed: %s", exc)
            return []

    # ── Logs (container logs for matching pods) ──────────────────────────────

    def _fetch_logs(self, q: LogQuery) -> list[LogRecord]:
        from kubernetes import client
        api = self._load()
        v1 = client.CoreV1Api(api)
        ns = self.namespace
        pods = (v1.list_namespaced_pod(ns).items if ns
                else v1.list_pod_for_all_namespaces().items)
        matching = [p for p in pods
                    if not q.service or q.service.lower() in p.metadata.name.lower()][:5]

        out: list[LogRecord] = []
        since = max(60, q.window.duration_s())
        for pod in matching:
            try:
                text = v1.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=pod.metadata.namespace,
                    since_seconds=since, tail_lines=min(q.limit, 200), timestamps=True)
            except Exception:  # noqa: BLE001 — a single unreadable pod must not fail the query
                continue
            for line in (text or "").splitlines():
                ts, _, body = line.partition(" ")
                if q.filter and q.filter.lower() not in body.lower():
                    continue
                level = _sniff(body)
                if q.levels and level not in q.levels and level != "UNKNOWN":
                    continue
                out.append(LogRecord.make(
                    self.provider_id, self.provider_type, ts, level,
                    q.service or pod.metadata.name, body,
                    labels={"pod": pod.metadata.name,
                            "namespace": pod.metadata.namespace,
                            "node": pod.spec.node_name or ""},
                ))
        return out

    async def query_logs(self, q: LogQuery) -> LogPage:
        import time
        t0 = time.monotonic()
        try:
            records = await asyncio.get_event_loop().run_in_executor(None, self._fetch_logs, q)
        except Exception as exc:  # noqa: BLE001
            return LogPage(error=str(exc)[:300],
                           query_ms=int((time.monotonic() - t0) * 1000))
        return LogPage(records=records[: q.limit], total_estimate=len(records),
                       query_ms=int((time.monotonic() - t0) * 1000))

    def _fetch_pod_health(self) -> list[dict]:
        from kubernetes import client
        v1 = client.CoreV1Api(self._load())
        ns = self.namespace
        pods = (v1.list_namespaced_pod(ns).items if ns
                else v1.list_pod_for_all_namespaces().items)
        unhealthy = []
        for p in pods:
            for cs in (p.status.container_statuses or []):
                waiting = getattr(cs.state, "waiting", None)
                terminated = getattr(cs.state, "terminated", None)
                reason = (getattr(waiting, "reason", None)
                          or getattr(terminated, "reason", None) or "")
                if reason in ("CrashLoopBackOff", "OOMKilled", "ImagePullBackOff",
                              "ErrImagePull", "CreateContainerConfigError"):
                    unhealthy.append({
                        "pod": p.metadata.name, "namespace": p.metadata.namespace,
                        "container": cs.name, "reason": reason,
                        "restarts": cs.restart_count,
                        "node": p.spec.node_name or "",
                    })
        return unhealthy

    async def pod_health(self) -> list[dict]:
        try:
            return await asyncio.get_event_loop().run_in_executor(None, self._fetch_pod_health)
        except Exception as exc:  # noqa: BLE001
            log.debug("kubernetes pod health failed: %s", exc)
            return []

    async def list_services(self) -> list[str]:
        def _fetch():
            from kubernetes import client
            v1 = client.CoreV1Api(self._load())
            ns = self.namespace
            svcs = (v1.list_namespaced_service(ns).items if ns
                    else v1.list_service_for_all_namespaces().items)
            return [s.metadata.name for s in svcs]
        try:
            return await asyncio.get_event_loop().run_in_executor(None, _fetch)
        except Exception:  # noqa: BLE001
            return []


def _sniff(body: str) -> str:
    upper = (body or "")[:200].upper()
    for lvl in ("FATAL", "ERROR", "WARN", "INFO", "DEBUG"):
        if lvl in upper:
            return lvl
    return "UNKNOWN"
