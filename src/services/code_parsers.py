"""Deterministic, language-agnostic extraction of dependencies and HTTP routes.

The existing code analysis only ran a Python AST walk, so a Java or Go repo
produced a language histogram and nothing else — and `apis`/`dependencies` came
back empty for every stack including Python.

Everything here is regex/manifest parsing, never an LLM call: the graph is a
factual record, and a hallucinated dependency edge is worse than a missing one.
Route extraction is explicitly best-effort and marks itself as such via
`factType: "inferred"` when written to the graph.
"""
from __future__ import annotations

import json
import logging
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "target", "build", ".venv", "venv",
    "dist", ".next", ".nuxt", "coverage", ".mypy_cache", ".pytest_cache",
    "vendor", ".gradle", "bin", "obj", ".idea", ".vscode",
}

# How many files to read per repo when hunting for routes. Route scanning opens
# files; an unbounded walk over a large monorepo would dominate analysis time.
_MAX_ROUTE_FILES = 400
_MAX_FILE_BYTES = 512 * 1024


@dataclass
class Dependency:
    name: str
    version: str
    ecosystem: str          # pypi | npm | maven | go | crates | rubygems | nuget | composer
    scope: str = "runtime"  # runtime | dev | test


@dataclass
class Route:
    method: str
    path: str
    file: str
    framework: str


@dataclass
class RepoFacts:
    languages: dict[str, int] = field(default_factory=dict)
    dependencies: list[Dependency] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    file_count: int = 0


# ── Language detection ───────────────────────────────────────────────────────

_EXT_LANG = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
    ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C", ".swift": "Swift",
    ".m": "Objective-C", ".sh": "Shell", ".sql": "SQL", ".xml": "XML",
    ".yaml": "YAML", ".yml": "YAML", ".tf": "Terraform", ".cbl": "COBOL",
    ".cob": "COBOL", ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart",
    ".ex": "Elixir", ".exs": "Elixir", ".clj": "Clojure", ".pl": "Perl",
}


def iter_files(root: Path, limit: int | None = None):
    """Walk `root`, skipping vendored and build directories."""
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path
        count += 1
        if limit and count >= limit:
            return


def detect_languages(root: Path) -> tuple[dict[str, int], int]:
    langs: dict[str, int] = {}
    total = 0
    for path in iter_files(root):
        lang = _EXT_LANG.get(path.suffix.lower())
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
            total += 1
    return langs, total


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ── Dependency manifests ─────────────────────────────────────────────────────

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*([<>=!~^]=?.*)?$")


def _parse_requirements(path: Path) -> list[Dependency]:
    out = []
    for line in _read(path).splitlines():
        line = line.split("#")[0].strip()
        # -r/-e/--index-url lines point elsewhere; they are not dependencies.
        if not line or line.startswith("-"):
            continue
        m = _REQ_LINE.match(line)
        if m:
            out.append(Dependency(m.group(1), (m.group(2) or "").strip(), "pypi"))
    return out


def _parse_pyproject(path: Path) -> list[Dependency]:
    try:
        data = tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    out: list[Dependency] = []
    for spec in data.get("project", {}).get("dependencies", []) or []:
        m = _REQ_LINE.match(str(spec).strip())
        if m:
            out.append(Dependency(m.group(1), (m.group(2) or "").strip(), "pypi"))
    # Poetry keeps its own table.
    poetry = data.get("tool", {}).get("poetry", {})
    for name, ver in (poetry.get("dependencies") or {}).items():
        if name.lower() == "python":
            continue
        out.append(Dependency(name, ver if isinstance(ver, str) else "", "pypi"))
    for name, ver in (poetry.get("dev-dependencies") or {}).items():
        out.append(Dependency(name, ver if isinstance(ver, str) else "", "pypi", "dev"))
    return out


def _parse_package_json(path: Path) -> list[Dependency]:
    try:
        data = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return []
    out = []
    for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev")):
        for name, ver in (data.get(key) or {}).items():
            out.append(Dependency(name, str(ver), "npm", scope))
    return out


