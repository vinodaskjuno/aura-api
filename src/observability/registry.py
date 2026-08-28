"""Provider registry — lazy dotted-path resolution.

Deliberately NOT the agent_registry `bootstrap()` pattern (hand-written imports at
startup) and deliberately not auto-import scanning: lazy resolution means an
uninstalled optional SDK breaks exactly one provider at query time instead of
crashing app startup.

Adding a provider is three declarative touch points:
  1. the module under src/observability/providers/
  2. one line here
  3. one string in routers/connectors.py::PROVIDER_MAP (so the UI offers it)
"""
from __future__ import annotations

import importlib
import logging
from typing import Type

from src.observability.base import ObservabilityProvider, ProviderNotConfigured

log = logging.getLogger(__name__)

_PROVIDER_TYPES: dict[str, str] = {
    "loki":          "src.observability.providers.loki:LokiProvider",
    "mimir":         "src.observability.providers.mimir:MimirProvider",
    "tempo":         "src.observability.providers.tempo:TempoProvider",
    "grafana_loki":  "src.observability.providers.loki:LokiProvider",
    "grafana_mimir": "src.observability.providers.mimir:MimirProvider",
    "grafana_tempo": "src.observability.providers.tempo:TempoProvider",
    "datadog":       "src.observability.providers.datadog:DatadogProvider",
    "sentry":        "src.observability.providers.sentry:SentryProvider",
    "elasticsearch": "src.observability.providers.elasticsearch:ElasticsearchProvider",
    "cloudwatch":    "src.observability.providers.cloudwatch:CloudWatchProvider",
    "kubernetes":    "src.observability.providers.kubernetes_obs:KubernetesProvider",
    "pagerduty":     "src.observability.providers.pagerduty_obs:PagerDutyProvider",
}

# Test/eval override slot — the harness registers FixtureProvider under real
# provider ids so agents need zero changes (seam S4).
_OVERRIDES: dict[str, ObservabilityProvider] = {}


def known_provider_types() -> list[str]:
    return sorted(set(_PROVIDER_TYPES.keys()))


def register_override(provider_id: str, provider: ObservabilityProvider) -> None:
    _OVERRIDES[provider_id] = provider


def clear_overrides() -> None:
    _OVERRIDES.clear()


def _resolve(provider_type: str) -> Type[ObservabilityProvider]:
    path = _PROVIDER_TYPES.get(provider_type)
    if not path:
        raise ProviderNotConfigured(f"unknown provider type '{provider_type}'")
    module_path, _, cls_name = path.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # optional SDK missing — fail this provider only
        raise ProviderNotConfigured(
            f"provider '{provider_type}' unavailable: {exc}") from exc
    return getattr(module, cls_name)


def get_provider(provider_type: str, config: dict,
                 provider_id: str = "", label: str = "") -> ObservabilityProvider:
    pid = provider_id or provider_type
    if pid in _OVERRIDES:
        return _OVERRIDES[pid]
    cls = _resolve(provider_type)
    return cls(config, provider_id=pid, label=label)


def resolve_providers(user_id: str, project_id: str | None = None,
                      provider_ids: list[str] | None = None,
                      ) -> list[ObservabilityProvider]:
    """Every configured provider for this user/project, instantiated.

    Never raises: a misconfigured provider is skipped with a warning so one bad
    Datadog token cannot take down an entire investigation.
    """
    from src.observability.credentials import list_provider_configs

    out: list[ObservabilityProvider] = []
    for cfg in list_provider_configs(user_id, project_id):
        pid = cfg["provider_id"]
        if provider_ids and pid not in provider_ids:
            continue
        try:
            out.append(get_provider(cfg["provider_type"], cfg["config"],
                                    provider_id=pid, label=cfg.get("label", "")))
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping provider %s (%s): %s", pid, cfg["provider_type"], exc)
    for pid, prov in _OVERRIDES.items():
        if provider_ids and pid not in provider_ids:
            continue
        if not any(p.provider_id == pid for p in out):
            out.append(prov)
    return out
