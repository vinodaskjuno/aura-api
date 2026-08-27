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

            result.entities_added = len(pods) + len(svcs) + len(deploys)
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
