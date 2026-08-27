"""Mule 4 repo parser — extracts flows, HTTP listeners, DB configs, downstream calls."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


class MuleParser:
    """Parse a Mule 4 repository and return a structured ParseResult."""

    # Mule XML namespaces
    _NS = {
        "mule": "http://www.mulesoft.org/schema/mule/core",
        "http": "http://www.mulesoft.org/schema/mule/http",
        "db":   "http://www.mulesoft.org/schema/mule/db",
        "kafka":"http://www.mulesoft.org/schema/mule/kafka",
        "vm":   "http://www.mulesoft.org/schema/mule/vm",
        "ee":   "http://www.mulesoft.org/schema/mule/ee/core",
        "apikit":"http://www.mulesoft.org/schema/mule/mule-apikit",
    }

    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": ["Mule 4", "Java"],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "flows": [],
            "business_rules": [],
            "description_hints": "",
        }

        self._parse_artifact_json(root, result)
        self._parse_pom_xml(root, result)
        self._parse_mule_flows(root, result)
        self._parse_properties(root, result)
        self._parse_raml(root, result)
        self._parse_exchange_json(root, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"Mule 4 service '{result['service_name']}' "
            f"exposes {len(result['apis'])} API endpoint(s), "
            f"calls {len(result['downstream_calls'])} downstream service(s), "
            f"connects to {len(result['databases'])} database(s)."
        )
        return result

    # ── mule-artifact.json ───────────────────────────────────────────────────
    def _parse_artifact_json(self, root: Path, result: dict) -> None:
        p = root / "mule-artifact.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "name" in data:
                result["service_name"] = data["name"]
            if "minMuleVersion" in data:
                result["tech_stack"].append(f"Mule {data['minMuleVersion']}")
        except Exception:
            pass

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
            result["tech_stack"].append("Maven")
        except Exception:
            pass

    # ── src/main/mule/*.xml ──────────────────────────────────────────────────
    def _parse_mule_flows(self, root: Path, result: dict) -> None:
        mule_dir = root / "src" / "main" / "mule"
        xml_files = list(mule_dir.rglob("*.xml")) if mule_dir.exists() else []
        # also search root level XML files
        xml_files += [f for f in root.glob("*.xml") if f.name != "pom.xml"]

        for xml_file in xml_files:
            try:
                self._parse_one_mule_xml(xml_file, result)
            except Exception:
                pass

    def _parse_one_mule_xml(self, xml_file: Path, result: dict) -> None:
        content = xml_file.read_text(encoding="utf-8", errors="ignore")
        # strip namespace declarations for simpler regex fallback
        try:
            root_el = ET.fromstring(content)
        except ET.ParseError:
            root_el = None

        # Flow names
        flow_names = re.findall(r'<(?:mule:)?flow[^>]+name=["\']([^"\']+)["\']', content)
        for fn in flow_names:
            if fn not in result["flows"]:
                result["flows"].append(fn)

        # HTTP listener → exposed API
        listeners = re.findall(
            r'<http:listener[^>]+path=["\']([^"\']+)["\'][^>]*(?:allowedMethods=["\']([^"\']*)["\'])?',
            content
        )
        for path, methods in listeners:
            for method in (methods.split(",") if methods else ["ANY"]):
                result["apis"].append({
                    "path": path.strip(),
                    "method": method.strip().upper() or "ANY",
                    "source": xml_file.name,
                })

        # HTTP request → downstream call
        requests = re.findall(
            r'<http:request[^>]+(?:url=["\']([^"\']+)["\']|path=["\']([^"\']+)["\'])',
            content
        )
        for url, path in requests:
            target = (url or path).strip()
            if target and target not in result["downstream_calls"]:
                result["downstream_calls"].append(target)

        # DB config → database reference
        db_configs = re.findall(
            r'<db:(?:config|generic-config)[^>]+(?:host=["\']([^"\']+)["\']|url=["\']([^"\']+)["\'])',
            content
        )
        for host, url in db_configs:
            ref = host or url
            if ref:
                result["databases"].append({"host": ref.strip(), "source": xml_file.name})

        # Kafka topics
        kafka_topics = re.findall(r'<kafka:[^>]+topic=["\']([^"\']+)["\']', content)
        for t in kafka_topics:
            if t not in result["topics"]:
                result["topics"].append(t)

        # VM queues (internal messaging)
        vm_queues = re.findall(r'<vm:[^>]+queueName=["\']([^"\']+)["\']', content)
        for q in vm_queues:
            if q not in result["topics"]:
                result["topics"].append(q)

        # Error handler types → business rules
        error_types = re.findall(r'<on-error-[^>]+type=["\']([^"\']+)["\']', content)
        for et in error_types:
            if et not in result["business_rules"]:
                result["business_rules"].append(et)

        # DataWeave transforms (presence detection)
        if "<ee:transform" in content or "<dw:transform-message" in content:
            if "DataWeave" not in result["tech_stack"]:
                result["tech_stack"].append("DataWeave")

    # ── *.properties ─────────────────────────────────────────────────────────
    def _parse_properties(self, root: Path, result: dict) -> None:
        props_dirs = [
            root / "src" / "main" / "resources",
            root / "src" / "main" / "mule" / "config",
            root,
        ]
        for d in props_dirs:
            if not d.exists():
                continue
            for f in d.rglob("*.properties"):
                env = self._env_from_filename(f.name)
                if env and env not in result["environments"]:
                    result["environments"].append(env)
                content = f.read_text(encoding="utf-8", errors="ignore")
                # DB URLs in properties
                for line in content.splitlines():
                    if "=" in line and not line.strip().startswith("#"):
                        key, _, val = line.partition("=")
                        val = val.strip()
                        if any(k in key.lower() for k in ("db.url", "datasource.url", "jdbc")):
                            result["databases"].append({"url": val, "source": f.name})
                        elif any(k in key.lower() for k in ("http.url", "api.url", "endpoint", "service.url")):
                            if val and val not in result["downstream_calls"]:
                                result["downstream_calls"].append(val)

    def _env_from_filename(self, name: str) -> str | None:
        """Extract environment name from filename like 'application-dev.properties'."""
        m = re.search(r"[-._](dev|test|staging|stage|prod|uat|qa|local|sit)", name, re.I)
        return m.group(1).lower() if m else None

    # ── *.raml ────────────────────────────────────────────────────────────────
    def _parse_raml(self, root: Path, result: dict) -> None:
        for raml in root.rglob("*.raml"):
            try:
                content = raml.read_text(encoding="utf-8", errors="ignore")
                title = re.search(r"^title:\s*(.+)$", content, re.M)
                if title:
                    result["service_name"] = title.group(1).strip()
                version = re.search(r"^version:\s*(.+)$", content, re.M)
                if version and "RAML" not in result["tech_stack"]:
                    result["tech_stack"].append("RAML")
                # Extract resource paths
                for path in re.findall(r"^(/[^\s:]+):", content, re.M):
                    for method in re.findall(r"^\s+(get|post|put|delete|patch):", content, re.M):
                        result["apis"].append({"path": path, "method": method.upper(), "source": raml.name})
                        break
            except Exception:
                pass

    # ── exchange.json ─────────────────────────────────────────────────────────
    def _parse_exchange_json(self, root: Path, result: dict) -> None:
        p = root / "exchange.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "name" in data and not result.get("service_name"):
                result["service_name"] = data["name"]
            if "version" in data:
                result["tech_stack"].append(f"Exchange v{data['version']}")
        except Exception:
            pass
