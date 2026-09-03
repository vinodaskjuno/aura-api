"""Kubernetes connector — discovers pods, services, deployments, and namespaces."""
from __future__ import annotations
from typing import Any
from ..base import AbstractConnector, SyncResult


class KubernetesConnector(AbstractConnector):
    """
    Config keys:
      kubeconfig_path: str | None   defaults to ~/.kube/config
      context: str | None           defaults to current context
      namespace: str | None         None = all namespaces
    """

    pipeline = "api"

    def test_connection(self) -> tuple[bool, str]:
        try:
            from kubernetes import client, config as k8s_config
            self._load_config()
            v1 = client.CoreV1Api()
            ns_list = v1.list_namespace(limit=1)
            return True, f"Connected — {len(ns_list.items)} namespace(s) visible"
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        try:
            from kubernetes import client
            self._load_config()
            v1 = client.CoreV1Api()
            apps_v1 = client.AppsV1Api()
            ns = self.config.get("namespace")

            pods = v1.list_pod_for_all_namespaces().items if not ns else \
                   v1.list_namespaced_pod(ns).items
            svcs = v1.list_service_for_all_namespaces().items if not ns else \
                   v1.list_namespaced_service(ns).items
            deploys = apps_v1.list_deployment_for_all_namespaces().items if not ns else \
                      apps_v1.list_namespaced_deployment(ns).items

            from src.graph import neo4j_client as graph

            cluster = self.config.get("cluster_name") or self.config.get("context") or "default"
            cluster_eid = f"k8s:cluster:{cluster}"
            graph.upsert_node("KubernetesCluster", cluster_eid,
                              {"name": cluster, "source": "kubernetes"})

            for svc in svcs:
                eid = f"k8s:svc:{svc.metadata.namespace}/{svc.metadata.name}"
                graph.upsert_node("Service", eid, {
                    "name": svc.metadata.name, "namespace": svc.metadata.namespace,
                    "clusterName": cluster, "source": "kubernetes",
                    "type": getattr(svc.spec, "type", "") or "",
                })
                graph.upsert_relationship(
                    "KubernetesCluster", cluster_eid, "Service", eid, "HOSTS",
                    provenance_props={"source": "kubernetes", "discoveredBy": "kubernetes_connector",
                                "confidence": 1.0, "factType": "known"})
                result.entities_added += 1

            for dep in deploys:
                eid = f"k8s:deploy:{dep.metadata.namespace}/{dep.metadata.name}"
                graph.upsert_node("Deployment", eid, {
                    "name": dep.metadata.name, "namespace": dep.metadata.namespace,
                    "clusterName": cluster, "source": "kubernetes",
                    "replicas": getattr(dep.spec, "replicas", 0) or 0,
                })
                result.entities_added += 1

            for pod in pods:
                eid = f"k8s:pod:{pod.metadata.namespace}/{pod.metadata.name}"
                graph.upsert_node("Container", eid, {
                    "name": pod.metadata.name, "namespace": pod.metadata.namespace,
                    "clusterName": cluster, "nodeName": pod.spec.node_name or "",
                    "phase": pod.status.phase or "", "source": "kubernetes",
                })
                result.entities_added += 1
        except Exception as exc:
            result.errors.append(str(exc))
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        try:
            from kubernetes import client
            self._load_config()
            v1 = client.CoreV1Api()
            ns_list = v1.list_namespace()
            return [{"name": ns.metadata.name} for ns in ns_list.items[:5]]
        except Exception:
            return []

    def _load_config(self) -> None:
        from kubernetes import config as k8s_config
        kube_path = self.config.get("kubeconfig_path")
        context = self.config.get("context")
        if kube_path:
            k8s_config.load_kube_config(config_file=kube_path, context=context)
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config(context=context)
