"""Lens definitions — named projections of the ontology.

A lens is a subset of node labels plus a set of *typed* edge specs. The typing is
the point: ``DEPENDS_ON`` means ``Repository → Dependency`` in the Git lens and
``Service → Service`` in the application view. A flat label + reltype filter
cannot separate those, which is why lens projection lives here and on the server
rather than being reconstructed by each client.

Import-time validation against :mod:`src.ontology.schema` turns lens/ontology
drift into a startup failure instead of a silently empty graph.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.ontology.schema import ALL_LABELS, RELATIONSHIP_TYPES


@dataclass(frozen=True)
class EdgeSpec:
    """One typed edge: ``from_labels -[rel_type]-> to_labels``.

    An edge is included in the lens only when the relationship type *and* both
    endpoint labels match. ``reverse`` records that the stored data direction is
    the opposite of the direction the view wants to draw (the seed points
    ``PART_OF``/``COMMITTED_TO`` child→parent, while the visual story reads
    parent→child), so renderers can flip the arrow without re-querying.
    """
    from_labels: tuple[str, ...]
    rel_type: str
    to_labels: tuple[str, ...]
    reverse: bool = False
    weight: int = 1


@dataclass(frozen=True)
class KpiSpec:
    id: str
    label: str
    format: str = "int"          # int | compact | percent | currency | duration
    hint: str = ""
    secondary: bool = False


@dataclass(frozen=True)
class Lens:
    id: str
    name: str
    description: str
    accent: str
    labels: tuple[str, ...]
    edges: tuple[EdgeSpec, ...]
    tiers: dict[str, int]
    anchor_labels: tuple[str, ...]
    kpis: tuple[KpiSpec, ...]

    def edge_types(self) -> tuple[str, ...]:
        """Distinct relationship types referenced by this lens."""
        seen: dict[str, None] = {}
        for e in self.edges:
            seen.setdefault(e.rel_type, None)
        return tuple(seen)


# ── Git lens ──────────────────────────────────────────────────────────────────
# Two joined columns: a structure spine (Repository → Module → CodeFile → Class →
# Function) and a release spine (Repository → BuildPipeline → BuildArtifact →
# Deployment → Environment), meeting at Service.

GIT_LENS = Lens(
    id="git",
    name="Git",
    description="Repository, code structure, build and release topology",
    accent="#f0564a",
    labels=(
        "Team", "User", "Project", "Repository", "Service",
        "Module", "CodeFile", "Class", "Function",
        "Dependency", "Vulnerability", "Configuration", "FeatureFlag",
        "BuildPipeline", "BuildArtifact", "Deployment", "DeploymentEnvironment",
    ),
    edges=(
        # ownership / context
        EdgeSpec(("User",), "BELONGS_TO", ("Team",)),
        EdgeSpec(("Repository",), "OWNED_BY", ("Team", "User")),
        EdgeSpec(("Repository",), "BELONGS_TO", ("Project",)),
        EdgeSpec(("Project",), "OWNED_BY", ("Team",)),
        EdgeSpec(("Repository",), "IMPLEMENTS", ("Service",)),
        # structure spine — stored child→parent, drawn parent→child
        EdgeSpec(("Module",), "PART_OF", ("Repository",), reverse=True, weight=3),
        EdgeSpec(("Configuration",), "PART_OF", ("Repository",), reverse=True),
        EdgeSpec(("CodeFile",), "COMMITTED_TO", ("Repository",), reverse=True, weight=3),
        EdgeSpec(("CodeFile",), "PART_OF", ("Module",), reverse=True, weight=3),
        EdgeSpec(("Class",), "PART_OF", ("CodeFile",), reverse=True, weight=3),
        EdgeSpec(("Class",), "BELONGS_TO", ("Module",), reverse=True),
        EdgeSpec(("Function",), "PART_OF", ("Class",), reverse=True, weight=3),
        # lateral code relationships
        EdgeSpec(("CodeFile",), "IMPORTS", ("CodeFile",)),
        EdgeSpec(("Function",), "CALLS", ("Function",)),
        EdgeSpec(("Class",), "EXTENDS", ("Class",)),
        # supply chain
        EdgeSpec(("Repository",), "DEPENDS_ON", ("Dependency",), weight=2),
        EdgeSpec(("Vulnerability",), "HAS_FINDING", ("Dependency",), reverse=True),
        # release spine
        EdgeSpec(("Repository",), "BUILT_BY", ("BuildPipeline",), weight=3),
        EdgeSpec(("BuildPipeline",), "PRODUCES", ("BuildArtifact",), weight=3),
        EdgeSpec(("BuildArtifact",), "DEPLOYED_TO", ("DeploymentEnvironment",), weight=3),
        EdgeSpec(("Deployment",), "PART_OF", ("BuildArtifact",), reverse=True),
        EdgeSpec(("Deployment",), "DEPLOYED_TO", ("DeploymentEnvironment",), weight=3),
        EdgeSpec(("Deployment",), "IMPLEMENTS", ("Service",)),
        EdgeSpec(("FeatureFlag",), "PART_OF", ("Service",), reverse=True),
    ),
    tiers={
        "Team": 0, "User": 0, "Project": 0,
        "Repository": 1, "Service": 1,
        "Module": 2, "Configuration": 2, "Dependency": 2, "BuildPipeline": 2,
        "CodeFile": 3, "BuildArtifact": 3, "Vulnerability": 3,
        "Class": 4, "Deployment": 4,
        "Function": 5, "DeploymentEnvironment": 5, "FeatureFlag": 5,
    },
    anchor_labels=("Repository", "Service"),
    kpis=(
        KpiSpec("repos", "Repositories"),
        KpiSpec("loc", "Lines of Code", "compact", "Total codebase mass"),
        KpiSpec("openPrs", "Open PRs", hint="Work in flight"),
        KpiSpec("staleRepos", "Stale >90d", hint="No commit in 90 days — likely abandoned"),
        KpiSpec("pipelineSuccess", "Pipeline Health", "percent",
                "Run-weighted mean success rate"),
        KpiSpec("avgCoverage", "Test Coverage", "percent", "Mean CodeFile coverage"),
        KpiSpec("vulnDeps", "Vuln Deps", hint="Dependencies with a known CVE"),
        KpiSpec("reposNoPipeline", "No Pipeline",
                hint="Repos with no BUILT_BY edge — untraceable to production"),
        KpiSpec("unownedRepos", "Unowned Repos",
                hint="No OWNED_BY edge", secondary=True),
        KpiSpec("unsignedArtifacts", "Unsigned Artifacts",
                hint="Supply-chain control gap", secondary=True),
        KpiSpec("deploys7d", "Deploys (7d)", secondary=True),
        KpiSpec("codeFiles", "Code Files", "compact", secondary=True),
    ),
)


# ── Infra lens ────────────────────────────────────────────────────────────────
# Containment, not flow: environment ⊃ network ⊃ cluster ⊃ host ⊃ workload.

INFRA_LENS = Lens(
    id="infra",
    name="Infra",
    description="Cloud, compute, network and identity topology",
    accent="#38bdf8",
    labels=(
        "DeploymentEnvironment", "Service", "Container",
        "KubernetesCluster", "VM", "Server",
        "CloudResource", "Network", "Database",
        "IAMRole", "IAMPolicy", "ServiceAccount",
        "Deployment", "BuildArtifact", "Infrastructure",
        "SecurityFinding", "AttackPath", "Team", "Project",
    ),
    edges=(
        # environment & network containment
        EdgeSpec(("Network",), "BELONGS_TO", ("DeploymentEnvironment",), weight=3),
        EdgeSpec(("Network",), "PART_OF", ("Network",), weight=2),          # subnet ⊂ vpc
        EdgeSpec(("Network",), "ROUTES_TO", ("Network",)),
        EdgeSpec(("Database",), "BELONGS_TO", ("DeploymentEnvironment", "Network")),
        # clusters & hosts
        EdgeSpec(("KubernetesCluster",), "DEPLOYED_TO", ("DeploymentEnvironment",), weight=3),
        EdgeSpec(("KubernetesCluster",), "CONTAINS", ("Container",), weight=2),
        EdgeSpec(("KubernetesCluster",), "HOSTS", ("Service",)),
        EdgeSpec(("VM",), "PART_OF", ("KubernetesCluster",), reverse=True, weight=2),
        EdgeSpec(("VM",), "BELONGS_TO", ("Network",)),
        EdgeSpec(("Server",), "HOSTS", ("Service",)),
        # workloads
        EdgeSpec(("Container",), "RUNS_ON", ("VM", "CloudResource"), weight=3),
        EdgeSpec(("Container",), "PART_OF", ("KubernetesCluster", "BuildArtifact"), reverse=True),
        EdgeSpec(("Container",), "IMPLEMENTS", ("Service",)),
        EdgeSpec(("Container",), "ACCESSES_AS", ("IAMRole", "ServiceAccount")),
        EdgeSpec(("Container",), "CONNECTS_TO", ("Database", "CloudResource")),
        EdgeSpec(("Container",), "EXPOSED_VIA", ("Network",)),
        # managed services
        EdgeSpec(("CloudResource",), "BELONGS_TO", ("Network", "DeploymentEnvironment")),
        EdgeSpec(("CloudResource",), "PART_OF", ("CloudResource",)),        # listener ⊂ alb
        EdgeSpec(("CloudResource",), "EXPOSED_VIA", ("Network",)),
        EdgeSpec(("CloudResource",), "ROUTES_TO", ("Service", "Container")),
        EdgeSpec(("CloudResource",), "CONTAINS", ("Database",)),
        # services
        EdgeSpec(("Service",), "RUNS_ON",
                 ("Container", "VM", "CloudResource", "Infrastructure"), weight=3),
        EdgeSpec(("Service",), "EXPOSED_VIA", ("Network",)),
        EdgeSpec(("Service",), "ACCESSES_AS", ("IAMRole", "ServiceAccount")),
        EdgeSpec(("Service",), "CONNECTS_TO", ("Database",)),
        # identity
        EdgeSpec(("IAMRole",), "GOVERNED_BY", ("IAMPolicy",)),
        EdgeSpec(("Service",), "OWNED_BY", ("Team",)),
        EdgeSpec(("Service",), "BELONGS_TO", ("Project",)),
        # delivery
        EdgeSpec(("Deployment",), "DEPLOYED_TO", ("DeploymentEnvironment",)),
        EdgeSpec(("BuildArtifact",), "DEPLOYED_TO", ("DeploymentEnvironment",)),
        # risk overlay — HAS_FINDING appears in BOTH directions in real data
        EdgeSpec(("Server", "VM", "Container", "CloudResource", "Network", "Infrastructure"),
                 "HAS_FINDING", ("SecurityFinding",)),
        EdgeSpec(("SecurityFinding",), "HAS_FINDING", ("Service",), reverse=True),
        EdgeSpec(("AttackPath",), "ROUTES_TO", ("Database", "Service")),
        EdgeSpec(("AttackPath",), "REFERENCED_BY", ("SecurityFinding",)),
    ),
    tiers={
        "DeploymentEnvironment": 0,
        "Service": 1, "Deployment": 1,
        "Container": 2, "BuildArtifact": 2,
        "KubernetesCluster": 3, "VM": 3, "Server": 3, "Infrastructure": 3,
        "CloudResource": 4, "Network": 4,
        "Database": 5,
        "IAMRole": 6, "IAMPolicy": 6, "ServiceAccount": 6,
        "SecurityFinding": 6, "AttackPath": 6,
        "Team": 7, "Project": 7,
    },
    anchor_labels=("DeploymentEnvironment", "KubernetesCluster", "Service", "Container"),
    kpis=(
        KpiSpec("environments", "Environments"),
        KpiSpec("clusters", "Clusters"),
        KpiSpec("computeNodes", "Compute Nodes", hint="VMs + physical servers"),
        KpiSpec("containerInstances", "Container Instances", hint="Sum of replicas"),
        KpiSpec("monthlyCost", "Monthly Cost", "currency",
                "Sum of CloudResource.monthlyCostUsd"),
        KpiSpec("unencrypted", "Unencrypted", hint="Resources with encryption off"),
        KpiSpec("eolServers", "EOL Servers", hint="Vendor support ends within 12 months"),
        KpiSpec("criticalFindings", "Critical Findings"),
        KpiSpec("noFlowLogs", "No Flow Logs", hint="Networks without flow logging",
                secondary=True),
        KpiSpec("unplacedResources", "Unplaced Infra",
                hint="Environment unresolvable by traversal — a data-quality signal",
                secondary=True),
        KpiSpec("unpatchedVms", "Stale Patch Level", secondary=True),
        KpiSpec("ungovernedRoles", "Ungoverned IAM Roles",
                hint="No GOVERNED_BY policy", secondary=True),
        KpiSpec("restarts", "Container Restarts", secondary=True),
    ),
)


LENSES: dict[str, Lens] = {
    GIT_LENS.id: GIT_LENS,
    INFRA_LENS.id: INFRA_LENS,
}


# ── Import-time validation ────────────────────────────────────────────────────

def _validate() -> None:
    labels = set(ALL_LABELS)
    rels = set(RELATIONSHIP_TYPES)
    for lens in LENSES.values():
        unknown = sorted(set(lens.labels) - labels)
        if unknown:
            raise ValueError(
                f"lens {lens.id!r}: labels not in schema.ALL_LABELS: {unknown}"
            )
        missing_tier = sorted(set(lens.labels) - set(lens.tiers))
        if missing_tier:
            raise ValueError(
                f"lens {lens.id!r}: labels with no tier assigned: {missing_tier}"
            )
        stray_tier = sorted(set(lens.tiers) - set(lens.labels))
        if stray_tier:
            raise ValueError(
                f"lens {lens.id!r}: tiers for labels not in the lens: {stray_tier}"
            )
        bad_anchor = sorted(set(lens.anchor_labels) - set(lens.labels))
        if bad_anchor:
            raise ValueError(
                f"lens {lens.id!r}: anchor labels not in the lens: {bad_anchor}"
            )
        for e in lens.edges:
            if e.rel_type not in rels:
                raise ValueError(
                    f"lens {lens.id!r}: {e.rel_type!r} not in schema.RELATIONSHIP_TYPES"
                )
            endpoints = set(e.from_labels) | set(e.to_labels)
            outside = sorted(endpoints - set(lens.labels))
            if outside:
                raise ValueError(
                    f"lens {lens.id!r}: edge {e.rel_type} references labels "
                    f"outside the lens: {outside}"
                )


_validate()
