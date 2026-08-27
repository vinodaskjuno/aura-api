"""Auto-detect repo type from the files present in a local path."""
from __future__ import annotations

import os
from pathlib import Path


def detect_repo_type(local_path: str) -> str:
    """
    Inspect the directory structure and return one of:
    mule | spring | python | ui-react | ui-angular | terraform | cicd | config | library | unknown
    """
    root = Path(local_path)
    if not root.exists():
        return "unknown"

    files: set[str] = set()
    dirs: set[str] = set()
    skip = {".git", "node_modules", "__pycache__", "target", "build", "dist", ".venv", "venv"}

    for dirpath, subdirs, filenames in os.walk(str(root)):
        subdirs[:] = [d for d in subdirs if d not in skip]
        rel = Path(dirpath).relative_to(root)
        dirs.add(str(rel))
        for f in filenames:
            files.add(str(rel / f) if str(rel) != "." else f)

    basenames = {Path(f).name.lower() for f in files}
    exts = {Path(f).suffix.lower() for f in files}

    # ── Mule 4 ────────────────────────────────────────────────────────────────
    if "mule-artifact.json" in basenames:
        return "mule"
    mule_xmls = [f for f in files if "src/main/mule" in f and f.endswith(".xml")]
    if mule_xmls:
        return "mule"

    # ── Terraform ─────────────────────────────────────────────────────────────
    tf_files = [f for f in files if f.endswith(".tf")]
    if tf_files:
        return "terraform"

    # ── CI/CD ─────────────────────────────────────────────────────────────────
    has_jenkinsfile = "jenkinsfile" in basenames or any("jenkinsfile" in f.lower() for f in files)
    has_gha = any(".github/workflows" in f for f in files)
    has_gitlab_ci = ".gitlab-ci.yml" in basenames or ".gitlab-ci.yaml" in basenames
    has_helm = "chart.yaml" in basenames
    has_k8s = any(d in ("k8s", "kubernetes", "manifests") for d in dirs)
    if has_jenkinsfile or has_gha or has_gitlab_ci or (has_helm and not _has_app_code(basenames)):
        return "cicd"

    # ── Spring Boot / Java (Maven or Gradle) ─────────────────────────────────
    has_pom    = "pom.xml" in basenames
    has_gradle = "build.gradle" in basenames or "build.gradle.kts" in basenames
    java_files = [f for f in files if f.endswith(".java") or f.endswith(".kt")]
    if (has_pom or has_gradle) and java_files:
        return "spring"

    # ── Angular ───────────────────────────────────────────────────────────────
    if "angular.json" in basenames:
        return "ui-angular"

    # ── Node.js backend vs React/UI frontend ─────────────────────────────────
    if "package.json" in basenames:
        # Read package.json to decide backend vs frontend
        pkg_json_path = next((root / f for f in files if f == "package.json"), None)
        if pkg_json_path is None:
            # Try the root directly
            pkg_json_path = root / "package.json"
        backend_signals = False
        frontend_signals = False
        try:
            import json
            data = json.loads((root / "package.json").read_text(encoding="utf-8", errors="ignore"))
            all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            dep_names = set(all_deps.keys())
            backend_pkgs = {"express", "fastify", "koa", "hapi", "@nestjs/core", "@nestjs/common",
                            "aws-lambda", "serverless", "aws-sdk", "@aws-sdk/client-lambda",
                            "@aws-cdk/core", "aws-cdk-lib", "sequelize", "mongoose", "pg",
                            "typeorm", "knex", "prisma", "@prisma/client",
                            # Node.js-specific tooling (never used in browser frontend)
                            "ts-node", "nodemon", "ts-jest", "@types/node"}
            frontend_pkgs = {"react", "react-dom", "@angular/core", "vue", "next", "nuxt",
                             "vite", "@vitejs/plugin-react", "svelte"}
            backend_signals = bool(dep_names & backend_pkgs)
            frontend_signals = bool(dep_names & frontend_pkgs)
        except Exception:
            pass

        tsx_jsx_files = [f for f in files if f.endswith(".tsx") or f.endswith(".jsx")]
        if backend_signals and not frontend_signals:
            return "node"
        if tsx_jsx_files or frontend_signals:
            return "ui-react"
        # Has .ts files but no tsx/jsx and no clear signal — check for server patterns
        ts_files = [f for f in files if f.endswith(".ts") and not f.endswith(".d.ts")]
        if ts_files:
            # Peek at content for backend patterns (Lambda, Express, NestJS, CDK)
            backend_content_patterns = (
                "express()", "Router()", "lambda.handler",
                "@Controller", "@Module", "createServer",
                "export const handler", "exports.handler",
                "APIGatewayProxyHandler", "APIGatewayEvent",
                "DynamoDBClient", "S3Client", "SQSClient", "SNSClient",
                "import * as cdk", "aws-cdk-lib",
                "new aws.", "@aws-sdk",
            )
            for tf in ts_files[:15]:
                try:
                    content = (root / tf).read_text(encoding="utf-8", errors="ignore")
                    if any(p in content for p in backend_content_patterns):
                        return "node"
                except Exception:
                    pass
            # No frontend signals (no tsx/jsx, no frontend deps) — default to node, not ui-react
            return "node"
        return "node"  # plain JS without frontend signals → treat as backend

    # ── Python ───────────────────────────────────────────────────────────────
    py_files = [f for f in files if f.endswith(".py")]
    has_requirements = "requirements.txt" in basenames or "pyproject.toml" in basenames
    if py_files and has_requirements:
        return "python"

    # ── Config / properties-only repos ───────────────────────────────────────
    prop_files = [f for f in files if f.endswith(".properties") or f.endswith(".raml")]
    yaml_files = [f for f in files if f.endswith(".yml") or f.endswith(".yaml")]
    code_files = (java_files + py_files +
                  [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))])
    if (prop_files or yaml_files) and not code_files and not tf_files:
        return "config"

    # ── Library / fallback ────────────────────────────────────────────────────
    if has_pom or has_gradle:
        return "spring"   # parse as spring to at least get deps

    return "unknown"


def _has_app_code(basenames: set[str]) -> bool:
    """Return True if there are application source code files."""
    code_exts = {".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".cs"}
    return any(Path(n).suffix.lower() in code_exts for n in basenames)
