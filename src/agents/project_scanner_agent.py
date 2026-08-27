"""
Project Scanner Agent — Stage 1 of the AURA understanding pipeline.
Discovers files, programming languages, frameworks, directory structure, and
entry points for a given project. Outputs a structured project manifest used
by file_analyzer and architecture_analyzer in Stage 2.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boto3

from .base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import settings as s

_FRAMEWORK_MARKERS: dict[str, list[str]] = {
    "fastapi": ["fastapi", "uvicorn"],
    "django": ["django", "manage.py"],
    "flask": ["flask"],
    "react": ["react", "vite.config", "next.config"],
    "angular": ["@angular/core"],
    "spring": ["spring-boot", "pom.xml"],
    "express": ["express", "package.json"],
}

_LANG_EXT: dict[str, str] = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".java": "java",
    ".go": "go", ".rs": "rust", ".cs": "csharp", ".rb": "ruby",
    ".tf": "terraform", ".yaml": "yaml", ".yml": "yaml",
}


class ProjectScannerAgent(BaseAgent):
    name = "project_scanner"
    description = (
        "Scans a project directory or repository to discover files, languages, "
        "frameworks, entry points, and dependency manifests."
    )

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        project_path = context.extra.get("project_path", ".")
        result.log(f"Scanning project at: {project_path}")

        manifest = _scan_directory(project_path)
        result.log(
            f"Discovered {manifest['total_files']} files in "
            f"{manifest['total_dirs']} directories"
        )

        prompt = _build_prompt(manifest, context.intent)
        try:
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = client.invoke_model(
                modelId=s.bedrock_model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            raw = json.loads(resp["body"].read())
            analysis = json.loads(raw["content"][0]["text"])
            result.log("LLM project analysis complete")
        except Exception as exc:  # noqa: BLE001
            result.log(f"LLM analysis failed, using raw manifest: {exc}")
            analysis = {"summary": "scan-only mode", "frameworks": [], "entry_points": []}

        result.output = {**manifest, "analysis": analysis}
        return result.finish("success")


def _scan_directory(root: str) -> dict[str, Any]:
    lang_counts: dict[str, int] = {}
    file_list: list[str] = []
    dep_files: list[str] = []
    total_dirs = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {
            ".git", "node_modules", "__pycache__", ".venv", "dist", "build",
        }]
        total_dirs += 1
        for fname in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fname), root)
            ext = Path(fname).suffix.lower()
            lang = _LANG_EXT.get(ext)
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
                if len(file_list) < 500:
                    file_list.append(rel)
            if fname in {
                "requirements.txt", "pyproject.toml", "package.json",
                "pom.xml", "go.mod", "Cargo.toml", "Gemfile",
            }:
                dep_files.append(rel)

    return {
        "root": root,
        "total_files": sum(lang_counts.values()),
        "total_dirs": total_dirs,
        "language_counts": lang_counts,
        "primary_language": max(lang_counts, key=lang_counts.get) if lang_counts else "unknown",
        "dependency_manifests": dep_files,
        "file_sample": file_list[:100],
    }


def _build_prompt(manifest: dict[str, Any], intent: str) -> str:
    return (
        f"You are AURA's project scanner. Analyze this project manifest and return JSON.\n\n"
        f"Intent: {intent}\n"
        f"Manifest: {json.dumps(manifest, indent=2)[:3000]}\n\n"
        "Return JSON: {frameworks: [], entry_points: [], layers: [], "
        "test_directories: [], summary: string}"
    )
