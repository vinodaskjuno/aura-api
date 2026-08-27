"""OpenAPI / Swagger parser — extracts API endpoints from spec files."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult


class OpenApiConnector(AbstractConnector):
    """
    Config keys:
      spec_url: str | None    URL to openapi.json / swagger.yaml
      spec_path: str | None   local file path
      service_name: str       name for the parent Service node
    """

    def test_connection(self) -> tuple[bool, str]:
        try:
            spec = self._load_spec()
            return True, f"Spec loaded: {spec.get('info', {}).get('title', 'unknown')}"
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        spec = self._load_spec()
        endpoints = self._extract_endpoints(spec)
        result.entities_added = len(endpoints)
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        spec = self._load_spec()
        return self._extract_endpoints(spec)[:5]

    def _load_spec(self) -> dict[str, Any]:
        if url := self.config.get("spec_url"):
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            if url.endswith((".yaml", ".yml")):
                import yaml
                return yaml.safe_load(resp.text)
            return resp.json()
        if path := self.config.get("spec_path"):
            with open(path) as f:
                if path.endswith((".yaml", ".yml")):
                    import yaml
                    return yaml.safe_load(f)
                return json.load(f)
        raise ValueError("No spec_url or spec_path configured")

    def _extract_endpoints(self, spec: dict) -> list[dict[str, Any]]:
        endpoints = []
        for path, methods in spec.get("paths", {}).items():
            for method, op in methods.items():
                if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    endpoints.append({
                        "path": path,
                        "method": method.upper(),
                        "operationId": op.get("operationId"),
                        "summary": op.get("summary"),
                        "tags": op.get("tags", []),
                        "security": op.get("security", []),
                    })
        return endpoints
