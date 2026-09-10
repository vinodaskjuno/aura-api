"""Infer which internal components an organisation already standardises on.

The target of a migration is never just "Airflow". It is Airflow plus whatever this
organisation uses for secrets, logging, APM and the rest — and code generated against
the wrong ones is code nobody can merge.

Asking the user to state all of it per migration is twenty dropdowns of tedium they
will rush. But Aura has already parsed the estate into the knowledge graph, and a
dependency manifest is a fairly direct statement of what a team uses: a service that
imports `hvac` is talking to Vault.

So the standards are INFERRED and then CONFIRMED. Both halves matter:

  * Inferred, because the graph already knows and re-typing it is waste.
  * Confirmed, because this is a heuristic over package names. `boto3` appears in
    almost every Python service on AWS and says nothing on its own about secrets
    management; a library can be a leftover; a minority usage is not a standard.

Which is why every candidate carries the NUMBER OF SERVICES it was found in. "Splunk
in 12 of 14" is a standard. "Splunk in 1 of 14" is someone's experiment, and the
difference is invisible unless the count is on screen.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# The roles a migration has to fill in the target. Ordered by how often getting one
# wrong makes generated code unusable.
CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("secrets",    "Secrets & credentials"),
    ("logging",    "Logging"),
    ("apm",        "Monitoring / APM"),
    ("tracing",    "Distributed tracing"),
    ("config",     "Configuration"),
    ("messaging",  "Messaging / queues"),
    ("storage",    "Object storage"),
    ("auth",       "Authentication"),
    ("scheduling", "Scheduling / orchestration"),
)

CAPABILITY_LABELS = dict(CAPABILITIES)


@dataclass(frozen=True)
class Signature:
    """Evidence that a technology is in use.

    `deps` alone is enough for a package that means one thing and nothing else —
    `hvac` is a Vault client and not much use for anything but Vault.

    `hints` exists for the packages that do not: `boto3` is how a Python service
    talks to all of AWS, so reading it as "AWS Secrets Manager" would announce a
    standard on the strength of a dependency almost every service has. When `hints`
    is set, one of those strings must ALSO appear somewhere in the evidence text
    before the candidate is offered.
    """
    capability: str
    technology: str
    deps: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    note: str = ""

    def matches(self, dep_name: str) -> bool:
        name = (dep_name or "").strip().lower()
        if not name:
            return False
        return any(d in name for d in self.deps)


SIGNATURES: tuple[Signature, ...] = (
    # ── Secrets ──
    Signature("secrets", "HashiCorp Vault", deps=("hvac", "vault", "spring-cloud-vault")),
    Signature("secrets", "AWS Secrets Manager", deps=("boto3", "aws-sdk", "aws-secretsmanager"),
              hints=("secretsmanager", "secrets_manager", "getsecretvalue"),
              note="boto3 alone is not evidence — needs a Secrets Manager reference."),
    Signature("secrets", "Azure Key Vault", deps=("azure-keyvault", "azure-identity")),
    Signature("secrets", "CyberArk", deps=("cyberark", "conjur")),

    # ── Logging ──
    Signature("logging", "Splunk", deps=("splunk-sdk", "splunk-handler", "splunk_handler",
                                         "splunk-logging")),
    Signature("logging", "Elastic / ELK", deps=("elasticsearch", "logstash", "ecs-logging")),
    Signature("logging", "Datadog Logs", deps=("datadog-api-client",)),
    Signature("logging", "Loki", deps=("python-logging-loki", "loki")),

    # ── APM ──
    Signature("apm", "Dynatrace", deps=("autodynatrace", "oneagent", "dynatrace")),
    Signature("apm", "Datadog APM", deps=("ddtrace",)),
    Signature("apm", "New Relic", deps=("newrelic",)),
    Signature("apm", "AppDynamics", deps=("appdynamics",)),

    # ── Tracing ──
    Signature("tracing", "OpenTelemetry", deps=("opentelemetry-sdk", "opentelemetry-api",
                                                "opentelemetry-instrumentation")),
    Signature("tracing", "Jaeger", deps=("jaeger-client",)),

    # ── Config ──
    Signature("config", "Consul", deps=("python-consul", "consul")),
    Signature("config", "Spring Cloud Config", deps=("spring-cloud-config",)),
    Signature("config", "AWS AppConfig", deps=("boto3",), hints=("appconfig",)),

    # ── Messaging ──
    Signature("messaging", "Apache Kafka", deps=("kafka-python", "confluent-kafka",
                                                 "spring-kafka")),
    Signature("messaging", "RabbitMQ", deps=("pika", "amqp", "spring-amqp")),
    Signature("messaging", "AWS SQS", deps=("boto3",), hints=("sqs",)),
    Signature("messaging", "IBM MQ", deps=("pymqi", "ibmmq")),

    # ── Storage ──
    Signature("storage", "AWS S3", deps=("boto3",), hints=("s3", "bucket")),
    Signature("storage", "Azure Blob Storage", deps=("azure-storage-blob",)),
    Signature("storage", "MinIO", deps=("minio",)),

    # ── Auth ──
    Signature("auth", "Okta", deps=("okta",)),
    Signature("auth", "Keycloak", deps=("python-keycloak", "keycloak")),
    Signature("auth", "Microsoft Entra ID", deps=("msal", "azure-identity")),
    Signature("auth", "LDAP / Active Directory", deps=("ldap3", "python-ldap",
                                                       "spring-security-ldap")),

    # ── Scheduling ──
    Signature("scheduling", "Apache Airflow", deps=("apache-airflow",)),
    Signature("scheduling", "Quartz", deps=("quartz",)),
    Signature("scheduling", "Celery", deps=("celery",)),
)


@dataclass
class Candidate:
    """One inferred technology, with the evidence behind it."""
    capability: str
    technology: str
    services: int = 0
    total_services: int = 0
    evidence: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def confidence(self) -> str:
        """A word for how much of the estate agrees.

        Deliberately coarse. A percentage implies a precision this does not have —
        it is a count of services whose dependency list mentioned a package.
        """
        if not self.total_services:
            return "unknown"
        share = self.services / self.total_services
        if share >= 0.6:
            return "strong"
        if share >= 0.25:
            return "mixed"
        return "weak"

    def as_dict(self) -> dict:
        return {
            "capability": self.capability,
            "capabilityLabel": CAPABILITY_LABELS.get(self.capability, self.capability),
            "technology": self.technology,
            "services": self.services,
            "totalServices": self.total_services,
            "confidence": self.confidence,
            "evidence": self.evidence[:8],
            "note": self.note,
        }


# ── Inference ────────────────────────────────────────────────────────────────

_DEP_QUERY = """
MATCH (r:Repository)-[:DEPENDS_ON]->(d:Dependency)
WHERE r.projectId = $projectId OR r.externalId STARTS WITH $prefix
RETURN d.name AS name, coalesce(d.ecosystem, '') AS ecosystem,
       count(DISTINCT r) AS repos
