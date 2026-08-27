"""
Enhanced dashboard API endpoints - provides real-time aggregated data
from Neo4j and DynamoDB for the main dashboard.
"""
from fastapi import APIRouter, Depends
from datetime import datetime, timezone
import logging

from .auth import get_current_user
from ..graph import neo4j_client as neo4j
from ..database import dynamo_client as dynamo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
def get_dashboard_summary(_: dict = Depends(get_current_user)):
    """Executive summary with key metrics from Neo4j."""
    if not neo4j.is_available():
        return {"totalNodes": 0, "totalRelationships": 0, "criticalVulnerabilities": 0, "activeProjects": 0, "infrastructureResources": 0, "apiEndpoints": 0, "dataAssets": 0, "aiAgents": 0}

    cypher = """
    MATCH (n) WITH count(n) AS totalNodes
    MATCH ()-[r]->() WITH totalNodes, count(r) AS totalRels
    OPTIONAL MATCH (v:SecurityFinding|Vulnerability) WHERE toLower(v.severity) IN ['critical', 'high']
    WITH totalNodes, totalRels, count(v) AS criticalVulns
    OPTIONAL MATCH (p:Project) WHERE p.status = 'active' OR p.status IS NULL
    WITH totalNodes, totalRels, criticalVulns, count(p) AS activeProjects
    OPTIONAL MATCH (i) WHERE i:Infrastructure OR i:Server OR i:VM OR i:Container OR i:KubernetesCluster OR i:CloudResource
    WITH totalNodes, totalRels, criticalVulns, activeProjects, count(DISTINCT i) AS infra
    OPTIONAL MATCH (a:API|APIEndpoint)
    WITH totalNodes, totalRels, criticalVulns, activeProjects, infra, count(a) AS apis
    OPTIONAL MATCH (d:Database|Table)
    WITH totalNodes, totalRels, criticalVulns, activeProjects, infra, apis, count(d) AS dataAssets
    OPTIONAL MATCH (ai:AIModel|AgentDefinition|AI_AGENT) WHERE ai.status = 'active' OR ai.status IS NULL
    RETURN totalNodes, totalRels, criticalVulns, activeProjects, infra, apis, dataAssets, count(ai) AS aiAgents
    """
    
    try:
        with neo4j.session() as s:
            result = s.run(cypher).single()
            if not result:
                return {"totalNodes": 0, "totalRelationships": 0}
            return {"totalNodes": result["totalNodes"], "totalRelationships": result["totalRels"], "criticalVulnerabilities": result["criticalVulns"], "activeProjects": result["activeProjects"], "infrastructureResources": result["infra"], "apiEndpoints": result["apis"], "dataAssets": result["dataAssets"], "aiAgents": result["aiAgents"]}
    except Exception as exc:
        log.exception("get_dashboard_summary failed")
        return {"error": str(exc)}


@router.get("/graph-health")
def get_graph_health(_: dict = Depends(get_current_user)):
    """Neo4j graph health metrics."""
    if not neo4j.is_available():
        return {"available": False}
    try:
        with neo4j.session() as s:
            node_dist = [{"label": r["lbl"], "count": r["cnt"]} for r in s.run("MATCH (n) UNWIND labels(n) AS lbl RETURN lbl, count(*) AS cnt ORDER BY cnt DESC LIMIT 15")]
            orphans = s.run("MATCH (n) WHERE NOT (n)--() RETURN count(n) AS orphans").single()["orphans"]
            sources = [{"source": r["source"], "count": r["cnt"]} for r in s.run("MATCH (n) WHERE n.source IS NOT NULL RETURN n.source AS source, count(*) AS cnt ORDER BY cnt DESC")]
            density_result = s.run("MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() WITH nodes, count(r) AS rels RETURN nodes, rels, CASE WHEN nodes > 0 THEN toFloat(rels) / nodes ELSE 0 END AS density").single()
            return {"available": True, "nodeDistribution": node_dist, "orphanNodes": orphans, "sourceBreakdown": sources, "totalNodes": density_result["nodes"], "totalRelationships": density_result["rels"], "relationshipDensity": round(density_result["density"], 2)}
    except Exception as exc:
        log.exception("get_graph_health failed")
        return {"available": False, "error": str(exc)}