def _parse_pom(path: Path) -> list[Dependency]:
    try:
        root = ET.fromstring(_read(path) or "<project/>")
    except ET.ParseError:
        return []
    # Maven POMs are namespaced; strip it rather than hard-coding one version.
    ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    find = (lambda el, tag: el.find(f"m:{tag}", ns)) if ns else (lambda el, tag: el.find(tag))
    out = []
    path_expr = ".//m:dependency" if ns else ".//dependency"
    for dep in root.findall(path_expr, ns) if ns else root.findall(path_expr):
        gid, aid, ver = find(dep, "groupId"), find(dep, "artifactId"), find(dep, "version")
        scope_el = find(dep, "scope")
        if aid is None:
            continue
        name = f"{gid.text}:{aid.text}" if gid is not None and gid.text else aid.text
        out.append(Dependency(
            (name or "").strip(), (ver.text or "").strip() if ver is not None else "",
            "maven", (scope_el.text or "runtime").strip() if scope_el is not None else "runtime"))
    return out


_GRADLE_DEP = re.compile(
    r"""(implementation|api|compileOnly|runtimeOnly|testImplementation)\s*\(?\s*['"]([^'"]+)['"]""")


def _parse_gradle(path: Path) -> list[Dependency]:
    out = []
    for conf, coord in _GRADLE_DEP.findall(_read(path)):
        parts = coord.split(":")
        name = ":".join(parts[:2]) if len(parts) >= 2 else coord
        ver = parts[2] if len(parts) >= 3 else ""
        scope = "test" if conf.startswith("test") else "runtime"
        out.append(Dependency(name, ver, "maven", scope))
    return out


_GOMOD = re.compile(r"^\s*([\w./-]+\.[\w./-]+)\s+(v[\w.\-+]+)", re.M)


def _parse_go_mod(path: Path) -> list[Dependency]:
    text = _read(path)
    # Drop the module's own declaration so a repo does not depend on itself.
    text = re.sub(r"^module\s+\S+", "", text, flags=re.M)
    return [Dependency(n, v, "go") for n, v in _GOMOD.findall(text)]


def _parse_cargo(path: Path) -> list[Dependency]:
    try:
        data = tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    out = []
    for key, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev")):
        for name, spec in (data.get(key) or {}).items():
            ver = spec if isinstance(spec, str) else (spec or {}).get("version", "")
            out.append(Dependency(name, str(ver), "crates", scope))
    return out


_GEM = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"])?""", re.M)


def _parse_gemfile(path: Path) -> list[Dependency]:
    return [Dependency(n, v or "", "rubygems") for n, v in _GEM.findall(_read(path))]


_CSPROJ = re.compile(r"""<PackageReference\s+Include=["']([^"']+)["'](?:\s+Version=["']([^"']+)["'])?""")


def _parse_csproj(path: Path) -> list[Dependency]:
    return [Dependency(n, v or "", "nuget") for n, v in _CSPROJ.findall(_read(path))]


def _parse_composer(path: Path) -> list[Dependency]:
    try:
        data = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return []
    out = []
    for key, scope in (("require", "runtime"), ("require-dev", "dev")):
        for name, ver in (data.get(key) or {}).items():
            if name.lower() in ("php",) or name.startswith("ext-"):
                continue
            out.append(Dependency(name, str(ver), "composer", scope))
    return out


_MANIFESTS: list[tuple[str, object, list[str]]] = [
    ("requirements.txt", _parse_requirements, ["Python"]),
    ("pyproject.toml",   _parse_pyproject,    ["Python"]),
    ("package.json",     _parse_package_json, ["Node.js"]),
    ("pom.xml",          _parse_pom,          ["Java", "Maven"]),
    ("build.gradle",     _parse_gradle,       ["Java", "Gradle"]),
    ("build.gradle.kts", _parse_gradle,       ["Kotlin", "Gradle"]),
    ("go.mod",           _parse_go_mod,       ["Go"]),
    ("Cargo.toml",       _parse_cargo,        ["Rust"]),
    ("Gemfile",          _parse_gemfile,      ["Ruby"]),
    ("composer.json",    _parse_composer,     ["PHP"]),
]

# Files that identify a technology without carrying dependencies.
_MARKER_TECH = {
    "dockerfile": ["Docker"], "docker-compose.yml": ["Docker Compose"],
    "docker-compose.yaml": ["Docker Compose"], "chart.yaml": ["Helm", "Kubernetes"],
    "serverless.yml": ["Serverless"], "angular.json": ["Angular"],
    "next.config.js": ["Next.js"], "nuxt.config.js": ["Nuxt.js"],
    "vite.config.ts": ["Vite"], "svelte.config.js": ["Svelte"],
    "mule-artifact.json": ["Mule 4"], "pipfile": ["Python"],
}

