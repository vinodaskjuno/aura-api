"""Migration profiles — one per source→target pair, plus a generic fallback.

A profile is a *declarative* description of how one platform maps onto another:
which files identify the source, how its component types correspond to the target's,
what reliably does not port, and what to ask before proposing anything.

Two paths, deliberately:

  * A **curated** profile exists for a pair someone has thought about. The mapping
    table it produces is predictable, and the risks it lists are the ones that
    actually bite — not whatever a model recalled about the platform.

  * **GENERIC** is the profile with everything empty. The strategy agent then works
    from the knowledge graph and the source files unaided.

Keeping the fallback is what makes "any stack to any stack" a fact rather than a
brochure line. A pair nobody has curated still migrates; it just migrates with less
help. And because the registry is data, adding a pair never touches a router or a
component — the same reason lens definitions live in `src/ontology/lenses.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MigrationProfile:
    source: str
    target: str
    label: str
    # Filename globs that identify the source platform in an uploaded tree. Used to
    # SUGGEST the source, never to enforce it — a customer's export may be arranged
    # in a way nobody anticipated, and refusing to proceed on that basis would be
    # worse than proceeding with a source the user confirmed by hand.
    detect: tuple[str, ...] = ()
    # source component type -> target component type
    component_map: dict[str, str] = field(default_factory=dict)
    # Things that do not port. Fed to the strategy agent so its `drop` and `manual`
    # verdicts start from known truth rather than being invented each run.
    known_risks: tuple[str, ...] = ()
    # Asked before a strategy is proposed. These are the questions whose answers
    # change the output — not a questionnaire for its own sake.
    questions: tuple[str, ...] = ()
    # Where generated files go, by kind.
    target_layout: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}->{self.target}"

    @property
    def curated(self) -> bool:
        """False for GENERIC. The UI says which path a user is watching."""
        return bool(self.component_map or self.known_risks)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "curated": self.curated,
            "componentMap": dict(self.component_map),
            "knownRisks": list(self.known_risks),
            "questions": list(self.questions),
            "targetLayout": dict(self.target_layout),
        }


# ── Generic fallback ─────────────────────────────────────────────────────────

GENERIC = MigrationProfile(
    source="*",
    target="*",
    label="Generic (knowledge-graph led)",
    questions=(
        "What does this application do, in one sentence, for someone who has never "
        "seen it?",
        "Which parts are you planning to retire rather than migrate?",
        "Are there components whose behaviour nobody currently understands?",
    ),
)


# ── Curated profiles ─────────────────────────────────────────────────────────

WORKFUSION_TO_AIRFLOW = MigrationProfile(
    source="workfusion",
    target="airflow",
    label="WorkFusion → Apache Airflow",
    detect=(
        "**/*.bpmn", "**/*.bpmn20.xml",
        "**/config.xml", "**/bot-config.xml",
        "**/*.groovy",
    ),
    component_map={
        # BPMN is the process definition; WorkFusion builds on Activiti, so the
        # element names are BPMN 2.0's.
        "process":       "DAG",
        "serviceTask":   "PythonOperator",
        "scriptTask":    "PythonOperator",
        "userTask":      "manual checkpoint (no Airflow equivalent)",
        "sequenceFlow":  "task dependency",
        "exclusiveGateway": "BranchPythonOperator",
        "parallelGateway":  "parallel task group",
        "timerEventDefinition": "DAG schedule",
        "subProcess":    "TaskGroup",
        "callActivity":  "TriggerDagRunOperator",
        "startEvent":    "DAG entry point",
        "endEvent":      "DAG completion",
        # An externally-started process becomes a DAG with no schedule, triggered by
        # the API or a Dataset — not a scheduled one with a sensor bolted on.
        "messageEventDefinition": "externally triggered DAG (schedule=None)",
        # A parallel multi-instance loop is Airflow's dynamic task mapping. Worth
        # naming explicitly: the naive translation is a static fan-out, which loses
        # the "however many there are today" behaviour that made it a loop.
        "multiInstanceLoopCharacteristics": "dynamic task mapping (.expand())",
        # WorkFusion-specific.
        "botTask":       "PythonOperator wrapping the bot's logic",
        "mlTask":        "no equivalent — see risks",
        "ocrTask":       "no equivalent — see risks",
    },
    known_risks=(
        "Attended (human-in-the-loop) bots have no Airflow equivalent. Airflow is an "
        "unattended scheduler; a userTask becomes a checkpoint that pauses a pipeline "
        "and waits for an external signal, which is a redesign rather than a port.",
        "WorkFusion's OCR and ML/cognitive tasks are platform features, not code. "
        "There is nothing to translate — they must be replaced by a separate service "
        "and that replacement is out of scope for a code migration.",
        "WorkFusion recorder scripts capture UI interactions against a specific screen "
        "layout. They do not survive being moved and generally cannot be automated at "
        "all in Airflow.",
        "Long-running WorkFusion processes rely on the engine holding state between "
        "steps. Airflow tasks are independent processes, so anything relying on "
        "in-memory continuity needs an explicit external store.",
        "WorkFusion's built-in retry and error-handling semantics differ from Airflow's "
        "retries/`on_failure_callback`. A literal translation changes failure behaviour "
        "in ways that are easy to miss until production.",
    ),
    questions=(
        "Which of these processes are attended (a person acts mid-run) versus fully "
        "unattended? Attended ones cannot be ported as-is.",
        "Are any processes using WorkFusion's OCR or ML/cognitive tasks? If so, what "
        "should replace them?",
        "What triggers each process today — a schedule, a queue, a file arriving, or a "
        "person? Airflow needs this stated explicitly.",
        "Where does the process keep state between steps, and can that move to an "
        "external store?",
        "Are there processes you intend to retire rather than migrate?",
    ),
    target_layout={
        "dag":      "dags/{name}.py",
        "operator": "plugins/operators/{name}.py",
        "shared":   "plugins/shared/{name}.py",
        "config":   "config/{name}.yaml",
        "docs":     "docs/{name}.md",
        "deps":     "requirements.txt",
    },
)


PROFILES: tuple[MigrationProfile, ...] = (
    WORKFUSION_TO_AIRFLOW,
)


def profile_for(source: str, target: str) -> MigrationProfile:
    """The curated profile for this pair, or GENERIC.

    Never returns None. A missing profile is a normal condition — it means nobody has
    curated this pair yet, not that the migration cannot proceed.
    """
    src = (source or "").strip().lower()
    tgt = (target or "").strip().lower()
    for profile in PROFILES:
        if profile.source == src and profile.target == tgt:
            return profile
    return GENERIC


def known_pairs() -> list[dict]:
    """Curated pairs, for the target dropdown."""
    return [p.as_dict() for p in PROFILES]


def known_targets() -> list[str]:
    """Every target any profile can reach, plus the ones the generic path handles.

    The generic entries matter: a dropdown listing only curated targets would make
    the product look narrower than it is.
    """
    curated = {p.target for p in PROFILES}
    return sorted(curated | {
        "airflow", "step-functions", "dagster", "prefect", "temporal",
        "spring-boot", "fastapi", "dotnet", "aws-lambda", "kubernetes-jobs",
    })
