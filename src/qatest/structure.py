"""Tests for a project that does not serve HTTP.

The browser runner can only test something with an address. An RPA application is
BPMN process definitions and Groovy scripts; a migrated Airflow project is DAG files.
Neither starts a server, so for both of them the previous answer was `unavailable` —
technically true and completely useless, since those are exactly the projects the
migration flow produces and consumes.

What CAN be asserted about them is structural, and it is worth more than it sounds:
a sequence flow pointing at an element that does not exist, a script task naming a
Groovy class nobody wrote, a DAG that does not import — these are the defects a
migration actually introduces, and none of them need the application to run.

**Deliberately honest about its limits.** Groovy is not compiled here (that needs a
JVM) and a DAG's operators are not resolved (that needs Airflow and the project's
dependencies). Each check says what it verified, and nothing claims more than it did.
"""
from __future__ import annotations

import ast
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Directories that are never the project.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             "target", ".idea", ".pytest_cache"}

#: A cap, so a monorepo cannot plan ten thousand checks.
MAX_FILES = 300

BPMN_NS = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"


@dataclass
class Check:
    """One structural assertion about one file."""
    check_id: str
    name: str          # "claims-intake.bpmn — every sequence flow resolves"
    rel_path: str
    validator: str     # which function runs it


def _walk(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(out) >= MAX_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return out


def plan_checks(root: Path) -> list[Check]:
    """Every structural check this project supports, in a stable order."""
    root = Path(root)
    if not root.is_dir():
        return []

    checks: list[Check] = []

    def add(path: Path, validator: str, what: str) -> None:
        rel = str(path.relative_to(root))
        checks.append(Check(check_id=f"struct-{len(checks):03d}",
                            name=f"{path.name} — {what}",
                            rel_path=rel, validator=validator))

    for path in _walk(root, (".bpmn",)):
        add(path, "bpmn_parses", "parses as BPMN")
        add(path, "bpmn_flows_resolve", "every sequence flow resolves")
        add(path, "bpmn_scripts_exist", "every script task names a file that exists")

    for path in _walk(root, (".groovy",)):
        add(path, "groovy_balanced", "is structurally balanced")

    for path in _walk(root, (".xml", ".properties")):
        if path.suffix.lower() == ".xml":
            add(path, "xml_parses", "parses as XML")
        else:
            add(path, "properties_parse", "parses as a properties file")

    for path in _walk(root, (".py",)):
        if _looks_like_a_dag(path):
            add(path, "dag_parses", "parses as Python")
            add(path, "dag_defines_a_dag", "defines a DAG with unique task ids")

    return checks


def _looks_like_a_dag(path: Path) -> bool:
    """A file Airflow will try to load, not just any Python file.

    Deliberately NOT "contains DAG(" — that was the first version and it excluded the
    single most important case. A converted file that imports Airflow but defines no
    DAG is exactly what a bad migration produces, and Airflow ignores it silently; a
    detector that skips it because it has no `DAG(` in it can never report the defect
    it exists to find.

    So: anything under a `dags/` folder that is Python, plus anything anywhere that
    imports Airflow. A plain module outside `dags/` that never mentions Airflow is
    still left alone — reporting those would put a false failure on every Python
    project in the estate.
    """
    if any(part == "dags" for part in path.parts):
        return True
    try:
        return "airflow" in path.read_text(errors="replace")[:4000]
    except OSError:
        return False


# ── Validators ────────────────────────────────────────────────────────────────
#
# Each returns (ok, detail). `detail` explains a failure, or says what was verified
# on success — a green tick that cannot say what it checked is not worth much.

def run_check(root: Path, check: Check) -> tuple[bool, str]:
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


def _v_bpmn_parses(path: Path, _root: Path) -> tuple[bool, str]:
    try:
        root_el = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return False, f"not well-formed XML: {exc}"
    processes = root_el.findall(f".//{BPMN_NS}process") or root_el.findall(".//process")
    if not processes:
        return False, "no <process> element — this is XML but not a BPMN definition"
    names = [p.get("id", "?") for p in processes]
    return True, f"{len(processes)} process(es): {', '.join(names)}"


def _v_bpmn_flows_resolve(path: Path, _root: Path) -> tuple[bool, str]:
    """A sequence flow pointing at an id that does not exist is a broken process —
    and it is the single most common thing a hand-edited migration gets wrong."""
    root_el = ET.parse(path).getroot()
    ids = {el.get("id") for el in root_el.iter() if el.get("id")}
    dangling = []
    flows = 0
    for flow in _find(root_el, "sequenceFlow"):
        flows += 1
        for attr in ("sourceRef", "targetRef"):
            ref = flow.get(attr)
            if ref and ref not in ids:
                dangling.append(f"{flow.get('id', '?')}.{attr} -> {ref}")
    if dangling:
        return False, f"{len(dangling)} unresolved: " + "; ".join(dangling[:5])
    return True, f"{flows} sequence flow(s) all resolve"


def _v_bpmn_scripts_exist(path: Path, root: Path) -> tuple[bool, str]:
    """`<script>PolicyLookup.adjudicate(execution)</script>` names a Groovy class.
    If no file defines it, the task cannot run — before or after a migration."""
    root_el = ET.parse(path).getroot()
    groovy = {p.stem for p in root.rglob("*.groovy")
              if not any(part in SKIP_DIRS for part in p.parts)}
    missing, checked = [], 0
    for task in _find(root_el, "scriptTask"):
        script = (_text_of(task, "script") or "").strip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.", script)
        if not match:
            continue
        checked += 1
        if match.group(1) not in groovy:
            missing.append(f"{task.get('id', '?')} -> {match.group(1)}")
    if missing:
        return False, ("no Groovy file defines: " + "; ".join(missing[:5]))
    if not checked:
        return True, "no script tasks reference a Groovy class"
    return True, f"{checked} script task(s) resolve to a Groovy file"


def _v_groovy_balanced(path: Path, _root: Path) -> tuple[bool, str]:
    """Brackets balance outside strings and comments.

    NOT a compile — that needs a JVM. Says so, so a pass is not mistaken for one.
    """
    text = path.read_text(errors="replace")
    stripped = _strip_groovy_noise(text)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False, f"unbalanced {ch!r}"
    if stack:
        return False, f"{len(stack)} unclosed {stack[-1]!r}"
    return True, "brackets balance (not compiled — that needs a JVM)"


def _strip_groovy_noise(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r'"""(.*?)"""', '""', text, flags=re.S)
    text = re.sub(r"'''(.*?)'''", "''", text, flags=re.S)
    text = re.sub(r'"(\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(\\.|[^'\\])*'", "''", text)
    return text


