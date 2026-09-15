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


@dataclass(frozen=True)
class Finding:
    """One control's verdict on ONE resource.

    Field shape follows `doctor.Finding` (doctor.py:34-62) rather than inventing a second
    vocabulary inside the same package: a stable id, a boolean, and text that says what
    was checked and what to do about it.

    `remedy` is the fix in the reader's own terms. A finding that says what is wrong and
    not what to do is a chore rather than a control, and the compliant sibling in the same
    template is usually the best example available.
    """
    resource: str
    resource_type: str
    ok: bool
    detail: str
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"resource": self.resource, "resourceType": self.resource_type,
                "ok": self.ok, "detail": self.detail, "remedy": self.remedy}


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


def _of_type(doc: Any, *types: str) -> list[tuple[str, dict, str]]:
    """Resources of the given types, as (logical name, properties, Type).

    Carries the Type because the per-resource view labels rows with it, and because
    which controls apply to a resource is decided by it.
    """
    out = []
    for name, body in _resources(doc).items():
        if isinstance(body, dict) and body.get("Type") in types:
            out.append((name, body.get("Properties") or {}, str(body.get("Type"))))
    return out


def _iac_files(root: Path) -> list[Path]:
    """The IaC files in this tree, deterministically ordered and capped.

    Shared by `plan_checks` and `resource_view` so the two cannot disagree about which
    files count — a resource that appeared in one and not the other would be a hole
    nobody could see.
    """
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and p.name in IAC_NAMES
            and not (set(p.relative_to(root).parts) & SKIP_DIRS)][:MAX_FILES]


def plan_checks(root: Path | None) -> list[Check]:
    """Every control that applies to this project, deterministically ordered.

    Ordered by (file, control) so `check_id` is stable between runs — an id that moves
    would make a result impossible to compare with the last one.
    """
    root = Path(root) if root else None
    if root is None or not root.is_dir():
        return []

    files = _iac_files(root)

    checks: list[Check] = []
    for path in files:
        rel = str(path.relative_to(root))
        for validator, title in _CONTROLS:
            checks.append(Check(check_id=f"policy-{len(checks):03d}",
                                name=f"{title} — {path.name}",
                                rel_path=rel, validator=validator))
    return checks


def findings_for(root: Path | str, check: Check) -> list[Finding]:
    """Every resource this control looked at, and how each one landed.

    The per-resource truth. `run_check` folds it into the one verdict the case model
    carries; the DevMate endpoint returns it whole, because a reader asking "is
    AuditFunction safe to ship?" should not have to pivot four control results in their
    head.

    Never raises: a broken validator fails its own check and not the run. The failure is
    reported as a single synthetic finding so callers need only handle one shape.
    """
    path = Path(root) / check.rel_path
    fn = globals().get(f"_v_{check.validator}")
    if fn is None:
        return [_broken(f"no validator named {check.validator!r}")]
    if not path.is_file():
        return [_broken(f"{check.rel_path} does not exist")]
    try:
        return list(fn(path, Path(root)))
    except Exception as exc:                                  # noqa: BLE001
        return [_broken(f"{type(exc).__name__}: {exc}")]


def _broken(detail: str) -> Finding:
    """A control that could not run. Attributed to no resource, because none was read."""
    return Finding(resource="", resource_type="", ok=False, detail=detail)


def run_check(root: Path | str, check: Check) -> tuple[bool, str]:
    """Run one control and fold it to a single verdict. Never raises.

    Kept at this signature on purpose. `Case` has no structured slot, `check_id` is
    positional and pinned stable by a test, and `Step.error` is capped at 2000 characters
    (runner.py:69) — so the case path wants a summary, not a list. The summary now carries
    SCALE, which the previous bare fail destroyed: nine clean functions and one offender
    used to read exactly like ten offenders.
    """
    findings = findings_for(root, check)
    if not findings:
        # Nothing of this control's type is declared. Vacuously true, and it says so —
        # "no resource of this kind" is a different fact from "all of them passed".
        return True, "no resource of this kind is declared in this file"

    failed = [f for f in findings if not f.ok]
    if not failed:
        return True, f"{len(findings)} of {len(findings)} — {findings[0].detail}"
    named = ", ".join(f.detail for f in failed)
    if len(findings) == 1 and not failed[0].resource:
        return False, failed[0].detail          # the control itself could not run
    return False, (f"{len(failed)} of {len(findings)} — {named}")