@router.get("/infrastructure")
def get_infrastructure_overview(_: dict = Depends(get_current_user)):
    """Real-time infrastructure metrics from Neo4j."""
    if not neo4j.is_available():
        return {}
    cypher = """
    OPTIONAL MATCH (container:Container) WITH count(container) AS containers
    OPTIONAL MATCH (vm:VM) WITH containers, count(vm) AS vms
    OPTIONAL MATCH (k8s:KubernetesCluster) WITH containers, vms, count(k8s) AS k8sClusters
    OPTIONAL MATCH (cloud:CloudResource) WITH containers, vms, k8sClusters, count(cloud) AS cloudResources
    OPTIONAL MATCH (i) WHERE i:Infrastructure OR i:CloudResource OR i:Server OR i:VM
    WITH containers, vms, k8sClusters, cloudResources, i.cloudPlatform AS platform, count(*) AS platformCount
    WHERE platform IS NOT NULL
    WITH containers, vms, k8sClusters, cloudResources, collect({platform: platform, count: platformCount}) AS cloudDist
    OPTIONAL MATCH (e) WHERE e:Infrastructure OR e:CloudResource OR e:Server OR e:VM
    WITH containers, vms, k8sClusters, cloudResources, cloudDist, e.environment AS env, count(*) AS envCount
    WHERE env IS NOT NULL
    WITH containers, vms, k8sClusters, cloudResources, cloudDist, collect({environment: env, count: envCount}) AS envDist
    OPTIONAL MATCH (r) WHERE r:Infrastructure OR r:CloudResource OR r:Server OR r:VM
    WITH containers, vms, k8sClusters, cloudResources, cloudDist, envDist, r.region AS region, count(*) AS regionCount
    WHERE region IS NOT NULL
    WITH containers, vms, k8sClusters, cloudResources, cloudDist, envDist, collect({region: region, count: regionCount}) AS regionDist
    OPTIONAL MATCH (h) WHERE h:Infrastructure OR h:CloudResource OR h:Server OR h:VM
    WITH containers, vms, k8sClusters, cloudResources, cloudDist, envDist, regionDist, h.status AS status, count(*) AS statusCount
    WHERE status IS NOT NULL
    RETURN containers, vms, k8sClusters, cloudResources, cloudDist, envDist, regionDist, collect({status: status, count: statusCount}) AS statusDist
    """
    try:
        with neo4j.session() as s:
            result = s.run(cypher).single()
            if not result:
                return {}
            return {"resourceCounts": {"containers": result["containers"], "vms": result["vms"], "k8sClusters": result["k8sClusters"], "cloudResources": result["cloudResources"]}, "cloudDistribution": result["cloudDist"], "environmentBreakdown": result["envDist"], "regionalDistribution": result["regionDist"], "healthStatus": result["statusDist"]}
    except Exception as exc:
        log.exception("get_infrastructure_overview failed")
        return {"error": str(exc)}


@router.get("/security")
def get_security_posture(_: dict = Depends(get_current_user)):
    """Real-time security metrics from Neo4j."""
    if not neo4j.is_available():
        return {}
    cypher = """
    OPTIONAL MATCH (v:SecurityFinding|Vulnerability)
    WITH toLower(v.severity) AS severity, count(*) AS cnt
    WHERE severity IS NOT NULL
    WITH collect({severity: severity, count: cnt}) AS severityDist
    OPTIONAL MATCH (open:SecurityFinding|Vulnerability)
    WHERE open.status <> 'closed' AND open.status <> 'resolved'
    WITH severityDist, count(open) AS openFindings
    OPTIONAL MATCH p=(:AttackPath)
    WITH severityDist, openFindings, count(p) AS attackPaths
    OPTIONAL MATCH (s:Service)-[r:HAS_FINDING|HAS_VULNERABILITY]->(f:SecurityFinding|Vulnerability)
    WHERE f.status <> 'closed'
    WITH severityDist, openFindings, attackPaths, s.name AS serviceName, count(f) AS findingCount
    ORDER BY findingCount DESC LIMIT 10
    RETURN severityDist, openFindings, attackPaths, collect({service: serviceName, findings: findingCount}) AS topVulnerable
    """
    try:
        with neo4j.session() as s:
            result = s.run(cypher).single()
            if not result:
                return {}
            return {"severityDistribution": result["severityDist"], "openFindings": result["openFindings"], "attackPaths": result["attackPaths"], "topVulnerableServices": result["topVulnerable"]}
    except Exception as exc:
        log.exception("get_security_posture failed")
        return {"error": str(exc)}


