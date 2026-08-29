"""Manifest, route, and service extraction across stacks.

The claim being tested is "any tech stack": the previous analysis ran a Python AST
walk and returned empty `apis`/`dependencies` for every language including Python,
so each ecosystem here is a separate assertion that the claim holds.
"""
from __future__ import annotations

import pytest

from src.services.code_parsers import (
    detect_languages, extract_routes, extract_services,
    parse_dependencies, parse_repository,
)


def write(root, files: dict[str, str]):
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


# ── Dependency manifests, one ecosystem at a time ────────────────────────────

def test_requirements_txt(tmp_path):
    write(tmp_path, {"requirements.txt": (
        "# comment\nfastapi>=0.115.0\nuvicorn[standard]==0.32.0\n\n"
        "-r other.txt\n--index-url https://example.com\npytest\n")})
    deps, tech = parse_dependencies(tmp_path)
    names = {d.name: d.version for d in deps}
    assert names["fastapi"] == ">=0.115.0"
    assert names["uvicorn"] == "==0.32.0"      # extras stripped from the name
    assert names["pytest"] == ""
    assert "other.txt" not in names            # -r points elsewhere, not a dependency
    assert "Python" in tech and "FastAPI" in tech


def test_package_json_separates_dev_dependencies(tmp_path):
    write(tmp_path, {"package.json": (
        '{"dependencies": {"react": "^19.0.0"},'
        ' "devDependencies": {"vite": "^6.0.0"}}')})
    deps, tech = parse_dependencies(tmp_path)
    by_name = {d.name: d for d in deps}
    assert by_name["react"].scope == "runtime"
    assert by_name["vite"].scope == "dev"
    assert by_name["react"].ecosystem == "npm"
    assert "React" in tech


def test_maven_pom_with_namespace(tmp_path):
    """Real POMs are namespaced; a parser that ignores that finds nothing."""
    write(tmp_path, {"pom.xml": """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
      <version>3.2.0</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId><artifactId>junit</artifactId>
      <version>4.13</version><scope>test</scope>
    </dependency>
  </dependencies>
</project>"""})
    deps, tech = parse_dependencies(tmp_path)
    by_name = {d.name: d for d in deps}
    assert by_name["org.springframework.boot:spring-boot-starter-web"].version == "3.2.0"
    assert by_name["junit:junit"].scope == "test"
    assert "Java" in tech and "Maven" in tech


def test_maven_pom_without_namespace(tmp_path):
    write(tmp_path, {"pom.xml": """<project>
  <dependencies><dependency>
    <groupId>com.example</groupId><artifactId>lib</artifactId><version>1.0</version>
  </dependency></dependencies></project>"""})
    deps, _ = parse_dependencies(tmp_path)
    assert [d.name for d in deps] == ["com.example:lib"]


def test_gradle(tmp_path):
    write(tmp_path, {"build.gradle": """
dependencies {
    implementation 'org.springframework:spring-core:6.1.0'
    testImplementation "org.junit.jupiter:junit-jupiter:5.10.0"
}"""})
    deps, tech = parse_dependencies(tmp_path)
    by_name = {d.name: d for d in deps}
    assert by_name["org.springframework:spring-core"].version == "6.1.0"
    assert by_name["org.junit.jupiter:junit-jupiter"].scope == "test"
    assert "Gradle" in tech


def test_go_mod_excludes_the_module_itself(tmp_path):
    write(tmp_path, {"go.mod": """module github.com/me/myapp

go 1.22

require (
    github.com/gin-gonic/gin v1.10.0
    github.com/stretchr/testify v1.9.0
)"""})
    deps, tech = parse_dependencies(tmp_path)
    names = {d.name for d in deps}
    assert "github.com/gin-gonic/gin" in names
    assert "github.com/me/myapp" not in names   # a repo does not depend on itself
    assert "Go" in tech and "Gin" in tech


def test_cargo_and_gemfile_and_csproj_and_composer(tmp_path):
    write(tmp_path, {
        "Cargo.toml": '[dependencies]\nserde = "1.0"\n[dev-dependencies]\ncriterion = "0.5"\n',
        "Gemfile": "source 'https://rubygems.org'\ngem 'rails', '7.1.0'\ngem 'sinatra'\n",
        "App.csproj": '<Project><ItemGroup>'
                      '<PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
                      '</ItemGroup></Project>',
        "composer.json": '{"require": {"php": "^8.2", "laravel/framework": "^11.0"}}',
    })
    deps, tech = parse_dependencies(tmp_path)
    by_eco: dict[str, dict] = {}
    for d in deps:
        by_eco.setdefault(d.ecosystem, {})[d.name] = d

    assert by_eco["crates"]["serde"].version == "1.0"
    assert by_eco["crates"]["criterion"].scope == "dev"
    assert by_eco["rubygems"]["rails"].version == "7.1.0"
    assert by_eco["nuget"]["Newtonsoft.Json"].version == "13.0.3"
    assert "laravel/framework" in by_eco["composer"]
    assert "php" not in by_eco["composer"]      # the runtime is not a package
    assert {"Rust", "Ruby", ".NET", "Ruby on Rails", "Laravel"} <= set(tech)


def test_pyproject_pep621_and_poetry(tmp_path):
    write(tmp_path, {"pyproject.toml": """
[project]
dependencies = ["httpx>=0.28", "pydantic>=2"]

[tool.poetry.dependencies]
python = "^3.12"
requests = "^2.31"

[tool.poetry.dev-dependencies]
black = "^24.0"
"""})
    deps, _ = parse_dependencies(tmp_path)
    by_name = {d.name: d for d in deps}
    assert "httpx" in by_name and "pydantic" in by_name
    assert by_name["requests"].version == "^2.31"
    assert by_name["black"].scope == "dev"
    assert "python" not in by_name              # the interpreter is not a dependency


