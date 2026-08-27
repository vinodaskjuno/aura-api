"""
Repo Ingestion Service — orchestrates parse → LLM describe → Neo4j write.

Flow:
  1. For each repo in a service: clone (or use localPath) → detect type → parse
  2. Merge all ParseResults for the service
  3. Call Bedrock to generate a 2-3 sentence description
  4. Write all ontology nodes + relationships to Neo4j
  5. Run cross-service correlation for the project
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from src.connectors.repo_connector import RepositoryConnector
from src.parsers import PARSER_MAP, detect_repo_type
from src.graph import neo4j_client as neo4j
from src.config_settings import settings

log = logging.getLogger(__name__)

_repo_connector = RepositoryConnector()


# ── Public API ────────────────────────────────────────────────────────────────

async def ingest_service(
    project_id: str,
    service_id: str,
    service_name: str,
    repos: list[dict[str, Any]],
    version_id: str | None = None,
) -> dict[str, Any]:
    """
    Clone + parse all repos for one service, write to Neo4j, return stats.

    Each repo dict: {repoUrl, repoType, token, branch, localPath (optional)}
    """
    log.info("Ingesting service %s (%s): %d repo(s)", service_name, service_id, len(repos))
    merged = _empty_result()
    merged["service_name"] = service_name
    parse_log: list[str] = []

    for repo in repos:
        repo_url   = repo.get("repoUrl", "")
        repo_type  = repo.get("repoType", "") or ""
        token      = repo.get("token", "")
        branch     = repo.get("branch", "main")
        local_path = repo.get("localPath", "")

        # 1. Clone if no local path
        if not local_path and repo_url:
            try:
                meta = _repo_connector.clone_repository(repo_url, branch, repo_type or "application", token or None)
                local_path = meta.local_path
                parse_log.append(f"Cloned {repo_url} → {local_path}")
            except Exception as exc:
                parse_log.append(f"Clone failed ({repo_url}): {exc}")
                continue

        if not local_path:
            parse_log.append(f"Skipped repo (no local path): {repo_url}")
            continue

        # 2. Auto-detect type if not specified
        if not repo_type or repo_type == "auto":
            repo_type = detect_repo_type(local_path)
            parse_log.append(f"Auto-detected type: {repo_type} for {local_path}")

        # 3. Parse
        parser_cls = PARSER_MAP.get(repo_type)
        if not parser_cls:
            parse_log.append(f"No parser for type '{repo_type}' — skipping")
            continue

        try:
            parse_result = parser_cls().parse(local_path)
            _merge_results(merged, parse_result)
            tech = parse_result.get("tech_stack", [])
            apis = parse_result.get("apis", [])
            dbs  = parse_result.get("databases", [])
            deps = parse_result.get("dependencies", [])
            mods = parse_result.get("modules", [])
            parse_log.append(
                f"Parsed [{repo_type}] {repo_url or local_path}: "
                f"{len(apis)} APIs, {len(dbs)} DBs, {len(deps)} deps, "
                f"{len(mods)} modules | tech: {', '.join(tech[:6])}"
            )
            log.info("Parsed [%s] %s: %d APIs, %d DBs, %d deps, tech=%s",
                     repo_type, local_path, len(apis), len(dbs), len(deps), tech[:6])
        except Exception as exc:
            parse_log.append(f"Parse error [{repo_type}]: {exc}")
            log.exception("Parse error for %s", local_path)

    # 4. LLM description
    description = await generate_service_description(merged, service_name)
    parse_log.append(f"Generated description ({len(description)} chars)")

    # 5. Write to Neo4j
    stats = _write_to_neo4j(project_id, service_id, service_name, description, merged, version_id)
    stats["log"] = parse_log
    stats["description"] = description

    # 6. Persist description back to DynamoDB
    try:
        from src.database import dynamo_client as db
        db.update_item(
            "services",
            {"projectId": project_id, "serviceId": service_id},
            {
                "description": description,
                "techStack":   merged.get("tech_stack", []),
                "lastIngested": datetime.now(timezone.utc).isoformat(),
                "status": "ingested",
                "ontologyStats": {k: v for k, v in stats.items() if k != "log"},
            },
        )
    except Exception as exc:
        log.warning("Failed to persist service description: %s", exc)

    return stats


async def generate_service_description(parse_result: dict, service_name: str) -> str:
    """Call Bedrock to produce a 2-3 sentence plain-English service description."""
    tech   = ", ".join(parse_result.get("tech_stack", [])[:8])
    apis   = [a.get("path", "") for a in parse_result.get("apis", [])[:5]]
    calls  = parse_result.get("downstream_calls", [])[:5]
    dbs    = [d.get("url", d.get("host", d.get("name", ""))) for d in parse_result.get("databases", [])[:3]]
    topics = parse_result.get("topics", [])[:3]
    envs   = parse_result.get("environments", [])

    prompt = (
        f"You are AURA, an enterprise architecture AI. Write a concise 2-3 sentence description "
        f"for a software service named '{service_name}'.\n\n"
        f"Technology: {tech or 'unknown'}\n"
        f"Exposed APIs (sample): {', '.join(apis) or 'none detected'}\n"
        f"Downstream calls: {', '.join(str(c) for c in calls) or 'none detected'}\n"
        f"Databases: {', '.join(str(d) for d in dbs) or 'none detected'}\n"
        f"Message topics: {', '.join(topics) or 'none'}\n"
        f"Environments: {', '.join(envs) or 'unknown'}\n\n"
        f"Instructions: Be specific and factual. Mention the technology stack, primary purpose, "
        f"and key integrations. Do not add bullet points or headers — plain prose only."
    )

    try:
        client = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        })
        resp = client.invoke_model(
            modelId=settings.bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        raw = json.loads(resp["body"].read())
        return raw["content"][0]["text"].strip()
    except Exception as exc:
        log.warning("LLM description failed: %s", exc)
        # Fallback: compose from parsed data
        parts = [f"'{service_name}' is a service"]
        if tech:
            parts.append(f"built with {tech}")
        if apis:
            parts.append(f"exposing {len(parse_result['apis'])} REST endpoint(s)")
        if dbs:
            parts.append(f"connecting to {len(parse_result['databases'])} database(s)")
        return " ".join(parts) + "."


def correlate_services(project_id: str) -> dict[str, Any]:
    """
    Cross-service correlation within a project:
    - downstream_calls URL → match to a known Service's API path → DEPENDS_ON
    - shared Kafka topic → producer + consumer services → DataFlow
    - shared DB hostname → single shared Database node
    """
    log.info("Running cross-service correlation for project %s", project_id)
    rels_added = 0

    try:
        # Find all services in this project from Neo4j
        cypher = """
        MATCH (s:Service {projectId: $pid})
        OPTIONAL MATCH (s)-[:PART_OF]->(a:API)
        RETURN s.externalId as sid, s.name as sname,
               collect(a.path) as api_paths
        """
        services = neo4j.run_query(cypher, {"pid": project_id})

        # Build lookup: api_path → service_id
        path_to_service: dict[str, str] = {}
        for svc in (services or []):
            for path in (svc.get("api_paths") or []):
                if path:
                    path_to_service[path] = svc["sid"]

        # For each service, check its downstream_calls against known paths
        for svc in (services or []):
            sid = svc["sid"]
            calls_cypher = """
            MATCH (s:Service {externalId: $sid})-[:CONNECTS_TO]->(df:DataFlow)
            RETURN df.url as url
            """
            calls = neo4j.run_query(calls_cypher, {"sid": sid}) or []
            for call in calls:
                url = call.get("url", "")
                # Fuzzy match URL path against known API paths
                matched = _match_url_to_service(url, path_to_service, sid)
                if matched:
                    if neo4j.link_nodes_by_eid(sid, matched, "DEPENDS_ON",
                                               {"source": "correlation", "confidence": 0.85}):
                        rels_added += 1

        # Kafka topic correlation
        topic_cypher = """
        MATCH (df:DataFlow {projectId: $pid})
        WHERE df.topic IS NOT NULL
        RETURN df.topic as topic, df.sourceService as src, df.targetService as tgt
        """
        topic_flows = neo4j.run_query(topic_cypher, {"pid": project_id}) or []
        for flow in topic_flows:
            if flow.get("src") and flow.get("tgt"):
                if neo4j.link_nodes_by_eid(flow["src"], flow["tgt"], "CONNECTS_TO",
                                           {"source": "kafka-topic-correlation",
                                            "topic": flow["topic"]}):
                    rels_added += 1

    except Exception as exc:
        log.warning("Correlation error: %s", exc)

    return {"rels_added": rels_added, "project_id": project_id}


# ── Neo4j writer ──────────────────────────────────────────────────────────────

def _write_to_neo4j(
    project_id: str,
    service_id: str,
    service_name: str,
    description: str,
    result: dict,
    version_id: str | None,
) -> dict[str, Any]:
    nodes_added = rels_added = 0

    def upsert(label: str, eid: str, props: dict) -> None:
        nonlocal nodes_added
        p = {"projectId": project_id, "source": "git-repo-loader", **props}
        if version_id:
            neo4j.upsert_node_with_version(label, eid, p, version_id)
        else:
            neo4j.upsert_node(label, eid, p)
        nodes_added += 1

    def rel(src: str, tgt: str, rtype: str) -> None:
        nonlocal rels_added
        ok = neo4j.link_nodes_by_eid(src, tgt, rtype,
                                     {"source": "git-repo-loader", "projectId": project_id})
        if ok:
            rels_added += 1

    # Ensure Project node exists FIRST (link_nodes_by_eid needs both nodes to exist)
    project_name = _get_project_name(project_id)
    neo4j.upsert_node("Project", project_id, {
        "name": project_name,
        "projectId": project_id,
        "source": "git-repo-loader",
    })

    # Service node — store dependencies and tech as properties (not graph nodes)
    upsert("Service", service_id, {
        "name": service_name,
        "description": description,
        "techStack": result.get("tech_stack", []),
        "environments": result.get("environments", []),
        "dependencies": result.get("dependencies", [])[:300],
        "status": "active",
        "serviceId": service_id,
    })

    # Link Project → Service
    if neo4j.link_nodes_by_eid(project_id, service_id, "CONTAINS",
                                {"source": "git-repo-loader"}):
        rels_added += 1

    # APIs
    for i, api in enumerate(result.get("apis", [])[:200]):
        path   = api.get("path", f"/unknown-{i}")
        method = api.get("method", "ANY")
        api_id = f"api:{service_id}:{method}:{_slug(path)}"
        upsert("API", api_id, {
            "name": f"{method} {path}",
            "path": path,
            "method": method,
            "protocol": "REST",
            "source": api.get("source", ""),
            "spec": api.get("spec", ""),
        })
        rel(api_id, service_id, "PART_OF")

    # Databases
    for db_ref in result.get("databases", [])[:50]:
        raw_url = db_ref.get("url") or db_ref.get("host") or db_ref.get("name") or ""
        if not raw_url:
            continue
        db_id  = f"db:{service_id}:{_slug(raw_url)}"
        engine = _detect_db_engine(raw_url)
        upsert("Database", db_id, {
            "name": raw_url[:120],
            "engine": engine,
            "url": raw_url[:300],
            "source": db_ref.get("source", ""),
        })
        rel(service_id, db_id, "CONNECTS_TO")

    # Downstream calls → DataFlow nodes
    for call in result.get("downstream_calls", [])[:100]:
        call_str = str(call)
        df_id = f"dataflow:{service_id}:{_slug(call_str)}"
        upsert("DataFlow", df_id, {
            "name": call_str[:120],
            "url": call_str[:300],
            "sourceService": service_id,
            "protocol": "HTTP",
        })
        rel(service_id, df_id, "CONNECTS_TO")

    # Kafka topics → DataFlow
    for topic in result.get("topics", [])[:50]:
        t_id = f"dataflow:topic:{_slug(topic)}"
        upsert("DataFlow", t_id, {
            "name": topic, "topic": topic,
            "protocol": "Kafka", "sourceService": service_id,
        })
        rel(service_id, t_id, "CONNECTS_TO")

    # Modules (flows / service classes)
    for mod in result.get("flows", []) + result.get("modules", []):
        mod_id = f"mod:{service_id}:{_slug(mod)}"
        upsert("Module", mod_id, {"name": mod})
        rel(mod_id, service_id, "PART_OF")

    # Business rules
    for br in result.get("business_rules", [])[:30]:
        br_id = f"rule:{service_id}:{_slug(br)}"
        upsert("BusinessRule", br_id, {"name": br})
        rel(service_id, br_id, "CONTAINS")

    # Feature flags
    for flag in result.get("feature_flags", [])[:30]:
        flag_id = f"flag:{service_id}:{_slug(flag)}"
        upsert("FeatureFlag", flag_id, {"name": flag})
        rel(flag_id, service_id, "PART_OF")

    # Tables
    for tbl in result.get("tables", [])[:50]:
        tbl_id = f"tbl:{service_id}:{_slug(tbl)}"
        upsert("Table", tbl_id, {"name": tbl})
        # Link to the first database of the service
        dbs = result.get("databases", [])
        if dbs:
            raw = dbs[0].get("url") or dbs[0].get("host") or ""
            if raw:
                rel(tbl_id, f"db:{service_id}:{_slug(raw)}", "BELONGS_TO")

    # Cloud resources (Terraform)
    for cr in result.get("cloud_resources", [])[:100]:
        cr_id = f"cloud:{service_id}:{_slug(cr.get('type','') + cr.get('name',''))}"
        upsert("CloudResource", cr_id, {
            "name": cr.get("name", ""),
            "resourceType": cr.get("type", ""),
            "region": cr.get("region", ""),
        })
        rel(service_id, cr_id, "HOSTED_IN")

    # K8s clusters (Terraform)
    for k8s in result.get("k8s_clusters", [])[:10]:
        k8s_id = f"k8s:{service_id}:{_slug(k8s.get('name',''))}"
        upsert("KubernetesCluster", k8s_id, {
            "name": k8s.get("name", ""),
            "region": k8s.get("region", ""),
        })
        rel(service_id, k8s_id, "DEPLOYED_TO")

    # Networks
    for net in result.get("networks", [])[:20]:
        net_id = f"net:{service_id}:{_slug(net.get('name',''))}"
        upsert("Network", net_id, {
            "name": net.get("name", ""),
            "cidr": net.get("cidr", ""),
            "region": net.get("region", ""),
        })

    # Pipelines (CI/CD)
    for pipe in result.get("pipelines", [])[:10]:
        pipe_id = f"pipeline:{service_id}:{_slug(pipe.get('name', pipe.get('source','ci')))}"
        upsert("BuildPipeline", pipe_id, {
            "name": pipe.get("name", "CI Pipeline"),
            "platform": pipe.get("type", ""),
            "stages": pipe.get("stages", []),
        })
        rel(pipe_id, service_id, "BELONGS_TO")

    # Deployment environments
    for env in result.get("environments", [])[:10]:
        env_id = f"env:{project_id}:{env}"
        upsert("DeploymentEnvironment", env_id, {"name": env, "environment": env})
        rel(service_id, env_id, "DEPLOYED_TO")

    # Features (UI routes)
    for feat in result.get("features", [])[:50]:
        feat_id = f"feat:{service_id}:{_slug(feat)}"
        upsert("Feature", feat_id, {"name": feat, "route": feat})
        rel(feat_id, service_id, "PART_OF")

    return {
        "nodes_added": nodes_added,
        "rels_added": rels_added,
        "apis_count": len(result.get("apis", [])),
        "databases_count": len(result.get("databases", [])),
        "dependencies_count": len(result.get("dependencies", [])),
        "tech_stack": result.get("tech_stack", []),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_result() -> dict[str, Any]:
    return {
        "service_name": "", "tech_stack": [], "apis": [], "databases": [],
        "downstream_calls": [], "dependencies": [], "topics": [], "environments": [],
        "flows": [], "modules": [], "tables": [], "business_rules": [],
        "feature_flags": [], "cloud_resources": [], "k8s_clusters": [],
        "networks": [], "pipelines": [], "containers": [], "features": [],
        "business_processes": [], "description_hints": "",
    }


def _merge_results(base: dict, new: dict) -> None:
    list_keys = [
        "tech_stack", "apis", "databases", "downstream_calls", "dependencies",
        "topics", "environments", "flows", "modules", "tables", "business_rules",
        "feature_flags", "cloud_resources", "k8s_clusters", "networks", "pipelines",
        "containers", "features", "business_processes",
    ]
    for k in list_keys:
        base[k] = base.get(k, []) + new.get(k, [])
    if new.get("service_name") and not base.get("service_name"):
        base["service_name"] = new["service_name"]
    if new.get("description_hints"):
        base["description_hints"] = (base.get("description_hints", "") + " " + new["description_hints"]).strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", str(s))[:80]


def _detect_db_engine(url: str) -> str:
    url_l = url.lower()
    if "postgres" in url_l or "pg" in url_l:
        return "postgresql"
    if "mysql" in url_l:
        return "mysql"
    if "oracle" in url_l:
        return "oracle"
    if "mongo" in url_l:
        return "mongodb"
    if "redis" in url_l:
        return "redis"
    if "elastic" in url_l or "es" in url_l:
        return "elasticsearch"
    return "unknown"


def _get_project_name(project_id: str) -> str:
    """Fetch project name from DynamoDB; fall back to the ID."""
    try:
        from src.database import dynamo_client as db
        # projects table has composite key (projectId PK, userId SK) — use query
        items = db.query_items("projects", "projectId", project_id)
        if items:
            return items[0].get("name", project_id)
    except Exception:
        pass
    return project_id


def _match_url_to_service(url: str, path_to_service: dict[str, str], exclude_sid: str) -> str | None:
    """Try to match a downstream call URL to a known service API path."""
    for path, sid in path_to_service.items():
        if sid == exclude_sid:
            continue
        if path and path in url:
            return sid
        # normalised match: extract path segment from URL
        url_path = re.sub(r"https?://[^/]+", "", url)
        if url_path and path and (url_path.startswith(path) or path.startswith(url_path)):
            return sid
    return None
