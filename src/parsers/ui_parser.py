"""UI repo parser — React / Angular: API calls, routes, environment configs."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


class UIParser:
    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": [],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "features": [],
            "modules": [],
            "description_hints": "",
        }

        self._parse_package_json(root, result)
        self._parse_angular_json(root, result)
        self._parse_env_files(root, result)
        self._parse_proxy_conf(root, result)
        self._parse_source_files(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"UI application '{result['service_name']}' "
            f"built with {', '.join(result['tech_stack'][:3])}. "
            f"Calls {len(result['downstream_calls'])} API endpoint(s)."
        )
        return result

    # ── package.json ──────────────────────────────────────────────────────────
    def _parse_package_json(self, root: Path, result: dict) -> None:
        p = root / "package.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "name" in data:
                result["service_name"] = data["name"]
            all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for pkg in all_deps:
                result["dependencies"].append(f"{pkg}@{all_deps[pkg]}")
                pkg_l = pkg.lower()
                if "react" in pkg_l and pkg_l in ("react", "react-dom"):
                    result["tech_stack"].append("React")
                if "@angular/core" == pkg_l:
                    result["tech_stack"].append("Angular")
                if "vue" == pkg_l:
                    result["tech_stack"].append("Vue")
                if "typescript" == pkg_l:
                    result["tech_stack"].append("TypeScript")
                if pkg_l in ("axios", "node-fetch", "@tanstack/react-query"):
                    result["tech_stack"].append("REST Client")
                if "next" == pkg_l:
                    result["tech_stack"].append("Next.js")
                if "vite" == pkg_l:
                    result["tech_stack"].append("Vite")
            if not result["tech_stack"]:
                result["tech_stack"].append("JavaScript")
        except Exception:
            pass

    # ── angular.json ──────────────────────────────────────────────────────────
    def _parse_angular_json(self, root: Path, result: dict) -> None:
        p = root / "angular.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            projects = data.get("projects", {})
            if projects:
                result["service_name"] = list(projects.keys())[0]
                result["tech_stack"].append("Angular")
        except Exception:
            pass

    # ── environment files ─────────────────────────────────────────────────────
    def _parse_env_files(self, root: Path, result: dict) -> None:
        # Angular environments
        env_dir = root / "src" / "environments"
        if env_dir.exists():
            for f in env_dir.glob("environment*.ts"):
                env_name = self._env_from_filename(f.name)
                if env_name and env_name not in result["environments"]:
                    result["environments"].append(env_name)
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                    for url in re.findall(r"(?:apiUrl|baseUrl|apiEndpoint|serviceUrl)\s*:\s*['\"]([^'\"]+)['\"]", content):
                        if url and url not in result["downstream_calls"]:
                            result["downstream_calls"].append(url)
                except Exception:
                    pass

        # React .env files
        for env_file in list(root.glob(".env")) + list(root.glob(".env.*")):
            env_name = self._env_from_dotenv(env_file.name)
            if env_name and env_name not in result["environments"]:
                result["environments"].append(env_name)
            try:
                content = env_file.read_text(encoding="utf-8", errors="ignore")
                for line in content.splitlines():
                    if not line.strip() or line.startswith("#"):
                        continue
                    key, _, val = line.partition("=")
                    val = val.strip().strip('"').strip("'")
                    if any(k in key.upper() for k in ("API_URL", "BASE_URL", "ENDPOINT", "SERVICE_URL", "REACT_APP_API")):
                        if val.startswith("http") and val not in result["downstream_calls"]:
                            result["downstream_calls"].append(val)
            except Exception:
                pass

    # ── proxy.conf.json ───────────────────────────────────────────────────────
    def _parse_proxy_conf(self, root: Path, result: dict) -> None:
        for p in [root / "proxy.conf.json", root / "proxy.conf.js"]:
            if not p.exists():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                for target in re.findall(r'"target"\s*:\s*"([^"]+)"', content):
                    if target not in result["downstream_calls"]:
                        result["downstream_calls"].append(target)
            except Exception:
                pass

    # ── TypeScript / JavaScript source files ──────────────────────────────────
    def _parse_source_files(self, root: Path, result: dict) -> None:
        skip = {"node_modules", ".git", "dist", "build", ".next", "coverage",
                "__pycache__", ".angular", "e2e"}
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in files:
                if not any(fname.endswith(ext) for ext in (".ts", ".tsx", ".js", ".jsx")):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                rel = str(fpath.relative_to(root))

                # Angular HttpClient calls
                for method, url in re.findall(
                    r"this\.http\.(get|post|put|delete|patch)\s*[<(]\s*['\"]([^'\"]+)['\"]",
                    content
                ):
                    full = url if url.startswith("http") else url
                    result["apis"].append({"path": full, "method": method.upper(), "source": rel})

                # Axios calls
                for method, url in re.findall(
                    r"axios\.(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]",
                    content
                ):
                    result["apis"].append({"path": url, "method": method.upper(), "source": rel})

                # fetch() calls
                for url in re.findall(r"fetch\s*\(\s*['\"]([^'\"]+)['\"]", content):
                    if url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)

                # API base URL constants
                for url in re.findall(r"(?:API_URL|BASE_URL|apiUrl|baseUrl)\s*=\s*['\"]([^'\"]+)['\"]", content):
                    if url.startswith("http") and url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)

                # Angular routes
                if "app-routing" in fname.lower() or "Routes" in content:
                    for route in re.findall(r"path:\s*['\"]([^'\"]+)['\"]", content):
                        if route and route not in result["features"]:
                            result["features"].append(route)

                # React Router routes
                if "Route" in content and ("BrowserRouter" in content or "createBrowserRouter" in content):
                    for route in re.findall(r'path=["\']([^"\']+)["\']', content):
                        if route not in result["features"]:
                            result["features"].append(route)

    def _env_from_filename(self, name: str) -> str | None:
        m = re.search(r"environment\.?([a-z]+)", name, re.I)
        if m and m.group(1) not in ("ts", "js"):
            return m.group(1).lower()
        return None

    def _env_from_dotenv(self, name: str) -> str | None:
        m = re.match(r"\.env\.(.+)", name)
        return m.group(1).lower() if m else None