def test_vendored_directories_are_skipped(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi\n",
        "node_modules/leftpad/package.json": '{"dependencies": {"evil": "1.0"}}',
        ".venv/lib/requirements.txt": "should-not-appear\n",
    })
    deps, _ = parse_dependencies(tmp_path)
    names = {d.name for d in deps}
    assert names == {"fastapi"}


def test_a_broken_manifest_does_not_stop_the_others(tmp_path):
    write(tmp_path, {
        "package.json": "{ this is not json",
        "requirements.txt": "fastapi\n",
    })
    deps, _ = parse_dependencies(tmp_path)
    assert [d.name for d in deps] == ["fastapi"]


def test_duplicate_packages_are_deduplicated(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi>=1\n",
        "sub/requirements.txt": "fastapi>=2\n",
    })
    deps, _ = parse_dependencies(tmp_path)
    assert [d.name for d in deps] == ["fastapi"]


# ── Routes ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel,body,expected", [
    ("api.py", '@app.get("/health")\ndef h(): ...\n@router.post("/orders")\ndef o(): ...',
     {("GET", "/health"), ("POST", "/orders")}),
    ("srv.js", "app.get('/users', h)\nrouter.delete('/users/:id', h)",
     {("GET", "/users"), ("DELETE", "/users/:id")}),
    ("Ctl.java", '@GetMapping("/v1/items")\npublic void a() {}\n@PostMapping("/v1/items")\npublic void b() {}',
     {("GET", "/v1/items"), ("POST", "/v1/items")}),
    ("Ctl.cs", '[HttpGet("api/ping")]\npublic void P() {}',
     {("GET", "api/ping")}),
    ("main.go", 'r.GET("/ping", h)\nr.POST("/submit", h)',
     {("GET", "/ping"), ("POST", "/submit")}),
])
def test_routes_per_framework(tmp_path, rel, body, expected):
    write(tmp_path, {rel: body})
    found = {(r.method, r.path) for r in extract_routes(tmp_path)}
    assert expected <= found


def test_flask_route_decorator_with_methods(tmp_path):
    write(tmp_path, {"app.py": '@app.route("/submit", methods=["POST", "PUT"])\ndef s(): ...'})
    found = {(r.method, r.path) for r in extract_routes(tmp_path)}
    assert ("POST", "/submit") in found and ("PUT", "/submit") in found


def test_routes_are_deduplicated(tmp_path):
    write(tmp_path, {"a.py": '@app.get("/x")\ndef a(): ...',
                     "b.py": '@app.get("/x")\ndef b(): ...'})
    assert len(extract_routes(tmp_path)) == 1


# ── Services ─────────────────────────────────────────────────────────────────

def test_service_suffixes_across_languages(tmp_path):
    write(tmp_path, {
        "a.py": "class PricingService:\n    pass\nclass Product:\n    pass\n",
        "B.java": "public class OrderController {}\npublic class Dto {}",
        "c.go": "type PaymentHandler struct {}\ntype Config struct {}",
        "d.ts": "export class AuthGateway {}\nexport class Row {}",
    })
    found = set(extract_services(tmp_path))
    assert found == {"PricingService", "OrderController", "PaymentHandler", "AuthGateway"}


def test_service_directory_convention(tmp_path):
    """Functional codebases declare no service class — the demo repo is one."""
    write(tmp_path, {
        "app/services/pricing.py": "def quote(): ...",
        "app/services/__init__.py": "",
        "app/models.py": "class Product: ...",
    })
    assert extract_services(tmp_path) == ["pricing"]


# ── Languages and the whole-repo entry point ─────────────────────────────────

def test_detect_languages_counts_by_extension(tmp_path):
    write(tmp_path, {"a.py": "", "b.py": "", "c.ts": "", "d.go": "", "e.unknown": ""})
    langs, total = detect_languages(tmp_path)
    assert langs == {"Python": 2, "TypeScript": 1, "Go": 1}
    assert total == 4


def test_parse_repository_combines_every_stack(tmp_path):
    write(tmp_path, {
        "backend/requirements.txt": "fastapi\n",
        "backend/app/main.py": '@app.get("/health")\ndef h(): ...',
        "backend/app/services/pricing.py": "def quote(): ...",
        "frontend/package.json": '{"dependencies": {"react": "^19.0.0"}}',
        "frontend/src/App.tsx": "export default () => null",
    })
    facts = parse_repository(tmp_path)
    assert {"Python", "TypeScript"} <= set(facts.languages)
    assert {"fastapi", "react"} <= {d.name for d in facts.dependencies}
    assert ("GET", "/health") in {(r.method, r.path) for r in facts.routes}
    assert "pricing" in facts.services
    assert {"FastAPI", "React"} <= set(facts.tech_stack)


def test_parse_repository_on_the_real_demo_project():
    """The fixture the demo actually uses — two stacks in one repo."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "demo-project" / "aura-demo-shop"
    if not root.is_dir():
        pytest.skip("demo project not present")
    facts = parse_repository(root)
    assert facts.languages.get("Python", 0) > 0
    assert facts.languages.get("TypeScript", 0) > 0
    assert {"fastapi", "react"} <= {d.name for d in facts.dependencies}
    assert len(facts.routes) >= 5
    assert "pricing" in facts.services