@router.get("/activity-feed")
def get_activity_feed(limit: int = 20, _: dict = Depends(get_current_user)):
    """Real-time activity feed from DynamoDB."""
    try:
        changelog = dynamo.get_recent_changelog(limit=limit)
        activities = []
        for entry in changelog:
            timestamp = entry.get("timestamp", "")
            change_type = entry.get("changeType", "UNKNOWN")
            entity_name = entry.get("entityName", "Unknown")
            actor = entry.get("actor", "System")
            notes = entry.get("notes", "")
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - dt
                if delta.total_seconds() < 60:
                    time_ago = "just now"
                elif delta.total_seconds() < 3600:
                    time_ago = f"{int(delta.total_seconds() / 60)}m ago"
                elif delta.total_seconds() < 86400:
                    time_ago = f"{int(delta.total_seconds() / 3600)}h ago"
                else:
                    time_ago = f"{int(delta.total_seconds() / 86400)}d ago"
            except:
                time_ago = "recently"
            color_map = {"CREATE": "#22c55e", "UPDATE": "#3b82f6", "DELETE": "#ef4444", "BULK_LOAD": "#8b5cf6", "RELATIONSHIP_ADD": "#10b981", "RELATIONSHIP_ARCHIVE": "#f59e0b"}
            color = color_map.get(change_type, "#6b7280")
            text = notes or f"{change_type}: {entity_name} by {actor}"
            activities.append({"text": text, "time": time_ago, "timestamp": timestamp, "color": color, "actor": actor, "type": change_type})
        return {"activities": activities}
    except Exception as exc:
        log.exception("get_activity_feed failed")
        return {"activities": []}


@router.get("/system-health")
def get_system_health(_: dict = Depends(get_current_user)):
    """Real-time system health checks."""
    health_items = []
    neo4j_ok = neo4j.is_available()
    health_items.append({"label": "Neo4j Graph DB", "status": "Connected" if neo4j_ok else "Disconnected", "color": "#22c55e" if neo4j_ok else "#ef4444"})
    try:
        dynamo._get_resource()
        health_items.append({"label": "DynamoDB", "status": "Operational", "color": "#22c55e"})
    except:
        health_items.append({"label": "DynamoDB", "status": "Error", "color": "#ef4444"})
    try:
        from ..advisor import bedrock
        health_items.append({"label": "AWS Bedrock", "status": "Connected", "color": "#22c55e"})
    except:
        health_items.append({"label": "AWS Bedrock", "status": "Not Available", "color": "#f59e0b"})
    health_items.append({"label": "REST API", "status": "Operational", "color": "#22c55e"})
    return {"healthItems": health_items}


@router.get("/applications")
def get_application_portfolio(_: dict = Depends(get_current_user)):
    """Application and service metrics from Neo4j."""
    if not neo4j.is_available():
        return {}
    cypher = """
    OPTIONAL MATCH (s:Service)
    WITH s.status AS status, count(*) AS cnt
    WHERE status IS NOT NULL
    WITH collect({status: status, count: cnt}) AS serviceStatus
    OPTIONAL MATCH (r:Repository)
    WITH serviceStatus, r.primaryLanguage AS lang, count(*) AS langCount
    WHERE lang IS NOT NULL
    WITH serviceStatus, collect({language: lang, count: langCount}) AS techStack
    OPTIONAL MATCH (a:API|APIEndpoint)
    RETURN serviceStatus, techStack, count(a) AS apiCount
    """
    try:
        with neo4j.session() as s:
            result = s.run(cypher).single()
            if not result:
                return {}
            return {"servicesByStatus": result["serviceStatus"], "techStack": result["techStack"], "apiCount": result["apiCount"]}
    except Exception as exc:
        log.exception("get_application_portfolio failed")
        return {"error": str(exc)}


@router.get("/data-landscape")
def get_data_landscape(_: dict = Depends(get_current_user)):
    """Data assets and lineage from Neo4j."""
    if not neo4j.is_available():
        return {}
    cypher = """
    OPTIONAL MATCH (d:Database)
    WITH d.type AS dbType, count(*) AS cnt
    WHERE dbType IS NOT NULL
    WITH collect({type: dbType, count: cnt}) AS dbTypes
    OPTIONAL MATCH (t:Table)
    WITH dbTypes, count(t) AS tableCount
    OPTIONAL MATCH (sens:Table|Database)
    WHERE sens.hasSensitiveData = true OR sens.containsPII = true
    RETURN dbTypes, tableCount, count(sens) AS sensitiveAssets
    """
    try:
        with neo4j.session() as s:
            result = s.run(cypher).single()
            if not result:
                return {}
            return {"databasesByType": result["dbTypes"], "tableCount": result["tableCount"], "sensitiveDataAssets": result["sensitiveAssets"]}
    except Exception as exc:
        log.exception("get_data_landscape failed")
        return {"error": str(exc)}