# Package names that imply a framework worth naming in the tech stack.
_DEP_TECH = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
    "react": "React", "vue": "Vue", "@angular/core": "Angular",
    "express": "Express", "next": "Next.js", "svelte": "Svelte",
    "org.springframework.boot:spring-boot-starter": "Spring Boot",
    "org.springframework:spring-core": "Spring",
    "github.com/gin-gonic/gin": "Gin", "github.com/labstack/echo": "Echo",
    "rails": "Ruby on Rails", "sinatra": "Sinatra",
    "laravel/framework": "Laravel", "symfony/symfony": "Symfony",
    "actix-web": "Actix", "rocket": "Rocket",
    "sqlalchemy": "SQLAlchemy", "pytest": "pytest", "junit": "JUnit",
}


def parse_dependencies(root: Path) -> tuple[list[Dependency], list[str]]:
    """Every dependency across every manifest found, plus inferred technologies."""
    deps: list[Dependency] = []
    tech: set[str] = set()
    for path in iter_files(root):
        name = path.name
        lower = name.lower()
        for manifest, parser, techs in _MANIFESTS:
            if name == manifest:
                try:
                    found = parser(path)          # type: ignore[operator]
                except Exception as exc:          # noqa: BLE001 — one bad manifest must not stop the rest
                    log.debug("manifest parse failed %s: %s", path, exc)
                    found = []
                if found or name == manifest:
                    tech.update(techs)
                deps.extend(found)
        if lower.endswith(".csproj"):
            deps.extend(_parse_csproj(path))
            tech.update([".NET", "C#"])
        if lower in _MARKER_TECH:
            tech.update(_MARKER_TECH[lower])
        if lower.endswith(".tf"):
            tech.add("Terraform")

    for d in deps:
        key = d.name.lower()
        if key in _DEP_TECH:
            tech.add(_DEP_TECH[key])
        else:
            for prefix, label in _DEP_TECH.items():
                if key.startswith(prefix):
                    tech.add(label)

    # Same package can appear in several manifests; keep the first occurrence.
    seen: set[tuple[str, str]] = set()
    unique = []
    for d in deps:
        k = (d.ecosystem, d.name.lower())
        if d.name and k not in seen:
            seen.add(k)
            unique.append(d)
    return unique, sorted(tech)


# ── HTTP routes ──────────────────────────────────────────────────────────────

