"""Credential resolution for observability providers.

Precedence, most specific first:
  1. `user-connectors` row scoped to this project
  2. `user-connectors` row with no project (user-level default)
  3. `config_settings.Settings` — the org-wide fallback for the platform's own stack

Nothing found -> the provider is simply absent from the list, and the collector
records it as `status: "unconfigured"` rather than failing the investigation.
"""
from __future__ import annotations

import logging

from src.config_settings import get_settings
from src.database.dynamo_client import query_items

log = logging.getLogger(__name__)

OBSERVABILITY_TYPES = {"observability", "incident"}


def _from_connectors(user_id: str, project_id: str | None) -> list[dict]:
    try:
        rows = query_items("user-connectors", "userId", user_id, limit=200)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read user-connectors: %s", exc)
        return []

    scoped: list[dict] = []
    unscoped: list[dict] = []
    for r in rows:
        if r.get("type") not in OBSERVABILITY_TYPES:
            continue
        provider = r.get("provider") or ""
        if not provider:
            continue
        entry = {
            "provider_id": r.get("connectorId", provider),
            "provider_type": provider,
            "label": r.get("name") or provider,
            "config": dict(r.get("config") or {}),
            "connector_id": r.get("connectorId", ""),
            "source": "connector",
        }
        row_project = r.get("projectId") or ""
        if project_id and row_project == project_id:
            scoped.append(entry)
        elif not row_project:
            unscoped.append(entry)

    # Project-scoped rows shadow unscoped ones of the same provider type.
    seen = {e["provider_type"] for e in scoped}
    return scoped + [e for e in unscoped if e["provider_type"] not in seen]


def _from_settings() -> list[dict]:
    """Org-wide fallbacks from .env. Only emitted when actually configured."""
    s = get_settings()
    out: list[dict] = []

    def add(ptype: str, label: str, cfg: dict, required: str) -> None:
        if cfg.get(required):
            out.append({"provider_id": f"env-{ptype}", "provider_type": ptype,
                        "label": label, "config": cfg, "connector_id": "",
                        "source": "settings"})

    grafana_common = {"api_token": s.grafana_api_token, "org_id": s.grafana_org_id,
                      "grafana_url": s.grafana_url}
    add("loki", "Grafana Loki (env)", {**grafana_common, "base_url": s.loki_url}, "base_url")
    add("mimir", "Grafana Mimir (env)", {**grafana_common, "base_url": s.mimir_url}, "base_url")
    add("tempo", "Grafana Tempo (env)", {**grafana_common, "base_url": s.tempo_url}, "base_url")
    add("datadog", "Datadog (env)",
        {"api_key": s.datadog_api_key, "app_key": s.datadog_app_key, "site": s.datadog_site},
        "api_key")
    add("sentry", "Sentry (env)",
        {"auth_token": s.sentry_auth_token, "org": s.sentry_org, "base_url": s.sentry_base_url},
        "auth_token")
    add("elasticsearch", "Elasticsearch (env)",
        {"base_url": s.elasticsearch_url, "api_key": s.elasticsearch_api_key,
         "index_pattern": s.elasticsearch_index_pattern},
        "base_url")
    add("pagerduty", "PagerDuty (env)", {"api_token": s.pagerduty_api_token}, "api_token")
    add("kubernetes", "Kubernetes (env)",
        {"api_server": s.kubernetes_api_server, "token": s.kubernetes_token,
         "namespace": s.kubernetes_namespace},
        "api_server")

    # CloudWatch needs no explicit credentials — boto3 resolves the ambient role.
    out.append({"provider_id": "env-cloudwatch", "provider_type": "cloudwatch",
                "label": "AWS CloudWatch", "config": {"region": s.aws_region},
                "connector_id": "", "source": "settings"})
    return out


def list_provider_configs(user_id: str, project_id: str | None = None) -> list[dict]:
    """All provider configs visible to this user, most specific first."""
    configs = _from_connectors(user_id, project_id)
    have = {c["provider_type"] for c in configs}
    configs += [c for c in _from_settings() if c["provider_type"] not in have]
    return configs


def resolve_config(provider_type: str, user_id: str,
                   project_id: str | None = None) -> dict | None:
    for cfg in list_provider_configs(user_id, project_id):
        if cfg["provider_type"] == provider_type:
            return cfg["config"]
    return None
