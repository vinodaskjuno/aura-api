"""Lens KPI aggregation.

One small, index-friendly query per KPI group rather than a single chained-``WITH``
mega-query. That shape is deliberate: the chained form is what broke
``/api/dashboard/infrastructure``, where a ``collect()`` after an aggregation runs
without a grouping key and silently collapses every distribution to one bucket.

Returns ``available: False`` with zeroed KPIs when Neo4j is down, so the lens
header renders rather than erroring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.graph import neo4j_client as neo4j
from src.ontology.lenses import Lens

log = logging.getLogger(__name__)


def _iso_days_ago(days: int) -> str:
    """UTC ISO-8601 with a trailing Z, matching the seed's timestamp shape.

    Mixed shapes break the lexicographic comparisons these queries rely on:
    ``'2026-08-23T12:00:00Z' < '2026-08-23T12:00:00+00:00'`` is true as strings.
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _scalar(cypher: str, **params) -> dict:
    """Run a single-row query, returning {} when it yields nothing."""
    try:
        with neo4j.session() as s:
            rec = s.run(cypher, **params).single()
            return dict(rec) if rec else {}
    except Exception as exc:
        log.warning("lens KPI query failed: %s", exc)
        return {}


def _breakdown(cypher: str, **params) -> list[dict]:
    """Run a key/count query, returning a list of {key, count}."""
    try:
        with neo4j.session() as s:
            return [{"key": r["key"], "count": r["count"]} for r in s.run(cypher, **params)]
    except Exception as exc:
        log.warning("lens breakdown query failed: %s", exc)
        return []


# ── Git ───────────────────────────────────────────────────────────────────────

def _git_summary() -> tuple[dict, dict]:
    v: dict = {}

    v.update(_scalar("""
        MATCH (r:Repository)
        RETURN count(r) AS repos,
               sum(coalesce(r.linesOfCode, 0))      AS loc,
               sum(coalesce(r.openPullRequests, 0)) AS openPrs
    """))

    v.update(_scalar("""
        MATCH (r:Repository) WHERE r.lastCommit < $since90d
        RETURN count(r) AS staleRepos
    """, since90d=_iso_days_ago(90)))

    v.update(_scalar("""
        MATCH (r:Repository) WHERE NOT (r)-[:OWNED_BY]->()
        RETURN count(r) AS unownedRepos
    """))

    v.update(_scalar("""
        MATCH (r:Repository) WHERE NOT (r)-[:BUILT_BY]->(:BuildPipeline)
        RETURN count(r) AS reposNoPipeline
    """))

    # Run-weighted, not a mean of percentages: a 3-run pipeline at 100% must not
    # offset a 4,000-run one at 80%.
    v.update(_scalar("""
        MATCH (p:BuildPipeline)
        WHERE p.successRatePercent IS NOT NULL AND coalesce(p.totalRuns, 0) > 0
        RETURN sum(p.totalRuns * p.successRatePercent) / sum(p.totalRuns) AS pipelineSuccess,
               sum(p.totalRuns) AS totalRuns,
               count(p)         AS pipelines
    """))

    v.update(_scalar("""
        MATCH (f:CodeFile)
        RETURN count(f)                        AS codeFiles,
               avg(f.testCoverage)             AS avgCoverage,
               sum(coalesce(f.linesOfCode, 0)) AS fileLoc
    """))

    v.update(_scalar("""
        MATCH (d:Dependency)
        WHERE d.hasKnownVulnerability = true OR (:Vulnerability)-[:HAS_FINDING]->(d)
        RETURN count(DISTINCT d) AS vulnDeps
    """))

    v.update(_scalar("""
        MATCH (a:BuildArtifact) WHERE a.signed = false
        RETURN count(a) AS unsignedArtifacts
    """))

    v.update(_scalar("""
        MATCH (d:Deployment) WHERE d.deployedAt >= $since7d
        RETURN count(d) AS deploys7d,
               count(CASE WHEN d.status IN ['failed', 'rolled-back'] THEN 1 END) AS failedDeploys7d
    """, since7d=_iso_days_ago(7)))

    breakdowns = {
        "language": _breakdown("""
            MATCH (r:Repository) WHERE r.language IS NOT NULL
            RETURN r.language AS key, count(*) AS count ORDER BY count DESC LIMIT 12
        """),
        "pipelineTool": _breakdown("""
            MATCH (p:BuildPipeline) WHERE p.tool IS NOT NULL
            RETURN p.tool AS key, count(*) AS count ORDER BY count DESC
        """),
        "visibility": _breakdown("""
            MATCH (r:Repository) WHERE r.visibility IS NOT NULL
            RETURN r.visibility AS key, count(*) AS count ORDER BY count DESC
        """),
    }
    return v, breakdowns


# ── Infra ─────────────────────────────────────────────────────────────────────