# ── The controls ─────────────────────────────────────────────────────────────

def _v_lambda_env_no_secrets(path: Path, _root: Path) -> list[Finding]:
    """IA-5(1) — authenticator management: no literal credential in an env var."""
    out: list[Finding] = []
    for name, props, rtype in _of_type(_load(path), "AWS::Serverless::Function",
                                       "AWS::Lambda::Function"):
        variables = ((props.get("Environment") or {}).get("Variables") or {})
        bad = []
        for key, value in variables.items():
            if not _SECRET_NAME.search(str(key)):
                continue
            if _REFERENCE.match(str(value)):
                continue            # a reference is the compliant pattern
            bad.append(str(key))
        if bad:
            out.append(Finding(
                name, rtype, False,
                f"literal value in {', '.join(f'{name}.{k}' for k in bad)}",
                remedy="move it to Secrets Manager and reference it: "
                       "'{{resolve:secretsmanager:<name>:SecretString:<key>}}'"))
        else:
            # The success message names its own limit. This reads DECLARED config, so a
            # value injected at deploy time is invisible to it — a control that
            # overstates its evidence turns "we did not look" into "this is fine".
            secret_keys = [k for k in variables if _SECRET_NAME.search(str(k))]
            out.append(Finding(
                name, rtype, True,
                (f"{name}.{secret_keys[0]} resolves from a reference" if secret_keys
                 else f"{name} declares no credential-shaped variable")
                + " (declared config only — a value injected at deploy time is not "
                  "visible here)"))
    return out


def _v_iam_no_wildcards(path: Path, _root: Path) -> list[Finding]:
    """AC-6(1) — least privilege: no wildcard Action or Resource."""
    out: list[Finding] = []
    for name, props, rtype in _of_type(_load(path), "AWS::Serverless::Function",
                                       "AWS::Lambda::Function", "AWS::IAM::Role",
                                       "AWS::IAM::Policy"):
        bad = []
        for statement in _statements(props):
            for field in ("Action", "Resource"):
                value = statement.get(field)
                values = value if isinstance(value, list) else [value]
                if any(str(v) == "*" for v in values if v is not None):
                    bad.append(field)
        if bad:
            out.append(Finding(
                name, rtype, False,
                f"wildcard in {', '.join(f'{name}.{f}' for f in sorted(set(bad)))}",
                remedy="name the actions and resources actually needed"))
        else:
            out.append(Finding(
                name, rtype, True,
                "names the actions and resources it needs" if _statements(props)
                else "declares no inline policy statement"))
    return out


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


def _v_runtime_supported(path: Path, _root: Path) -> list[Finding]:
    """SI-2 — flaw remediation: no end-of-support runtime."""
    out: list[Finding] = []
    for name, props, rtype in _of_type(_load(path), "AWS::Serverless::Function",
                                       "AWS::Lambda::Function"):
        runtime = str(props.get("Runtime", ""))
        if runtime.lower() in DEPRECATED_RUNTIMES:
            out.append(Finding(
                name, rtype, False,
                f"{name} runs {runtime}, which is end-of-support",
                remedy="move to a current runtime — no security patches are issued "
                       "for this one"))
        else:
            # Says what it verified AND its limit: the list is dated, and a list like
            # this is wrong the moment it stops being maintained.
            out.append(Finding(name, rtype, True,
                               f"{runtime or 'no runtime declared'} "
                               f"(supported as of 2026-09)"))
    return out


