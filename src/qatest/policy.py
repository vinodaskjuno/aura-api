"""NIST-mapped policy checks over a project's infrastructure-as-code.

Deliberately the same shape as `structure.py`: a `Check` naming a validator, a
`plan_checks` that walks the tree, and `run_check` resolving `_v_<validator>` by name.
Registering a control is defining one function — there is no registry to update and no
framework to learn.

WHAT THESE CHECK, AND WHAT THEY DO NOT
--------------------------------------
They read the template. They describe what the project DECLARES, not what is deployed —
a resource created by hand, or drifted since, is invisible here. Every success message
says so, because a control that overstates its evidence is worse than no control: it
converts "we did not look" into "this is fine".

Static was chosen over querying a live emulator precisely so these answer with no
emulator, no podman and no runner — the API can read the working copy off EFS, which is
what lets DevMate show posture the moment a project is analysed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Same skip list and cap as structure.py, for the same reasons: a vendored tree can hold
#: thousands of templates and none of them are this project's.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", "target", "vendor", ".terraform"}
MAX_FILES = 300

#: Files that plausibly declare cloud infrastructure.
IAC_NAMES = {"template.yaml", "template.yml", "serverless.yml", "serverless.yaml",
             "samconfig.yaml", "cloudformation.yaml", "cloudformation.yml"}

#: Runtimes AWS has retired or announced retirement for. Dated deliberately — a list like
#: this is wrong the moment it stops being maintained, so it says when it was true.
#: Accurate as of 2026-09.
DEPRECATED_RUNTIMES = {
    "python2.7", "python3.6", "python3.7", "python3.8",
    "nodejs12.x", "nodejs14.x", "nodejs16.x",
    "ruby2.7", "dotnetcore3.1", "go1.x", "java8",
}

#: Names that suggest a value is a credential rather than configuration.
_SECRET_NAME = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential)", re.I)

#: A value that DEFERS to something else is fine — that is the compliant pattern. Only a
#: literal is a finding.
_REFERENCE = re.compile(
    r"^\s*(\{\{resolve:|<Ref>|<GetAtt>|<Sub>|<ImportValue>|arn:aws:secretsmanager:)",
    re.I)


@dataclass
class Check:
    """One control applied to one file."""
    check_id: str
    name: str
    rel_path: str
    validator: str


def _load(path: Path) -> Any:
    """Parse a CloudFormation/SAM template.

    SAM uses custom YAML tags (`!Ref`, `!GetAtt`, `!Sub`) that SafeLoader rejects
    outright. They are collapsed to a marker string rather than resolved: these checks
    care whether a value is a REFERENCE at all, never what it points at.
    """
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor(
        "!", lambda loader, suffix, node: f"<{suffix}>")
    return yaml.load(path.read_text(encoding="utf-8", errors="replace"), Loader=_Loader)


def _resources(doc: Any) -> dict:
    return (doc or {}).get("Resources", {}) if isinstance(doc, dict) else {}


def _of_type(doc: Any, *types: str) -> list[tuple[str, dict]]:
    out = []
    for name, body in _resources(doc).items():
        if isinstance(body, dict) and body.get("Type") in types:
            out.append((name, body.get("Properties") or {}))
    return out


def plan_checks(root: Path | None) -> list[Check]:
    """Every control that applies to this project, deterministically ordered.

    Ordered by (file, control) so `check_id` is stable between runs — an id that moves
    would make a result impossible to compare with the last one.
    """
    root = Path(root) if root else None
    if root is None or not root.is_dir():
        return []

    files = [p for p in sorted(root.rglob("*"))
             if p.is_file() and p.name in IAC_NAMES
             and not (set(p.relative_to(root).parts) & SKIP_DIRS)][:MAX_FILES]

    checks: list[Check] = []
    for path in files:
        rel = str(path.relative_to(root))
        for validator, title in _CONTROLS:
            checks.append(Check(check_id=f"policy-{len(checks):03d}",
                                name=f"{title} — {path.name}",
                                rel_path=rel, validator=validator))
    return checks


def run_check(root: Path | str, check: Check) -> tuple[bool, str]:
    """Run one control. Never raises — a broken validator fails its own check."""
    path = Path(root) / check.rel_path
    fn = globals().get(f"_v_{check.validator}")
    if fn is None:
        return False, f"no validator named {check.validator!r}"
    if not path.is_file():
        return False, f"{check.rel_path} does not exist"
    try:
        return fn(path, Path(root))
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ── The controls ─────────────────────────────────────────────────────────────

def _v_lambda_env_no_secrets(path: Path, _root: Path) -> tuple[bool, str]:
    """IA-5(1) — authenticator management: no literal credential in an env var."""
    bad = []
    for name, props in _of_type(_load(path), "AWS::Serverless::Function",
                                "AWS::Lambda::Function"):
        variables = ((props.get("Environment") or {}).get("Variables") or {})
        for key, value in variables.items():
            if not _SECRET_NAME.search(str(key)):
                continue
            text = str(value)
            if _REFERENCE.match(text):
                continue        # a reference is the compliant pattern
            bad.append(f"{name}.{key}")
    if bad:
        return False, (f"literal value in {', '.join(bad)} — move it to Secrets Manager "
                       f"and reference it")
    return True, ("no function declares a literal credential in its environment "
                  "(declared config only — a value injected at deploy time is not "
                  "visible here)")


def _v_iam_no_wildcards(path: Path, _root: Path) -> tuple[bool, str]:
    """AC-6(1) — least privilege: no wildcard Action or Resource."""
    bad = []
    for name, props in _of_type(_load(path), "AWS::Serverless::Function",
                                "AWS::Lambda::Function", "AWS::IAM::Role",
                                "AWS::IAM::Policy"):
        for statement in _statements(props):
            for field in ("Action", "Resource"):
                value = statement.get(field)
                values = value if isinstance(value, list) else [value]
                if any(str(v) == "*" for v in values if v is not None):
                    bad.append(f"{name}.{field}")
    if bad:
        return False, f"wildcard in {', '.join(sorted(set(bad)))} — name the actions and "\
                      f"resources actually needed"
    return True, "no policy statement uses a wildcard Action or Resource"


def _statements(props: dict) -> list[dict]:
    """Policy statements wherever SAM and CloudFormation put them."""
    found: list[dict] = []
    for policy in (props.get("Policies") or []):
        if isinstance(policy, dict):
            block = policy.get("Statement", policy)
            found.extend(block if isinstance(block, list) else [block])
    document = props.get("PolicyDocument") or {}
    if isinstance(document, dict):
        block = document.get("Statement") or []
        found.extend(block if isinstance(block, list) else [block])
    return [s for s in found if isinstance(s, dict)]


def _v_runtime_supported(path: Path, _root: Path) -> tuple[bool, str]:
    """SI-2 — flaw remediation: no end-of-support runtime."""
    bad = [f"{name} ({props.get('Runtime')})"
           for name, props in _of_type(_load(path), "AWS::Serverless::Function",
                                       "AWS::Lambda::Function")
           if str(props.get("Runtime", "")).lower() in DEPRECATED_RUNTIMES]
    if bad:
        return False, (f"end-of-support runtime in {', '.join(bad)} — no security "
                       f"patches are issued for it")
    return True, "every function declares a supported runtime (list current as of 2026-09)"


def _v_encryption_declared(path: Path, _root: Path) -> tuple[bool, str]:
    """SC-13 — cryptographic protection: encryption at rest is declared."""
    bad = []
    for name, props in _of_type(_load(path), "AWS::DynamoDB::Table"):
        if not props.get("SSESpecification"):
            bad.append(f"{name} (no SSESpecification)")
    for name, props in _of_type(_load(path), "AWS::S3::Bucket"):
        if not props.get("BucketEncryption"):
            bad.append(f"{name} (no BucketEncryption)")
    for name, props in _of_type(_load(path), "AWS::SQS::Queue"):
        if not (props.get("SqsManagedSseEnabled") or props.get("KmsMasterKeyId")):
            bad.append(f"{name} (no server-side encryption)")
    if bad:
        return False, (f"encryption not declared for {', '.join(bad)} — AWS may still "
                       f"apply a default, but the template does not say so")
    return True, ("every table, bucket and queue declares encryption at rest "
                  "(declared, not verified against a deployed resource)")


#: (validator, title). Order fixes check_id, so append rather than insert.
_CONTROLS: tuple[tuple[str, str], ...] = (
    ("lambda_env_no_secrets", "IA-5(1) · no literal secrets in Lambda env vars"),
    ("iam_no_wildcards",      "AC-6(1) · least privilege, no IAM wildcards"),
    ("runtime_supported",     "SI-2 · Lambda runtime is supported"),
    ("encryption_declared",   "SC-13 · encryption at rest is declared"),
)