def _infra_summary() -> tuple[dict, dict]:
    v: dict = {}

    v.update(_scalar("MATCH (e:DeploymentEnvironment) RETURN count(e) AS environments"))

    v.update(_scalar("""
        MATCH (k:KubernetesCluster)
        RETURN count(k) AS clusters, sum(coalesce(k.nodeCount, 0)) AS clusterNodes
    """))

    v.update(_scalar("""
        MATCH (n) WHERE n:VM OR n:Server RETURN count(n) AS computeNodes
    """))

    v.update(_scalar("""
        MATCH (c:Container)
        RETURN sum(coalesce(c.replicas, 0))    AS containerInstances,
               sum(coalesce(c.restartCount, 0)) AS restarts,
               count(CASE WHEN coalesce(c.restartCount, 0) > 5 THEN 1 END) AS unstableWorkloads
    """))

    v.update(_scalar("""
        MATCH (n:CloudResource) WHERE n.monthlyCostUsd IS NOT NULL
        RETURN sum(n.monthlyCostUsd) AS monthlyCost, count(n) AS costedResources
    """))

    v.update(_scalar("""
        MATCH (n) WHERE (n:CloudResource OR n:Database)
          AND (n.encrypted = false OR n.encryptedAtRest = false OR n.storageEncrypted = false)
        RETURN count(n) AS unencrypted
    """))

    v.update(_scalar("""
        MATCH (s:Server) WHERE s.endOfSupport IS NOT NULL AND s.endOfSupport < $in12mo
        RETURN count(s) AS eolServers
    """, in12mo=_iso_days_ago(-365)))

    v.update(_scalar("""
        MATCH (f:SecurityFinding) WHERE toLower(coalesce(f.severity, '')) = 'critical'
        RETURN count(f) AS criticalFindings
    """))

    v.update(_scalar("""
        MATCH (n:Network)
        RETURN count(CASE WHEN n.flowLogsEnabled = false THEN 1 END) AS noFlowLogs,
               count(CASE WHEN n.openToWorld = true THEN 1 END)      AS worldOpenSgs
    """))

    v.update(_scalar("""
        MATCH (v:VM) WHERE coalesce(v.patchLevel, 'unknown') <> 'current'
        RETURN count(v) AS unpatchedVms
    """))

    v.update(_scalar("""
        MATCH (r:IAMRole) WHERE NOT (r)-[:GOVERNED_BY]->(:IAMPolicy)
        RETURN count(r) AS ungovernedRoles
    """))

    # Infra whose environment cannot be resolved by traversal. The count itself
    # is the finding — unplaceable resources are a hygiene signal, not noise.
    v.update(_scalar("""
        MATCH (n) WHERE (n:CloudResource OR n:VM OR n:Container OR n:Database)
          AND NOT (n)-[:BELONGS_TO|DEPLOYED_TO|PART_OF*1..3]->(:DeploymentEnvironment)
        RETURN count(n) AS unplacedResources
    """))

    breakdowns = {
        "provider": _breakdown("""
            MATCH (n) WHERE n:CloudResource OR n:VM OR n:Server
                         OR n:KubernetesCluster OR n:Network OR n:Database
            RETURN coalesce(n.provider, n.cloudPlatform, 'unknown') AS key,
                   count(*) AS count ORDER BY count DESC
        """),
        "region": _breakdown("""
            MATCH (n) WHERE n.region IS NOT NULL
            RETURN n.region AS key, count(*) AS count ORDER BY count DESC LIMIT 12
        """),
        "label": _breakdown("""
            MATCH (n) WHERE n:CloudResource OR n:VM OR n:Server OR n:Container
                         OR n:KubernetesCluster OR n:Network OR n:Database
            RETURN labels(n)[0] AS key, count(*) AS count ORDER BY count DESC
        """),
        "environment": _breakdown("""
            MATCH (e:DeploymentEnvironment)
            OPTIONAL MATCH (e)<-[:BELONGS_TO|DEPLOYED_TO]-(n)
            RETURN e.name AS key, count(n) AS count ORDER BY count DESC
        """),
    }
    return v, breakdowns


_BUILDERS = {"git": _git_summary, "infra": _infra_summary}


def get_lens_summary(lens: Lens) -> dict:
    """Return ``{lensId, available, kpis: [...], breakdowns: {...}}`` for a lens."""
    if not neo4j.is_available():
        return {
            "lensId": lens.id,
            "available": False,
            "warning": "Neo4j not available — no data",
            "kpis": [
                {"id": k.id, "label": k.label, "format": k.format, "hint": k.hint,
                 "secondary": k.secondary, "value": None}
                for k in lens.kpis
            ],
            "breakdowns": {},
        }

    builder = _BUILDERS.get(lens.id)
    values, breakdowns = builder() if builder else ({}, {})

    return {
        "lensId": lens.id,
        "available": True,
        "kpis": [
            {"id": k.id, "label": k.label, "format": k.format, "hint": k.hint,
             "secondary": k.secondary, "value": values.get(k.id)}
            for k in lens.kpis
        ],
        # Extra computed values the tiles don't claim (totalRuns, worldOpenSgs, …)
        # are still useful to the detail panel, so pass them through.
        "extras": {k: v for k, v in values.items()
                   if k not in {kp.id for kp in lens.kpis}},
        "breakdowns": breakdowns,
    }
