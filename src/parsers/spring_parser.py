"""Spring Boot parser — controllers, feign clients, repositories, config files."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


class SpringParser:
    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": ["Java", "Spring Boot"],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "tables": [],
            "modules": [],
            "description_hints": "",
        }

        self._parse_pom_xml(root, result)
        self._parse_build_gradle(root, result)
        self._parse_java_files(root, result)
        self._parse_yaml_props(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"Spring Boot service '{result['service_name']}' "
            f"exposes {len(result['apis'])} REST endpoint(s), "
            f"connects to {len(result['databases'])} database(s), "
            f"calls {len(result['downstream_calls'])} downstream service(s)."
        )
        return result

    # ── pom.xml ───────────────────────────────────────────────────────────────
    def _parse_pom_xml(self, root: Path, result: dict) -> None:
        p = root / "pom.xml"
        if not p.exists():
            return
        try:
            tree = ET.parse(str(p))
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            artifact = tree.find("m:artifactId", ns)
            if artifact is not None and artifact.text:
                result["service_name"] = artifact.text.strip()
            for dep in tree.findall(".//m:dependency", ns):
                g = dep.find("m:groupId", ns)
                a = dep.find("m:artifactId", ns)
                v = dep.find("m:version", ns)
                if a is not None and a.text:
                    name = a.text.strip()
                    ver  = v.text.strip() if v is not None and v.text else ""
                    result["dependencies"].append(f"{name}:{ver}" if ver else name)
                    # detect tech from dependencies
                    gid = g.text.strip() if g is not None and g.text else ""
                    if "kafka" in name.lower() or "kafka" in gid.lower():
                        result["tech_stack"].append("Kafka")
                    if "redis" in name.lower():
                        result["tech_stack"].append("Redis")
                    if "mongo" in name.lower():
                        result["tech_stack"].append("MongoDB")
                    if "security" in name.lower():
                        result["tech_stack"].append("Spring Security")
            result["tech_stack"].append("Maven")
        except Exception:
            pass

    # ── build.gradle / build.gradle.kts ──────────────────────────────────────
    def _parse_build_gradle(self, root: Path, result: dict) -> None:
        for gradle_name in ("build.gradle", "build.gradle.kts"):
            p = root / gradle_name
            if not p.exists():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                result["tech_stack"].append("Gradle")
                # extract implementation/compile/api dependencies
                # Groovy: implementation 'group:artifact:version'
                # Kotlin:  implementation("group:artifact:version")
                for dep in re.findall(
                    r"""(?:implementation|compile|api|runtimeOnly|testImplementation)\s*[(\s]["']([^"']+)["'][)]?""",
                    content
                ):
                    parts = dep.split(":")
                    name = parts[1] if len(parts) >= 2 else dep
                    ver  = parts[2] if len(parts) >= 3 else ""
                    entry = f"{name}:{ver}" if ver else name
                    if entry not in result["dependencies"]:
                        result["dependencies"].append(entry)
                    name_l = name.lower()
                    if "kafka" in name_l:
                        result["tech_stack"].append("Kafka")
                    if "redis" in name_l:
                        result["tech_stack"].append("Redis")
                    if "mongo" in name_l:
                        result["tech_stack"].append("MongoDB")
                    if "postgres" in name_l or "postgresql" in name_l:
                        result["tech_stack"].append("PostgreSQL")
                    if "mysql" in name_l:
                        result["tech_stack"].append("MySQL")
                    if "spring-boot" in name_l or "spring-web" in name_l:
                        result["tech_stack"].append("Spring Boot")
                    if "security" in name_l:
                        result["tech_stack"].append("Spring Security")

                # project name from settings.gradle
                settings_p = root / "settings.gradle"
                if not settings_p.exists():
                    settings_p = root / "settings.gradle.kts"
                if settings_p.exists():
                    sc = settings_p.read_text(encoding="utf-8", errors="ignore")
                    m = re.search(r"""rootProject\.name\s*=\s*["']([^"']+)["']""", sc)
                    if m:
                        result["service_name"] = m.group(1)
            except Exception:
                pass

    # ── Java source files ─────────────────────────────────────────────────────
    def _parse_java_files(self, root: Path, result: dict) -> None:
        skip = {"target", "build", ".mvn", "test", "__pycache__"}
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in skip]
            for fname in files:
                if not fname.endswith(".java"):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                # Controllers → REST APIs
                if re.search(r"@(Rest)?Controller", content):
                    class_mapping = re.search(r"@RequestMapping\([\"']([^\"']+)[\"']", content)
                    base = class_mapping.group(1) if class_mapping else ""
                    for ann, path1, path2 in re.findall(
                        r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
                        r"(?:\([\"']([^\"']*)[\"']|\(value\s*=\s*[\"']([^\"']*)[\"']|\()?",
                        content
                    ):
                        method_map = {
                            "GetMapping": "GET", "PostMapping": "POST",
                            "PutMapping": "PUT", "DeleteMapping": "DELETE",
                            "PatchMapping": "PATCH", "RequestMapping": "ANY",
                        }
                        path = path1 or path2 or ""
                        full_path = (base + path).strip() or "/"
                        result["apis"].append({
                            "path": full_path,
                            "method": method_map.get(ann, "ANY"),
                            "source": fname,
                        })

                # FeignClient → downstream calls
                feign = re.findall(r"@FeignClient\s*\([^)]*(?:url\s*=\s*[\"']([^\"']+)[\"']|name\s*=\s*[\"']([^\"']+)[\"'])", content)
                for url, name in feign:
                    ref = url or name
                    if ref and ref not in result["downstream_calls"]:
                        result["downstream_calls"].append(ref)

                # RestTemplate / WebClient calls
                rt = re.findall(r'(?:restTemplate|webClient|RestTemplate)\s*\.\s*\w+\s*\(\s*["\']([^"\']+)["\']', content)
                for url in rt:
                    if url.startswith("http") and url not in result["downstream_calls"]:
                        result["downstream_calls"].append(url)

                # JPA / Repository → table names
                if re.search(r"@(Repository|JpaRepository|CrudRepository)", content):
                    entity = re.search(r"(?:interface|class)\s+(\w+)Repository", fname)
                    if entity:
                        result["tables"].append(entity.group(1))
                    # @Table annotation
                    for tname in re.findall(r'@Table\s*\(\s*name\s*=\s*["\']([^"\']+)["\']', content):
                        if tname not in result["tables"]:
                            result["tables"].append(tname)

                # Kafka topics
                for topic in re.findall(r'@KafkaListener\s*\([^)]*topics\s*=\s*\{?["\']([^"\']+)["\']', content):
                    if topic not in result["topics"]:
                        result["topics"].append(topic)
                for topic in re.findall(r'kafkaTemplate\.send\s*\(\s*["\']([^"\']+)["\']', content):
                    if topic not in result["topics"]:
                        result["topics"].append(topic)

                # Service modules
                if re.search(r"@Service", content):
                    class_name = re.search(r"(?:public\s+)?class\s+(\w+)", content)
                    if class_name and class_name.group(1) not in result["modules"]:
                        result["modules"].append(class_name.group(1))

    # ── application.yml / application.properties ──────────────────────────────
    def _parse_yaml_props(self, root: Path, result: dict) -> None:
        resources_dir = root / "src" / "main" / "resources"
        search_dirs = [resources_dir, root]
        for d in search_dirs:
            if not d.exists():
                continue
            for f in d.glob("application*.yml"):
                self._parse_app_yaml(f, result)
            for f in d.glob("application*.yaml"):
                self._parse_app_yaml(f, result)
            for f in d.glob("application*.properties"):
                self._parse_app_props(f, result)
            for bsf in d.glob("bootstrap*.yml"):
                self._parse_app_yaml(bsf, result)

    def _parse_app_yaml(self, f: Path, result: dict) -> None:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        env = self._env_from_filename(f.name)
        if env and env not in result["environments"]:
            result["environments"].append(env)

        # spring.application.name
        m = re.search(r"application:\s*\n\s*name:\s*(.+)", content)
        if m:
            result["service_name"] = m.group(1).strip()

        # datasource
        db_url = re.search(r"url:\s*(jdbc:[^\s]+)", content)
        if db_url:
            result["databases"].append({"url": db_url.group(1).strip(), "source": f.name})
            if "postgresql" in db_url.group(1).lower():
                result["tech_stack"].append("PostgreSQL")
            elif "mysql" in db_url.group(1).lower():
                result["tech_stack"].append("MySQL")
            elif "oracle" in db_url.group(1).lower():
                result["tech_stack"].append("Oracle")

        # downstream service URLs
        for url in re.findall(r"(?:url|base-url|endpoint|service-url):\s*(https?://[^\s]+)", content):
            if url not in result["downstream_calls"]:
                result["downstream_calls"].append(url)

        # Kafka bootstrap servers
        kafka = re.search(r"bootstrap-servers:\s*(.+)", content)
        if kafka:
            result["tech_stack"].append("Kafka")

    def _parse_app_props(self, f: Path, result: dict) -> None:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        env = self._env_from_filename(f.name)
        if env and env not in result["environments"]:
            result["environments"].append(env)

        for line in content.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            key, _, val = line.partition("=")
            key, val = key.strip().lower(), val.strip()
            if "spring.application.name" in key:
                result["service_name"] = val
            elif "datasource.url" in key or "jdbc" in key:
                result["databases"].append({"url": val, "source": f.name})
                if "postgresql" in val.lower():
                    result["tech_stack"].append("PostgreSQL")
            elif any(k in key for k in ("url", "endpoint", "service")):
                if val.startswith("http") and val not in result["downstream_calls"]:
                    result["downstream_calls"].append(val)

    def _env_from_filename(self, name: str) -> str | None:
        m = re.search(r"[-._](dev|test|staging|stage|prod|uat|qa|local|sit)", name, re.I)
        return m.group(1).lower() if m else None