"""

_REPO_COUNT_QUERY = """
MATCH (r:Repository)
WHERE r.projectId = $projectId OR r.externalId STARTS WITH $prefix
RETURN count(DISTINCT r) AS total
"""


def infer(project_id: str, extra_evidence: str = "") -> list[dict]:
    """Candidate technologies per capability, strongest first within each.

    `extra_evidence` is any additional text to test `hints` against — config file
    contents, for instance. Without it, a signature that requires a hint can never
    fire from dependency names alone, which is the intended conservative default.

    Returns [] rather than raising when the graph is unreachable: an inference step
    that cannot run should leave the user with an empty form to fill in, not a
    broken screen.
    """
    from src.graph import neo4j_client as neo4j

    prefix = f"repo:{project_id}"
    try:
        rows = neo4j.run_query(_DEP_QUERY, {"projectId": project_id, "prefix": prefix})
        totals = neo4j.run_query(_REPO_COUNT_QUERY, {"projectId": project_id, "prefix": prefix})
    except Exception as exc:  # noqa: BLE001 — an empty form beats a broken screen
        log.warning("capability inference failed for %s: %s", project_id, exc)
        return []

    total_services = int((totals or [{}])[0].get("total") or 0)
    haystack = (extra_evidence or "").lower()

    found: dict[tuple[str, str], Candidate] = {}
    for row in rows or []:
        dep_name = str(row.get("name") or "")
        repos = int(row.get("repos") or 0)
        for sig in SIGNATURES:
            if not sig.matches(dep_name):
                continue
            # A signature with hints needs corroboration — see the note on boto3.
            if sig.hints and not any(h in haystack for h in sig.hints):
                continue
            key = (sig.capability, sig.technology)
            candidate = found.get(key)
            if candidate is None:
                candidate = Candidate(
                    capability=sig.capability, technology=sig.technology,
                    total_services=total_services, note=sig.note)
                found[key] = candidate
            candidate.services = max(candidate.services, repos)
            label = f"{dep_name} ({row.get('ecosystem') or 'dep'}) in {repos} repo(s)"
            if label not in candidate.evidence:
                candidate.evidence.append(label)

    ordered = sorted(found.values(),
                     key=lambda c: (c.capability, -c.services, c.technology))
    return [c.as_dict() for c in ordered]


def default_mapping(candidates: list[dict]) -> list[dict]:
    """Pre-select the strongest candidate per capability, and say so.

    Every capability appears, including ones nothing was inferred for — a missing row
    is indistinguishable from a row nobody thought about, and the user needs to see
    the gap in order to fill it.
    """
    best: dict[str, dict] = {}
    for c in candidates:
        cap = c["capability"]
        if cap not in best or c["services"] > best[cap]["services"]:
            best[cap] = c

    mapping: list[dict] = []
    for cap, label in CAPABILITIES:
        chosen = best.get(cap)
        mapping.append({
            "capability": cap,
            "capabilityLabel": label,
            "technology": chosen["technology"] if chosen else "",
            # Where this value came from. A strategy that cannot say why it targets
            # Vault is a strategy a reviewer will not sign.
            "origin": "inferred" if chosen else "unset",
            "services": chosen["services"] if chosen else 0,
            "totalServices": chosen["totalServices"] if chosen else 0,
            "confidence": chosen["confidence"] if chosen else "unknown",
            "evidence": chosen["evidence"] if chosen else [],
        })
    return mapping
