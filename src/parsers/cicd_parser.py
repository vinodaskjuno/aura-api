"""CI/CD parser — Jenkinsfile, GitHub Actions, GitLab CI, Helm, Kubernetes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class CiCdParser:
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
            "pipelines": [],
            "containers": [],
            "description_hints": "",
        }

        self._parse_jenkinsfile(root, result)
        self._parse_github_actions(root, result)
        self._parse_gitlab_ci(root, result)
        self._parse_helm(root, result)
        self._parse_kubernetes(root, result)
        self._parse_dockerfile(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"CI/CD repo '{result['service_name']}' with "
            f"{len(result['pipelines'])} pipeline(s) targeting "
            f"{', '.join(result['environments']) or 'unknown'} environment(s)."
        )
        return result

    # ── Jenkinsfile ───────────────────────────────────────────────────────────
    def _parse_jenkinsfile(self, root: Path, result: dict) -> None:
        for p in list(root.glob("Jenkinsfile")) + list(root.glob("Jenkinsfile.*")):
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            result["tech_stack"].append("Jenkins")

            # Pipeline name
            name = re.search(r"pipelineName\s*=\s*['\"]([^'\"]+)['\"]", content)
            if name:
                result["service_name"] = name.group(1)

            # Stage names
            stages = re.findall(r"stage\s*\(\s*['\"]([^'\"]+)['\"]", content)
            result["pipelines"].append({"source": p.name, "stages": stages, "type": "Jenkins"})

            # Deploy environments
            for env in re.findall(
                r"(?:deploy|environment|deployTo|ENV|ENVIRONMENT)\s*(?:=|:)\s*['\"]([^'\"]+)['\"]",
                content
            ):
                e = self._normalise_env(env)
                if e and e not in result["environments"]:
                    result["environments"].append(e)

            # Downstream job triggers
            for job in re.findall(r"build\s*job\s*:\s*['\"]([^'\"]+)['\"]", content):
                if job not in result["downstream_calls"]:
                    result["downstream_calls"].append(job)

            # Docker image references
            for img in re.findall(r"(?:image|docker pull|FROM)\s+['\"]?([a-zA-Z0-9/_:-]+)['\"]?", content):
                if "/" in img or ":" in img:
                    result["containers"].append({"image": img, "source": p.name})

    # ── GitHub Actions (.github/workflows/*.yml) ──────────────────────────────
    def _parse_github_actions(self, root: Path, result: dict) -> None:
        wf_dir = root / ".github" / "workflows"
        if not wf_dir.exists():
            return
        for f in wf_dir.glob("*.yml"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            result["tech_stack"].append("GitHub Actions")

            # Workflow name
            wname = re.search(r"^name:\s*(.+)$", content, re.M)
            stages: list[str] = re.findall(r"^\s{4,6}(\w[\w -]+):\s*$", content, re.M)

            # environment: targets
            for env in re.findall(r"environment:\s*([^\n{]+)", content):
                e = self._normalise_env(env.strip().strip("'\""))
                if e and e not in result["environments"]:
                    result["environments"].append(e)

            # uses: actions references
            for action in re.findall(r"uses:\s*([^\n@]+)@", content):
                result["dependencies"].append(action.strip())

            # Docker image
            for img in re.findall(r"image:\s*([a-zA-Z0-9/_.:@-]+)", content):
                result["containers"].append({"image": img, "source": f.name})

            result["pipelines"].append({
                "name": wname.group(1).strip() if wname else f.stem,
                "stages": stages[:10],
                "type": "GitHub Actions",
                "source": str(f.relative_to(root)),
            })

    # ── .gitlab-ci.yml ────────────────────────────────────────────────────────
    def _parse_gitlab_ci(self, root: Path, result: dict) -> None:
        for p in [root / ".gitlab-ci.yml", root / ".gitlab-ci.yaml"]:
            if not p.exists():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            result["tech_stack"].append("GitLab CI")

            stages = re.findall(r"^\s*-\s+(\w+)", content, re.M)
            for env in re.findall(r"environment:\s*\n\s+name:\s*([^\n]+)", content):
                e = self._normalise_env(env.strip())
                if e and e not in result["environments"]:
                    result["environments"].append(e)

            for img in re.findall(r"image:\s*([a-zA-Z0-9/_.:@-]+)", content):
                result["containers"].append({"image": img, "source": ".gitlab-ci.yml"})

            result["pipelines"].append({
                "name": root.name, "stages": stages[:10],
                "type": "GitLab CI", "source": ".gitlab-ci.yml",
            })

    # ── Helm ──────────────────────────────────────────────────────────────────
    def _parse_helm(self, root: Path, result: dict) -> None:
        chart = root / "Chart.yaml"
        if not chart.exists():
            chart = next(root.rglob("Chart.yaml"), None)
        if not chart:
            return
        try:
            content = chart.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        result["tech_stack"].append("Helm")
        name = re.search(r"^name:\s*(.+)$", content, re.M)
        if name:
            result["service_name"] = name.group(1).strip()

        # dependencies in Chart.yaml
        for dep in re.findall(r"^\s*-\s*name:\s*(.+)$", content, re.M):
            result["dependencies"].append(dep.strip())

        # values files
        for values_file in chart.parent.glob("values*.yaml"):
            self._parse_helm_values(values_file, result)

    def _parse_helm_values(self, f: Path, result: dict) -> None:
        env = self._normalise_env(f.stem.replace("values", "").lstrip("-"))
        if env and env not in result["environments"]:
            result["environments"].append(env)
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        # image tags
        for tag in re.findall(r"tag:\s*([^\n]+)", content):
            result["containers"].append({"image": f":{tag.strip()}", "source": f.name})
        # replica count
        replicas = re.search(r"replicaCount:\s*(\d+)", content)
        if replicas:
            result["containers"].append({"replicas": int(replicas.group(1)), "source": f.name})
        # ingress host
        for host in re.findall(r"host:\s*([^\n]+)", content):
            h = host.strip()
            if "." in h and h not in result["downstream_calls"]:
                result["downstream_calls"].append(h)

    # ── Kubernetes manifests ──────────────────────────────────────────────────
    def _parse_kubernetes(self, root: Path, result: dict) -> None:
        k8s_dirs = [root / "k8s", root / "kubernetes", root / "manifests", root / "deploy"]
        yaml_files: list[Path] = []
        for d in k8s_dirs:
            if d.exists():
                yaml_files += list(d.rglob("*.yml")) + list(d.rglob("*.yaml"))
        if not yaml_files:
            yaml_files = list(root.rglob("*.yml")) + list(root.rglob("*.yaml"))

        for f in yaml_files[:50]:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "kind:" not in content:
                continue
            result["tech_stack"].append("Kubernetes")

            kind = re.search(r"^kind:\s*(.+)$", content, re.M)
            name_m = re.search(r"^\s*name:\s*(.+)$", content, re.M)
            kind_val = kind.group(1).strip() if kind else "Unknown"
            name_val = name_m.group(1).strip() if name_m else f.stem

            if kind_val in ("Deployment", "StatefulSet", "DaemonSet"):
                for img in re.findall(r"image:\s*([a-zA-Z0-9/_.:@-]+)", content):
                    result["containers"].append({"name": name_val, "image": img, "source": f.name})
                # env vars → downstream URLs
                for url in re.findall(r"value:\s*(https?://[^\n]+)", content):
                    if url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url.strip())

            elif kind_val == "Ingress":
                for host in re.findall(r"host:\s*([^\n]+)", content):
                    h = host.strip()
                    if "." in h:
                        result["downstream_calls"].append(h)

            elif kind_val == "Namespace":
                ns = name_val.lower()
                env = self._normalise_env(ns)
                if env and env not in result["environments"]:
                    result["environments"].append(env)

            elif kind_val == "ConfigMap":
                for url in re.findall(r"(?:URL|ENDPOINT|HOST).*:\s*(https?://[^\n]+)", content):
                    if url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url.strip())

    # ── Dockerfile ────────────────────────────────────────────────────────────
    def _parse_dockerfile(self, root: Path, result: dict) -> None:
        for f in list(root.glob("Dockerfile")) + list(root.glob("Dockerfile.*")):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            result["tech_stack"].append("Docker")
            base = re.search(r"^FROM\s+([^\n]+)", content, re.M)
            if base:
                result["containers"].append({"image": base.group(1).strip(), "source": f.name})
            expose = re.findall(r"^EXPOSE\s+(\d+)", content, re.M)
            for port in expose:
                result["apis"].append({"path": f"port:{port}", "method": "TCP", "source": f.name})

    def _normalise_env(self, s: str) -> str | None:
        m = re.search(r"(dev|test|staging|stage|prod|uat|qa|local|sit)", s, re.I)
        return m.group(1).lower() if m else None