_ROUTE_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("FastAPI/Flask", ".py", re.compile(
        r"@(?:\w+)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.I)),
    ("Flask", ".py", re.compile(
        r"@(?:\w+)\.route\(\s*[\"']([^\"']+)[\"'](?:.*?methods\s*=\s*\[([^\]]*)\])?", re.I | re.S)),
    ("Express", ".js", re.compile(
        r"\b(?:app|router)\.(get|post|put|patch|delete)\(\s*[\"'`]([^\"'`]+)[\"'`]", re.I)),
    ("Express", ".ts", re.compile(
        r"\b(?:app|router)\.(get|post|put|patch|delete)\(\s*[\"'`]([^\"'`]+)[\"'`]", re.I)),
    ("Spring", ".java", re.compile(
        r"@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?[\"']([^\"']+)[\"']")),
    ("Spring", ".kt", re.compile(
        r"@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?[\"']([^\"']+)[\"']")),
    ("ASP.NET", ".cs", re.compile(
        r"\[Http(Get|Post|Put|Patch|Delete)\(\s*[\"']([^\"']*)[\"']")),
    ("Gin/Echo", ".go", re.compile(
        r"\.(GET|POST|PUT|PATCH|DELETE)\(\s*\"([^\"]+)\"")),
]


def extract_routes(root: Path) -> list[Route]:
    """Best-effort HTTP route discovery. Never authoritative — marked inferred."""
    out: list[Route] = []
    seen: set[tuple[str, str]] = set()
    for path in iter_files(root, limit=_MAX_ROUTE_FILES):
        suffix = path.suffix.lower()
        text: str | None = None
        for framework, ext, pattern in _ROUTE_PATTERNS:
            if suffix != ext:
                continue
            if text is None:
                text = _read(path)
                if not text:
                    break
            for match in pattern.finditer(text):
                groups = match.groups()
                if framework == "Flask" and len(groups) == 2 and groups[0].startswith("/"):
                    route_path = groups[0]
                    methods = [m.strip().strip("\"'").upper()
                               for m in (groups[1] or "GET").split(",") if m.strip()]
                else:
                    methods, route_path = [groups[0].upper()], groups[1]
                for method in methods:
                    key = (method, route_path)
                    if route_path and key not in seen:
                        seen.add(key)
                        out.append(Route(method, route_path,
                                         str(path.relative_to(root)), framework))
    return out


# ── Services ─────────────────────────────────────────────────────────────────

# Type declarations across the stacks we detect. The Python AST walk in
# code_analysis_agent only ever saw .py files; these are regexes so a Java, Go or
# TypeScript repo yields services too.
_TYPE_DECL: list[tuple[str, re.Pattern]] = [
    (".py",    re.compile(r"^\s*class\s+([A-Z]\w+)", re.M)),
    (".ts",    re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Z]\w+)", re.M)),
    (".tsx",   re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Z]\w+)", re.M)),
    (".js",    re.compile(r"^\s*(?:export\s+)?class\s+([A-Z]\w+)", re.M)),
    (".java",  re.compile(r"^\s*(?:public\s+|final\s+|abstract\s+)*(?:class|interface)\s+([A-Z]\w+)", re.M)),
    (".kt",    re.compile(r"^\s*(?:open\s+|data\s+|abstract\s+)*class\s+([A-Z]\w+)", re.M)),
    (".cs",    re.compile(r"^\s*(?:public\s+|internal\s+|sealed\s+|abstract\s+)*(?:class|interface)\s+([A-Z]\w+)", re.M)),
    (".go",    re.compile(r"^\s*type\s+([A-Z]\w+)\s+(?:struct|interface)\b", re.M)),
    (".rb",    re.compile(r"^\s*class\s+([A-Z]\w+)", re.M)),
    (".php",   re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Z]\w+)", re.M)),
    (".rs",    re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Z]\w+)", re.M)),
    (".scala", re.compile(r"^\s*(?:case\s+)?class\s+([A-Z]\w+)", re.M)),
]

# A repo has far more types than services. These suffixes are the conventional
# markers for something that behaves like one; anything else would make the graph
# a class dump rather than an architecture view.
_SERVICE_SUFFIXES = (
    "Service", "Controller", "Router", "Handler", "Manager", "Repository",
    "Agent", "Client", "Gateway", "Provider", "Worker", "Consumer", "Producer",
    "UseCase", "Resource", "Facade",
)


# Directory names that conventionally hold one service per module. Suffix matching
# alone misses the very common functional style — the demo repo's
# backend/app/services/pricing.py declares no class at all, so nothing would be
# found for a repo whose services are plainly laid out on disk.
_SERVICE_DIRS = {"services", "service", "handlers", "controllers", "usecases", "domain"}

_CODE_EXTS = {ext for ext, _ in _TYPE_DECL}


def extract_services(root: Path) -> list[str]:
    """Service-like types and service modules, across any detected language."""
    found: set[str] = set()
    by_ext: dict[str, list[re.Pattern]] = {}
    for ext, pattern in _TYPE_DECL:
        by_ext.setdefault(ext, []).append(pattern)

    for path in iter_files(root, limit=_MAX_ROUTE_FILES):
        suffix = path.suffix.lower()
        if suffix not in _CODE_EXTS:
            continue

        parts = path.relative_to(root).parts
        stem = path.stem
        if (len(parts) > 1 and parts[-2].lower() in _SERVICE_DIRS
                and not stem.startswith("__") and not stem.startswith("index")):
            found.add(stem)

        patterns = by_ext.get(suffix)
        text = _read(path) if patterns else ""
        for pattern in patterns or []:
            if not text:
                break
            for name in pattern.findall(text):
                if name.endswith(_SERVICE_SUFFIXES):
                    found.add(name)
    return sorted(found)


def parse_repository(root: Path) -> RepoFacts:
    """Everything derivable from a checkout, for any stack."""
    languages, file_count = detect_languages(root)
    deps, tech = parse_dependencies(root)
    routes = extract_routes(root)
    services = extract_services(root)
    for lang in languages:
        if lang not in tech:
            tech.append(lang)
    return RepoFacts(languages=languages, dependencies=deps, routes=routes,
                     services=services, tech_stack=sorted(set(tech)),
                     file_count=file_count)
