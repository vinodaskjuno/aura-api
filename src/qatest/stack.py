"""Assertions about a running stack, for output the migration produced.

A converted project has no API nodes in the knowledge graph — nothing analysed it —
so the graph-driven planner has nothing to say about it. What it does have is a
compose stack the converter shipped, and once that is up there are a handful of
questions worth asking that are specific to the target rather than to the project.

For Airflow the important one is **import errors**. A generated DAG that does not
import is not a failing pipeline, it is an *absent* one: Airflow logs the error and
carries on, the DAG never appears, and nothing anywhere goes red. That is the failure
mode a migration produces most often and the one a reviewer is least likely to catch
by eye.

Kept separate from `structure.py` because these need the application running, and
separate from the graph-driven cases because they are properties of the TARGET
platform, not of the project.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TIMEOUT_S = 30


@dataclass(frozen=True)
class Assertion:
    """One question to ask a running stack."""
    assertion_id: str
    name: str
    path: str
    checker: str
    auth: tuple[str, str] | None = None


@dataclass(frozen=True)
class StackChecks:
    target: str
    assertions: tuple[Assertion, ...] = field(default_factory=tuple)


#: Airflow's REST API, with the credentials the generated compose stack creates.
_AIRFLOW_AUTH = ("aura", "aura")

AIRFLOW = StackChecks(
    target="airflow",
    assertions=(
        Assertion("stack-000", "Airflow reports itself healthy",
                  "/health", "airflow_health"),
        Assertion("stack-001", "every converted DAG imports without error",
                  "/api/v1/importErrors", "airflow_no_import_errors", _AIRFLOW_AUTH),
        Assertion("stack-002", "the converted DAGs are registered",
                  "/api/v1/dags", "airflow_dags_present", _AIRFLOW_AUTH),
    ),
)

CHECKS: dict[str, StackChecks] = {"airflow": AIRFLOW}


def for_target(target: str) -> StackChecks | None:
    return CHECKS.get((target or "").strip().lower())


def run_assertion(base_url: str, assertion: Assertion) -> tuple[bool, str]:
    """Ask one question of the running stack. (ok, detail)."""
    url = base_url.rstrip("/") + assertion.path
    try:
        body, status = _get(url, assertion.auth)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {assertion.path}"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"could not reach {assertion.path}: {exc}"

    fn = globals().get(f"_c_{assertion.checker}")
    if fn is None:
        return False, f"no checker named {assertion.checker!r}"
    return fn(body, status)


def _get(url: str, auth: tuple[str, str] | None) -> tuple[str, int]:
    request = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", "replace"), resp.status


# ── Checkers ──────────────────────────────────────────────────────────────────

def _c_airflow_health(body: str, status: int) -> tuple[bool, str]:
    if status != 200:
        return False, f"/health returned {status}"
    try:
        data = json.loads(body)
    except ValueError:
        return False, "/health did not return JSON"
    parts = {k: (v or {}).get("status") for k, v in data.items() if isinstance(v, dict)}
    unhealthy = [k for k, v in parts.items() if v not in ("healthy", None)]
    if unhealthy:
        return False, "unhealthy: " + ", ".join(f"{k}={parts[k]}" for k in unhealthy)
    return True, ", ".join(f"{k} {v}" for k, v in parts.items()) or "healthy"


def _c_airflow_no_import_errors(body: str, _status: int) -> tuple[bool, str]:
    """The one that matters.

    An import error does not make a DAG fail — it makes it ABSENT. Airflow logs it
    and carries on, so a migration that produced a DAG referencing an operator nobody
    installed looks, from every other angle, like a migration that produced nothing.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return False, "importErrors did not return JSON"
    errors = data.get("import_errors") or []
    if errors:
        lines = [f"{e.get('filename', '?').split('/')[-1]}: "
                 f"{(e.get('stack_trace') or '').strip().splitlines()[-1][:120]}"
                 for e in errors[:4]]
        return False, f"{len(errors)} DAG file(s) failed to import — " + "; ".join(lines)
    return True, "no import errors"


def _c_airflow_dags_present(body: str, _status: int) -> tuple[bool, str]:
    try:
        data = json.loads(body)
    except ValueError:
        return False, "dags did not return JSON"
    dags = data.get("dags") or []
    if not dags:
        return False, ("Airflow is running but registered no DAGs — the converted "
                       "files are not in the DAGs folder, or none of them defines a DAG")
    names = [d.get("dag_id", "?") for d in dags[:6]]
    return True, f"{len(dags)} DAG(s): " + ", ".join(names)
