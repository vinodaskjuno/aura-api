from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".py": "python", ".java": "java", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".cs": "csharp", ".cob": "cobol",
    ".cbl": "cobol", ".xml": "mule", ".yaml": "yaml", ".yml": "yaml",
    ".kt": "kotlin", ".go": "go", ".rb": "ruby", ".php": "php",
}


class CodeAnalysisAgent(BaseAgent):
    name = "code_analysis_agent"
    description = "Analyse multi-language source code: Java, Python, TypeScript, Cobol, Mule, Angular, React"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("CodeAnalysisAgent: Starting multi-language code analysis")

        project_id = context.project_id
        if not project_id:
            result.log("No project_id — skipping code analysis")
            return result.finish("partial")

        # Pull project connectors from DynamoDB
        try:
            from src.database.dynamo_client import scan_items
            all_connectors = scan_items("user-connectors", limit=500)
            connectors = [c for c in all_connectors if c.get("projectId") == project_id]
        except Exception:
            connectors = []

        if not connectors:
            result.log("No connectors found for project — skipping")
            return result.finish("partial")

        analysis_summary: dict = {
            "project_id": project_id,
            "repos_analyzed": 0,
            "total_files": 0,
            "tech_stack": [],
            "languages": {},
            "services": [],
            "apis": [],
            "dependencies": [],
            "db_references": [],
            "cloud_resources": [],
        }

        for connector in connectors:
            source_type = connector.get("sourceType", "git")
            if source_type == "mcp":
                continue  # MCP connectors are not file-based — skip code analysis

            local_path = connector.get("localPath", "")
            repo_url   = connector.get("repoUrl", "")
            repo_type  = connector.get("repoType", "unknown")
            label      = local_path or repo_url or "(unknown)"
            result.log(f"Analysing {repo_type} [{source_type}]: {label}")

            try:
                repo_analysis = await asyncio.get_event_loop().run_in_executor(
                    None, self._analyse_repo, connector
                )
                if repo_analysis.get("file_count", 0) == 0 and not repo_analysis.get("tech_stack"):
                    result.log(f"  Skipped (no files found): {label}")
                    continue
                analysis_summary["repos_analyzed"] += 1
                analysis_summary["total_files"] += repo_analysis.get("file_count", 0)
                for lang, count in repo_analysis.get("languages", {}).items():
                    analysis_summary["languages"][lang] = analysis_summary["languages"].get(lang, 0) + count
                analysis_summary["tech_stack"].extend(repo_analysis.get("tech_stack", []))
                analysis_summary["services"].extend(repo_analysis.get("services", []))
                analysis_summary["apis"].extend(repo_analysis.get("apis", []))
                analysis_summary["dependencies"].extend(repo_analysis.get("dependencies", []))
            except Exception as exc:
                result.log(f"  Warning [{repo_type}]: {exc}")

        # Deduplicate
        analysis_summary["tech_stack"] = list(set(analysis_summary["tech_stack"]))
        analysis_summary["dependencies"] = list(set(analysis_summary["dependencies"]))

        result.output = analysis_summary
        result.log(f"Analysis complete: {analysis_summary['repos_analyzed']} repos, "
                   f"{analysis_summary['total_files']} files, "
                   f"tech stack: {', '.join(analysis_summary['tech_stack'][:8])}")

        # Persist to S3
        try:
            from src.storage.s3_client import put_json
            uri = put_json("analysis", f"{project_id}/code_analysis.json", analysis_summary)
            result.artifacts.append(S3Ref(bucket="aura-analysis", key=f"{project_id}/code_analysis.json", uri=uri))
            result.log(f"Saved analysis to {uri}")
        except Exception as exc:
            result.log(f"S3 save warning: {exc}")

        return result.finish("success")

    def _analyse_repo(self, connector: dict) -> dict:
        """Analyse a single repo connector — local path (file walk + AST) or git URL (metadata only)."""
        local_path  = connector.get("localPath", "").strip()
        source_type = connector.get("sourceType", "git")
        tech_stack: list[str] = []
        languages:  dict[str, int] = {}
        services:   list[str] = []
        apis:       list[str] = []
        dependencies: list[str] = []
        file_count  = 0

        # ── For git-only connectors with no local path, infer from URL ────────
        if source_type == "git" and not local_path:
            repo_url = connector.get("repoUrl", "")
            repo_type = connector.get("repoType", "")
            # Infer tech from repo type label
            type_hints = {
                "ui": ["React", "TypeScript", "Node.js"],
                "backend": ["Java", "Spring Boot"],
                "mule": ["Mule", "Java", "XML"],
                "infra": ["Terraform", "AWS", "Kubernetes"],
                "config": ["YAML", "Docker"],
            }
            tech_stack = type_hints.get(repo_type, [])
            if "angular" in repo_url.lower():  tech_stack += ["Angular", "TypeScript"]
            if "react"   in repo_url.lower():  tech_stack += ["React", "TypeScript"]
            if "spring"  in repo_url.lower():  tech_stack += ["Java", "Spring Boot"]
            if "python"  in repo_url.lower():  tech_stack += ["Python"]
            return {"file_count": 0, "languages": {}, "tech_stack": list(set(tech_stack)),
                    "services": [], "apis": [], "dependencies": []}

        # ── Local path analysis ───────────────────────────────────────────────
        if not local_path or not os.path.isdir(local_path):
            logger.warning("Local path not found or not a directory: %r", local_path)
            return {"file_count": 0, "languages": {}, "tech_stack": [], "services": [], "apis": [], "dependencies": []}

        SKIP_DIRS = {".git", "node_modules", "__pycache__", "target", "build", ".venv", "venv",
                     "dist", ".next", "coverage", ".mypy_cache"}

        # Walk all files — count by language, run Python AST analysis
        python_files: list[str] = []
        for root, dirs, files in os.walk(local_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for fname in files:
                ext = Path(fname).suffix.lower()
                lang = SUPPORTED_EXTENSIONS.get(ext)
                if lang:
                    languages[lang] = languages.get(lang, 0) + 1
                    file_count += 1
                    if ext == ".py":
                        python_files.append(os.path.join(root, fname))

        # Python AST — extract class names as services
        if python_files:
            try:
                from src.services.code_analyzer import PythonCodeAnalyzer
                analyzer = PythonCodeAnalyzer()
                for py_file in python_files[:30]:  # cap at 30 files
                    module = analyzer.analyze_file(py_file)
                    if module:
                        for cls in module.classes:
                            name = cls.name
                            # Classes that look like services/controllers/agents
                            if any(s in name for s in ("Service", "Controller", "Router", "Agent", "Handler", "Manager")):
                                if name not in services:
                                    services.append(name)
            except Exception as exc:
                logger.debug("Python AST analysis warning: %s", exc)

        # Tech stack from config files
        CONFIG_TECH = {
            "pom.xml":              ["Java", "Maven"],
            "build.gradle":         ["Java", "Gradle"],
            "package.json":         ["Node.js"],
            "requirements.txt":     ["Python"],
            "pyproject.toml":       ["Python"],
            "setup.py":             ["Python"],
            "angular.json":         ["Angular", "TypeScript"],
            "next.config.js":       ["Next.js", "React", "TypeScript"],
            "nuxt.config.js":       ["Nuxt.js", "Vue"],
            "mule-artifact.json":   ["Mule 4", "Java"],
            "Dockerfile":           ["Docker"],
            "docker-compose.yml":   ["Docker", "Docker Compose"],
            "terraform.tf":         ["Terraform", "AWS"],
            "main.tf":              ["Terraform"],
            "Chart.yaml":           ["Helm", "Kubernetes"],
            "serverless.yml":       ["Serverless", "AWS Lambda"],
            ".csproj":              [".NET", "C#"],
            ".sln":                 [".NET"],
            "go.mod":               ["Go"],
            "Cargo.toml":           ["Rust"],
            "Gemfile":              ["Ruby", "Ruby on Rails"],
        }
        for root, dirs, files in os.walk(local_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                for pattern, techs in CONFIG_TECH.items():
                    if fname == pattern or fname.endswith(pattern):
                        tech_stack.extend(techs)
                # .cbl files for COBOL
                if fname.lower().endswith((".cbl", ".cob", ".cobol")):
                    tech_stack.extend(["COBOL", "Mainframe"])

        # Infer tech from detected languages
        lang_tech = {
            "typescript": "TypeScript", "javascript": "JavaScript",
            "java": "Java", "python": "Python", "csharp": ".NET",
            "kotlin": "Kotlin", "go": "Go", "rust": "Rust", "ruby": "Ruby",
            "mule": "Mule", "cobol": "COBOL",
        }
        for lang in languages:
            if lang in lang_tech and lang_tech[lang] not in tech_stack:
                tech_stack.append(lang_tech[lang])

        return {
            "file_count":  file_count,
            "languages":   languages,
            "tech_stack":  list(set(tech_stack)),
            "services":    list(set(services)),
            "apis":        apis,
            "dependencies": dependencies,
        }