def _v_xml_parses(path: Path, _root: Path) -> tuple[bool, str]:
    try:
        root_el = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return False, f"not well-formed XML: {exc}"
    return True, f"root element <{_local(root_el.tag)}>"


def _v_properties_parse(path: Path, _root: Path) -> tuple[bool, str]:
    keys, bad = 0, []
    for n, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" not in line and ":" not in line:
            bad.append(f"line {n}")
            continue
        keys += 1
    if bad:
        return False, "no key/value separator on " + ", ".join(bad[:5])
    return True, f"{keys} propert{'y' if keys == 1 else 'ies'}"


def _v_dag_parses(path: Path, _root: Path) -> tuple[bool, str]:
    try:
        ast.parse(path.read_text(errors="replace"))
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"
    return True, "parses as Python (imports not resolved — that needs Airflow installed)"


def _v_dag_defines_a_dag(path: Path, _root: Path) -> tuple[bool, str]:
    """A DAG file that defines no DAG is silently ignored by Airflow, which is the
    worst way for a migration to fail: nothing errors, the pipeline is just absent."""
    tree = ast.parse(path.read_text(errors="replace"))
    dags, task_ids = 0, []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "DAG":
                dags += 1
            for kw in node.keywords:
                if kw.arg == "task_id" and isinstance(kw.value, ast.Constant):
                    task_ids.append(kw.value.value)
        elif isinstance(node, ast.FunctionDef):
            if any(_deco_name(d) in ("dag", "task") for d in node.decorator_list):
                if _deco_name(node.decorator_list[0]) == "dag":
                    dags += 1
                else:
                    task_ids.append(node.name)
    if not dags:
        return False, ("no DAG is defined — Airflow ignores this file silently, so "
                       "the pipeline would simply be missing")
    duplicates = {t for t in task_ids if task_ids.count(t) > 1}
    if duplicates:
        return False, "duplicate task ids: " + ", ".join(sorted(duplicates)[:5])
    return True, f"{dags} DAG(s), {len(task_ids)} unique task id(s)"


# ── helpers ───────────────────────────────────────────────────────────────────

def _find(root_el, tag: str):
    return root_el.findall(f".//{BPMN_NS}{tag}") or root_el.findall(f".//{tag}")


def _text_of(el, tag: str) -> str:
    for child in el:
        if _local(child.tag) == tag:
            return child.text or ""
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def _deco_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _deco_name(node.func)
    return ""
