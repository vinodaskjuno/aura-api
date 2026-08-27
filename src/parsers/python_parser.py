"""Python repo parser — FastAPI/Flask/Django: routes, models, DB config, tasks."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


class PythonParser:
    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": ["Python"],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "tables": [],
            "modules": [],
            "business_processes": [],
            "description_hints": "",
        }

        self._parse_requirements(root, result)
        self._parse_pyproject(root, result)
        self._parse_python_files(root, result)
        self._parse_env_files(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"Python service '{result['service_name']}' "
            f"exposes {len(result['apis'])} endpoint(s), "
            f"connects to {len(result['databases'])} database(s)."
        )
        return result

    # ── requirements.txt ──────────────────────────────────────────────────────
    def _parse_requirements(self, root: Path, result: dict) -> None:
        for req_file in root.glob("requirements*.txt"):
            try:
                for line in req_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    pkg = re.split(r"[>=<!=~]", line)[0].strip().lower()
                    result["dependencies"].append(line)
                    if pkg in ("fastapi", "starlette"):
                        result["tech_stack"].append("FastAPI")
                    elif pkg == "flask":
                        result["tech_stack"].append("Flask")
                    elif pkg == "django":
                        result["tech_stack"].append("Django")
                    elif pkg in ("sqlalchemy", "databases"):
                        result["tech_stack"].append("SQLAlchemy")
                    elif pkg == "celery":
                        result["tech_stack"].append("Celery")
                    elif pkg in ("redis", "redis-py", "aioredis"):
                        result["tech_stack"].append("Redis")
                    elif pkg in ("psycopg2", "psycopg2-binary", "asyncpg"):
                        result["tech_stack"].append("PostgreSQL")
                    elif pkg in ("pymongo", "motor"):
                        result["tech_stack"].append("MongoDB")
                    elif pkg in ("kafka-python", "aiokafka", "confluent-kafka"):
                        result["tech_stack"].append("Kafka")
                    elif pkg in ("boto3", "botocore"):
                        result["tech_stack"].append("AWS")
            except Exception:
                pass

    # ── pyproject.toml ────────────────────────────────────────────────────────
    def _parse_pyproject(self, root: Path, result: dict) -> None:
        p = root / "pyproject.toml"
        if not p.exists():
            return
        try:
            content = p.read_text(encoding="utf-8")
            name = re.search(r'^name\s*=\s*["\']([^"\']+)["\']', content, re.M)
            if name:
                result["service_name"] = name.group(1)
            for dep in re.findall(r'^\s*["\']([a-zA-Z0-9_-]+)[>=<!\^~]', content, re.M):
                result["dependencies"].append(dep)
        except Exception:
            pass

    # ── Python source files ───────────────────────────────────────────────────
    def _parse_python_files(self, root: Path, result: dict) -> None:
        skip = {".venv", "venv", "__pycache__", ".git", "build", "dist", "migrations",
                "tests", "test", ".mypy_cache", ".tox", "htmlcov"}
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                rel = str(fpath.relative_to(root))

                # FastAPI routes
                for method, path in re.findall(
                    r"@(?:app|router)\.(get|post|put|delete|patch|options)\s*\(\s*[\"']([^\"']+)[\"']",
                    content
                ):
                    result["apis"].append({"path": path, "method": method.upper(), "source": rel})

                # Flask routes
                for methods_str, path in re.findall(
                    r"@(?:app|blueprint)\s*\.route\s*\(\s*[\"']([^\"']+)[\"'][^)]*methods\s*=\s*\[([^\]]+)\]",
                    content
                ):
                    for m in re.findall(r'["\'](\w+)["\']', path):
                        result["apis"].append({"path": methods_str, "method": m.upper(), "source": rel})

                # Django URL patterns
                for path in re.findall(r"path\s*\(\s*[\"']([^\"']+)[\"']", content):
                    result["apis"].append({"path": "/" + path, "method": "ANY", "source": rel})

                # SQLAlchemy models → tables
                for tname in re.findall(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']", content):
                    if tname not in result["tables"]:
                        result["tables"].append(tname)

                # Database URLs in config
                for url in re.findall(r'(?:DATABASE_URL|DB_URL|SQLALCHEMY_DATABASE_URI)\s*=\s*["\']([^"\']+)["\']', content):
                    result["databases"].append({"url": url, "source": rel})

                # Downstream HTTP calls
                for url in re.findall(r'(?:httpx|requests|aiohttp)\s*\.\s*\w+\s*\(\s*["\']([^"\']+)["\']', content):
                    if url.startswith("http") and url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)
                for url in re.findall(r'(?:base_url|BASE_URL|API_URL|ENDPOINT)\s*=\s*["\']([^"\']+)["\']', content):
                    if url.startswith("http") and url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)

                # Celery tasks
                for tname in re.findall(r"@(?:app|celery)\.task[^)]*\)\s*\ndef\s+(\w+)", content):
                    result["business_processes"].append(tname)

                # Kafka
                for topic in re.findall(r'(?:topic|TOPIC|KAFKA_TOPIC)\s*=\s*["\']([^"\']+)["\']', content):
                    if topic not in result["topics"]:
                        result["topics"].append(topic)

                # Service/manager class names
                for cls in re.findall(r"class\s+(\w+(?:Service|Manager|Handler|Repository))", content):
                    if cls not in result["modules"]:
                        result["modules"].append(cls)

    # ── .env files ────────────────────────────────────────────────────────────
    def _parse_env_files(self, root: Path, result: dict) -> None:
        for env_file in root.glob(".env*"):
            try:
                content = env_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            env_name = self._env_from_filename(env_file.name)
            if env_name and env_name not in result["environments"]:
                result["environments"].append(env_name)
            for line in content.splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip().lower(), val.strip().strip('"').strip("'")
                if any(k in key for k in ("db_url", "database_url", "postgres", "mysql", "mongo")):
                    result["databases"].append({"url": val, "source": env_file.name})
                elif any(k in key for k in ("api_url", "base_url", "endpoint", "service_url")):
                    if val.startswith("http") and val not in result["downstream_calls"]:
                        result["downstream_calls"].append(val)

    def _env_from_filename(self, name: str) -> str | None:
        m = re.search(r"\.env\.(dev|test|staging|prod|uat|qa|local|sit)", name, re.I)
        return m.group(1).lower() if m else None