def _v_encryption_declared(path: Path, _root: Path) -> list[Finding]:
    """SC-13 — cryptographic protection: encryption at rest is declared."""
    #: How each store declares it, and what to say when it does not.
    DECLARES = {
        "AWS::DynamoDB::Table": ("SSESpecification", lambda p: bool(p.get("SSESpecification"))),
        "AWS::S3::Bucket": ("BucketEncryption", lambda p: bool(p.get("BucketEncryption"))),
        "AWS::SQS::Queue": ("SqsManagedSseEnabled",
                            lambda p: bool(p.get("SqsManagedSseEnabled")
                                           or p.get("KmsMasterKeyId"))),
    }
    out: list[Finding] = []
    doc = _load(path)
    for name, props, rtype in _of_type(doc, *DECLARES):
        prop_name, declared = DECLARES[rtype]
        if declared(props):
            # Names its own limit. This reads a template, so it can only ever report
            # what is DECLARED — a resource created by hand, or drifted since, is
            # invisible here, and a control that overstates its evidence is worse than
            # no control.
            out.append(Finding(name, rtype, True,
                               f"{prop_name} declared in the template; not verified "
                               f"against a deployed resource"))
        else:
            out.append(Finding(
                name, rtype, False, f"{name} declares no {prop_name}",
                remedy=f"add {prop_name} — AWS may still apply a default, but the "
                       f"template does not say so"))
    return out


#: (validator, title). Order fixes check_id, so append rather than insert.
_CONTROLS: tuple[tuple[str, str], ...] = (
    ("lambda_env_no_secrets", "IA-5(1) · no literal secrets in Lambda env vars"),
    ("iam_no_wildcards",      "AC-6(1) · least privilege, no IAM wildcards"),
    ("runtime_supported",     "SI-2 · Lambda runtime is supported"),
    ("encryption_declared",   "SC-13 · encryption at rest is declared"),
)


#: Every resource type any control reads. A type absent from this set is one nothing
#: assesses — the per-resource view must say so rather than leave the resource out, and
#: the two are different claims.
def _covered_types() -> set[str]:
    return {"AWS::Serverless::Function", "AWS::Lambda::Function", "AWS::IAM::Role",
            "AWS::IAM::Policy", "AWS::DynamoDB::Table", "AWS::S3::Bucket",
            "AWS::SQS::Queue"}


def resource_view(root: Path | str) -> list[dict]:
    """Every declared resource, with the controls that apply to it and how each landed.

    The pivot. Same checks, same results — asked "what is wrong with AuditFunction?"
    instead of "how did IA-5(1) do?".

    A resource NO control reads is included with an empty `controls` list and
    `applicable: 0`. Leaving it out, or showing it as passing, would be the panel
    vouching for something it never examined — the same distinction
    `get_project_policy` already draws for a project with no IaC at all.
    """
    root = Path(root) if root else None
    if root is None or not root.is_dir():
        return []

    titles = dict(_CONTROLS)
    # {(file, resource): row}, in first-seen order so the view is stable between reads.
    rows: dict[tuple[str, str], dict] = {}

    def row_for(rel: str, name: str, rtype: str) -> dict:
        key = (rel, name)
        if key not in rows:
            rows[key] = {"name": name, "type": rtype, "file": rel,
                         "applicable": 0, "passed": 0, "controls": []}
        return rows[key]

    for check in plan_checks(root):
        title = titles.get(check.validator, check.validator)
        for finding in findings_for(root, check):
            if not finding.resource:
                continue                # the control itself could not run
            row = row_for(check.rel_path, finding.resource, finding.resource_type)
            row["controls"].append({"id": check.validator,
                                    "title": title,
                                    "ok": finding.ok,
                                    "detail": finding.detail,
                                    "remedy": finding.remedy})
            row["applicable"] += 1
            row["passed"] += 1 if finding.ok else 0

    # Then the ones nothing looked at, so they are present and visibly unassessed.
    covered = _covered_types()
    for path in _iac_files(root):
        rel = str(path.relative_to(root))
        for name, body in (_resources(_load_safe(path)) or {}).items():
            rtype = str(body.get("Type", "")) if isinstance(body, dict) else ""
            if rtype in covered or (rel, name) in rows:
                continue
            rows[(rel, name)] = {"name": name, "type": rtype, "file": rel,
                                 "applicable": 0, "passed": 0, "controls": []}

    # Findings first: the reader is here to act, and ordering should follow that.
    return sorted(rows.values(),
                  key=lambda r: (r["applicable"] == 0,
                                 r["passed"] == r["applicable"],
                                 r["file"], r["name"]))


def _load_safe(path: Path) -> Any:
    """`_load` that never raises. A malformed template must cost its own rows, not the
    whole view."""
    try:
        return _load(path)
    except Exception:                                         # noqa: BLE001
        return {}
