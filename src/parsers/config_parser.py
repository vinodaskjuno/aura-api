"""Config repo parser — application.yml/properties, RAML, OpenAPI, feature flags."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ConfigParser:
    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": ["Configuration"],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "feature_flags": [],
            "description_hints": "",
        }

        self._parse_yaml_files(root, result)
        self._parse_properties_files(root, result)
        self._parse_raml_files(root, result)
        self._parse_openapi_files(root, result)
        self._parse_feature_flags(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"Config repo '{result['service_name']}' covering "
            f"{', '.join(result['environments']) or 'unknown'} environments, "
            f"{len(result['apis'])} API spec(s), {len(result['feature_flags'])} feature flag(s)."
        )
        return result

    # ── YAML config files ─────────────────────────────────────────────────────
    def _parse_yaml_files(self, root: Path, result: dict) -> None:
        for f in list(root.rglob("application*.yml")) + list(root.rglob("application*.yaml")) \
                + list(root.rglob("bootstrap*.yml")):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            env = self._env_from_filename(f.name)
            if env and env not in result["environments"]:
                result["environments"].append(env)

            # App name
            m = re.search(r"name:\s*(.+)", content)
            if m and "application" in content[:500]:
                result["service_name"] = m.group(1).strip()

            # DB URL
            for url in re.findall(r"url:\s*(jdbc:[^\s]+)", content):
                result["databases"].append({"url": url, "source": f.name})
                if "postgresql" in url.lower():
                    result["tech_stack"].append("PostgreSQL")
                elif "mysql" in url.lower():
                    result["tech_stack"].append("MySQL")

            # Downstream service URLs
            for url in re.findall(r"(?:url|base-url|endpoint|service-url):\s*(https?://[^\s]+)", content):
                if url not in result["downstream_calls"]:
                    result["downstream_calls"].append(url)

            # Kafka
            if "kafka" in content.lower():
                result["tech_stack"].append("Kafka")
                for topic in re.findall(r"(?:topic|topics):\s*([^\n{]+)", content):
                    t = topic.strip().strip("'\"")
                    if t and t not in result["topics"]:
                        result["topics"].append(t)

            result["tech_stack"].append("YAML")

    # ── .properties files ─────────────────────────────────────────────────────
    def _parse_properties_files(self, root: Path, result: dict) -> None:
        for f in root.rglob("*.properties"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            env = self._env_from_filename(f.name)
            if env and env not in result["environments"]:
                result["environments"].append(env)

            for line in content.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                key, _, val = line.partition("=")
                key_l = key.strip().lower()
                val = val.strip()
                if "datasource.url" in key_l or "jdbc" in key_l:
                    result["databases"].append({"url": val, "source": f.name})
                elif any(k in key_l for k in ("api.url", "service.url", "endpoint", "http.url")):
                    if val.startswith("http") and val not in result["downstream_calls"]:
                        result["downstream_calls"].append(val)
            result["tech_stack"].append("Properties")

    # ── RAML files ────────────────────────────────────────────────────────────
    def _parse_raml_files(self, root: Path, result: dict) -> None:
        for f in root.rglob("*.raml"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            result["tech_stack"].append("RAML")
            title = re.search(r"^title:\s*(.+)$", content, re.M)
            version = re.search(r"^version:\s*(.+)$", content, re.M)
            base_uri = re.search(r"^baseUri:\s*(.+)$", content, re.M)

            if title:
                result["service_name"] = title.group(1).strip()

            # Extract resource paths + methods
            current_path = ""
            for line in content.splitlines():
                path_m = re.match(r"^(/[^\s:]+):", line)
                if path_m:
                    current_path = path_m.group(1)
                method_m = re.match(r"^\s+(get|post|put|delete|patch):", line)
                if method_m and current_path:
                    result["apis"].append({
                        "path": current_path,
                        "method": method_m.group(1).upper(),
                        "source": f.name,
                        "spec": "RAML",
                        "version": version.group(1).strip() if version else "",
                    })

    # ── OpenAPI / Swagger ─────────────────────────────────────────────────────
    def _parse_openapi_files(self, root: Path, result: dict) -> None:
        patterns = ["*.oas.yml", "*.oas.yaml", "swagger*.yml", "swagger*.yaml",
                    "openapi*.yml", "openapi*.yaml", "api*.yml", "api-spec*.yml"]
        for pat in patterns:
            for f in root.rglob(pat):
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if "openapi:" not in content and "swagger:" not in content:
                    continue
                result["tech_stack"].append("OpenAPI")
                title = re.search(r"^\s*title:\s*(.+)$", content, re.M)
                if title:
                    result["service_name"] = title.group(1).strip()

                # Extract paths
                in_paths = False
                current_path = ""
                for line in content.splitlines():
                    if re.match(r"^paths:", line):
                        in_paths = True
                        continue
                    if in_paths:
                        path_m = re.match(r"^\s{2}(/[^\s:]+):", line)
                        if path_m:
                            current_path = path_m.group(1)
                        method_m = re.match(r"^\s{4}(get|post|put|delete|patch|options):", line)
                        if method_m and current_path:
                            result["apis"].append({
                                "path": current_path,
                                "method": method_m.group(1).upper(),
                                "source": f.name,
                                "spec": "OpenAPI",
                            })
                        if re.match(r"^\S", line) and "paths:" not in line:
                            in_paths = False

        # JSON OpenAPI specs
        for f in list(root.rglob("swagger*.json")) + list(root.rglob("openapi*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if "paths" in data:
                    result["tech_stack"].append("OpenAPI")
                    title = data.get("info", {}).get("title", "")
                    if title:
                        result["service_name"] = title
                    for path, methods in data["paths"].items():
                        for method in methods:
                            if method.lower() in ("get", "post", "put", "delete", "patch"):
                                result["apis"].append({
                                    "path": path, "method": method.upper(),
                                    "source": f.name, "spec": "OpenAPI",
                                })
            except Exception:
                pass

    # ── Feature flag JSON files ───────────────────────────────────────────────
    def _parse_feature_flags(self, root: Path, result: dict) -> None:
        for f in list(root.rglob("feature*.json")) + list(root.rglob("flags*.json")) \
                + list(root.rglob("toggles*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for key in data:
                        result["feature_flags"].append(key)
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "name" in item:
                            result["feature_flags"].append(item["name"])
            except Exception:
                pass

    def _env_from_filename(self, name: str) -> str | None:
        m = re.search(r"[-._](dev|test|staging|stage|prod|uat|qa|local|sit)", name, re.I)
        return m.group(1).lower() if m else None
