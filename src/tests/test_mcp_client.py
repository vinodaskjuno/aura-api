"""Aura as an MCP client: naming, schema sanitising, and failure isolation.

The naming and schema tests carry the most weight, and not for tidiness. Model providers
enforce tool-name and tool-spec rules SERVER-SIDE — botocore validates only minimum
string length — so an illegal name produced here is not a caught error around one tool.
It is a ValidationException on the whole request: the chat turn dies before a token
streams and the user loses every built-in tool as well. One misconfigured MCP server
would take chat down for that user entirely.

These tests are what stop that regressing.
"""
from __future__ import annotations

import asyncio

import pytest

from src.mcp_client import adapter, naming, registry


# ── Naming ────────────────────────────────────────────────────────────────────

HOSTILE_NAMES = [
    "tool_servicenow_list_cmdb_items",      # long but ordinary
    "get-user-profile",                     # hyphens: illegal on Bedrock
    "files.read",                           # dots
    "123_starts_with_digit",                # must start with a letter
    "",                                     # empty
    "x" * 200,                              # absurdly long
    "ünïcødé_tool",                         # non-ascii
    "with spaces in it",
    "__dunder__",
]


@pytest.mark.parametrize("remote", HOSTILE_NAMES)
@pytest.mark.parametrize("server_name", ["Acme SRE", "", "9 Lives", "a" * 60])
def test_tool_names_are_always_legal(server_name, remote):
    """The property that must never break, over inputs a real server can produce."""
    slug = naming.slug_for(server_name, "3f2a1b4c-dead-beef-0000-111122223333")
    assert naming.LEGAL_SLUG.match(slug), f"illegal slug {slug!r}"

    name = naming.tool_name(slug, remote)
    if name:
        assert naming.LEGAL_TOOL.match(name), f"illegal tool name {name!r}"
        assert len(name) <= naming.MAX_TOOL_NAME
        assert name.startswith("mcp__")


def test_connector_uuid_would_have_been_illegal():
    """Pins the bug this design exists to avoid.

    The obvious scheme — mcp__{connectorId}__{tool} — is both too long and contains
    hyphens, which Bedrock rejects. If someone 'simplifies' naming.py back to that,
    this fails.
    """
    connector_id = "3f2a1b4c-dead-beef-0000-111122223333"
    naive = f"mcp__{connector_id}__tool_servicenow_list_cmdb_items"
    assert not naming.LEGAL_TOOL.match(naive)
    assert len(naive) > naming.MAX_TOOL_NAME

    slug = naming.slug_for("Acme SRE", connector_id)
    assert naming.LEGAL_TOOL.match(naming.tool_name(slug, "tool_servicenow_list_cmdb_items"))


def test_slug_is_stable_and_disambiguates():
    """Stable so tool names do not churn; suffixed so two servers named the same by
    one user do not collide."""
    a = naming.slug_for("Acme SRE", "aaaaaaaa-0000-0000-0000-000000000000")
    b = naming.slug_for("Acme SRE", "bbbbbbbb-0000-0000-0000-000000000000")
    assert a == naming.slug_for("Acme SRE", "aaaaaaaa-0000-0000-0000-000000000000")
    assert a != b


def test_long_tool_names_stay_unique_after_truncation():
    slug = naming.slug_for("srv", "abc00000-0000-0000-0000-000000000000")
    one = naming.tool_name(slug, "a_very_long_tool_name_" + "x" * 90 + "_alpha")
    two = naming.tool_name(slug, "a_very_long_tool_name_" + "x" * 90 + "_beta")
    assert one and two and one != two, "truncation must not collapse distinct tools"


# ── Schema sanitising ─────────────────────────────────────────────────────────

def test_pydantic_titles_are_stripped():
    """FastMCP emits a `title` on every field. Pure token cost on every request."""
    out = adapter.sanitise_schema({
        "type": "object",
        "title": "list_deploysArguments",
        "properties": {"service": {"type": "string", "title": "Service", "default": ""}},
    })
    assert "title" not in out
    assert "title" not in out["properties"]["service"]
    assert out["properties"]["service"]["default"] == ""


def test_refs_are_inlined_and_defs_dropped():
    out = adapter.sanitise_schema({
        "type": "object",
        "$defs": {"Filter": {"type": "string", "enum": ["a", "b"]}},
        "properties": {"filter": {"$ref": "#/$defs/Filter"}},
    })
    assert "$defs" not in out
    assert out["properties"]["filter"]["type"] == "string"
    assert out["properties"]["filter"]["enum"] == ["a", "b"]


def test_recursive_ref_terminates():
    out = adapter.sanitise_schema({
        "type": "object",
        "$defs": {"Node": {"type": "object",
                           "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    })
    assert out["type"] == "object"          # terminated rather than hung


@pytest.mark.parametrize("raw,reason", [
    ({"type": "string"}, "root must be an object"),
    (None, "absent schema"),
    ({}, "empty schema"),
    ("not a dict", "wrong type entirely"),
])
def test_root_is_always_an_object(raw, reason):
    out = adapter.sanitise_schema(raw)
    if out is not None:
        assert out["type"] == "object", reason
        assert "properties" in out


def test_null_unions_collapse():
    out = adapter.sanitise_schema({
        "type": "object",
        "properties": {"q": {"type": ["string", "null"]}},
    })
    assert out["properties"]["q"]["type"] == "string"


def test_empty_required_is_dropped():
    assert "required" not in adapter.sanitise_schema({"type": "object",
                                                      "properties": {}, "required": []})


def test_absurd_schema_is_rejected_not_truncated():
    """A generated schema can be megabytes. Shipping it would blow the request, so the
    tool is dropped instead — a missing tool beats a failed turn."""
    huge = {"type": "object",
            "properties": {f"f{i}": {"type": "string", "description": "x" * 200}
                           for i in range(500)}}
    assert adapter.sanitise_schema(huge) is None


# ── Tool definitions ──────────────────────────────────────────────────────────

def test_description_is_never_empty():
    """MCP allows a null description; botocore's NonEmptyString does not, and the
    resulting ParamValidationError is raised locally and kills the whole turn."""
    for description in ("", None, "   "):
        out = adapter.to_tool_def("Acme", "mcp__a_1__t", "t", description, {"type": "object"})
        assert out["description"].strip(), f"empty description survived for {description!r}"


def test_unusable_schema_drops_the_tool():
    huge = {"type": "object",
            "properties": {f"f{i}": {"type": "string", "description": "x" * 200}
                           for i in range(500)}}
    assert adapter.to_tool_def("Acme", "mcp__a_1__t", "t", "d", huge) is None


def test_tool_defs_pass_botocore_validation():
    """The strongest check available offline: push the merged spec through the REAL
    bedrock-runtime input shape. Catches anything botocore enforces client-side,
    with no AWS call."""
    botocore = pytest.importorskip("botocore.session")
    from botocore.validate import validate_parameters

    model = botocore.get_session().get_service_model("bedrock-runtime")
    shape = model.operation_model("ConverseStream").input_shape

    slug = naming.slug_for("Acme SRE", "3f2a1b4c-dead-beef-0000-111122223333")
    tools = []
    for remote in HOSTILE_NAMES:
        name = naming.tool_name(slug, remote)
        if not name:
            continue
        definition = adapter.to_tool_def("Acme SRE", name, remote, "", {"type": "object"})
        tools.append({"toolSpec": {"name": definition["name"],
                                   "description": definition["description"],
                                   "inputSchema": {"json": definition["input_schema"]}}})
    assert tools, "the fixture produced no usable tools"

    validate_parameters({"modelId": "claude-haiku-4-5",
                         "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                         "toolConfig": {"tools": tools}}, shape)


# ── Failure isolation ─────────────────────────────────────────────────────────

def test_no_user_id_means_no_tools():
    """Fail-closed. Connectors are per-user rows holding endpoints and credentials;
    there is deliberately no 'fall back to everyone's servers'."""
    assert asyncio.run(registry.bundle_for_user("")) is registry.EMPTY


def test_unreachable_server_degrades_rather_than_raising(monkeypatch):
    from src.mcp_client.client import Server
    monkeypatch.setattr(registry, "servers_for_user", lambda uid: [
        Server(connector_id="c1", slug="srv_c1", name="Down", url="http://127.0.0.1:9/mcp")])
    registry.invalidate()

    bundle = asyncio.run(registry.bundle_for_user("u1"))
    assert bundle.defs == []
    assert "Down" in bundle.degraded


def test_a_failing_tool_returns_an_error_not_an_exception(monkeypatch):
    """The model must see a normal tool result saying it failed. A raise here would
    end the conversation instead of the tool call."""
    from src.mcp_client.client import Server

    async def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("src.mcp_client.client.call_tool", boom)
    call = registry._make_caller(Server("c1", "srv_c1", "Down", "http://x/mcp"), "t")
    out = call(arg=1)
    assert "error" in out and "connection refused" in out["error"]


def test_tool_call_timeout_is_reported_not_raised(monkeypatch):
    from src.mcp_client.client import Server

    async def hang(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr("src.mcp_client.client.call_tool", hang)
    monkeypatch.setattr(registry, "CALL_TIMEOUT", 0.05)
    call = registry._make_caller(Server("c1", "srv_c1", "Slow", "http://x/mcp"), "t")
    assert "timed out" in call()["error"]


def test_builtin_tool_names_cannot_be_shadowed(monkeypatch):
    from src.mcp_client.client import Server

    monkeypatch.setattr(registry, "servers_for_user", lambda uid: [
        Server(connector_id="c1", slug="srv_c1", name="S", url="http://x/mcp")])

    async def fake_discover(server):
        return [{"name": "read_file", "description": "d", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr(registry, "_discover", fake_discover)
    registry.invalidate()

    bundle = asyncio.run(registry.bundle_for_user("u1", reserved={"mcp__srv_c1__read_file"}))
    assert bundle.defs == [], "a reserved name must not be re-issued"


# ── The import constraint ─────────────────────────────────────────────────────

def test_registry_module_does_not_import_mcp_at_module_scope():
    """`src/main.py` imports the advisor chain at module scope. If anything on that
    chain imported `mcp` eagerly, the whole app would fail to boot wherever the
    optional package is missing — which was true of every dev machine until recently.
    """
    import ast
    import pathlib

    for module in ("client.py", "registry.py", "adapter.py", "naming.py", "__init__.py"):
        source = pathlib.Path("src/mcp_client", module).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset == 0:
                names = [a.name for a in getattr(node, "names", [])]
                target = getattr(node, "module", "") or ""
                assert not target.startswith("mcp") and not any(
                    n.split(".")[0] == "mcp" for n in names), (
                    f"{module} imports `mcp` at module scope")


# ── Deploying must not seed the knowledge graph ───────────────────────────────
# Not strictly MCP, but it is the same change: the graph is filled from MCP connectors
# during a demo and from nothing else. The nightly ontology_delta_job wrote ~700
# synthetic nodes at 02:00 UTC, which quietly refilled a graph someone had emptied.

def test_graph_writing_jobs_are_disabled_by_default():
    from src.scheduler.jobs import JOB_DEFS, job_is_enabled

    by_id = {j["id"]: j for j in JOB_DEFS}
    for job_id in ("ontology_delta_job", "correlation_refresh_job"):
        assert job_id in by_id, f"{job_id} disappeared — did the job list change?"
        assert by_id[job_id].get("default_enabled") is False
        assert not job_is_enabled(by_id[job_id]), (
            f"{job_id} would be scheduled, and it writes to the knowledge graph")


def test_a_job_can_still_be_turned_on(monkeypatch):
    """Disabled by DEFAULT, not permanently — the Data Loader toggle must still work."""
    from src.scheduler import jobs

    monkeypatch.setattr("src.services.ontology_version_service.load_scheduler_config",
                        lambda job_id: {"enabled": True})
    assert jobs.job_is_enabled({"id": "ontology_delta_job", "default_enabled": False})


def test_unrelated_jobs_keep_their_own_default():
    """online_eval_job carries its own enabled flag; this change must not alter it."""
    from src.scheduler.jobs import JOB_DEFS

    others = [j for j in JOB_DEFS
              if j["id"] not in ("ontology_delta_job", "correlation_refresh_job")]
    assert others, "expected other scheduled jobs to exist"
    for job in others:
        assert "default_enabled" not in job or job["default_enabled"] is True


# ── SSRF guard ────────────────────────────────────────────────────────────────
# The endpoint is a user-supplied URL the BACKEND dereferences, from inside the VPC.
# 169.254.169.254 is the ECS task-role credential endpoint, and a stored connector row
# would reach it on every chat turn.

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/mcp",          # instance metadata
    "http://169.254.170.2/mcp",            # ECS task metadata
    "file:///etc/passwd",
    "ftp://example.com/mcp",
    "http:///mcp",                         # no host
])
def test_dangerous_endpoints_are_refused(url):
    from src.mcp_client import client
    with pytest.raises(client.McpBlocked):
        client._guard_url(url)


def test_private_endpoints_are_allowed():
    """Deliberate: the sample servers are VPC-internal and reached by Service Connect
    alias, so blocking RFC1918 would block the feature. Link-local is the range that
    actually matters, and it is blocked above."""
    from src.mcp_client import client
    client._guard_url("http://127.0.0.1:8081/mcp")


def test_task_group_errors_are_unwrapped():
    """anyio wraps a plain 503 as "unhandled errors in a TaskGroup (1 sub-exception)",
    which is what an operator would otherwise see in the Test result."""
    from src.mcp_client.client import _explain

    inner = ConnectionRefusedError("connection refused")
    assert "connection refused" in _explain(ExceptionGroup("grouped", [inner]))
    assert "ValueError: boom" == _explain(ValueError("boom"))
    assert _explain(None) == "unknown error"


# ── Graph ingest ──────────────────────────────────────────────────────────────

def test_list_results_arrive_one_block_per_item():
    """FastMCP returns a list as one content block PER ITEM, so joining them with
    newlines produces concatenated JSON that no parser accepts. This is why the client
    keeps `blocks` separate from `text`."""
    from src.mcp_client.ingest import _parse

    blocks = ['{"id": "a", "_label": "Service"}', '{"id": "b", "_label": "Service"}']
    assert [r["id"] for r in _parse(blocks)] == ["a", "b"]

    # A single block holding an array, and an envelope, must both still work.
    assert len(_parse(['[{"id": "a"}, {"id": "b"}]'])) == 2
    assert len(_parse(['{"items": [{"id": "a"}]}'])) == 1
    assert _parse(["not json at all"]) is None


@pytest.mark.parametrize("tool,expected", [
    ("list_services", "Service"),
    ("list_incidents", "Incident"),
    ("list_repositories", "Repository"),
    ("list_deploys", ""),        # "Deploy" is not an ontology label; declared _label wins
    ("delete_order", ""),        # not a lister at all
    ("get_service_health", ""),
])
def test_label_inference(tool, expected):
    from src.mcp_client.ingest import _labels, infer_label
    assert infer_label(tool, _labels()) == expected


def test_unmappable_records_are_counted_not_dropped(monkeypatch):
    """A loader that quietly discards half its input is worse than one that fails:
    nobody finds out until the graph answers a question wrongly."""
    import src.mcp_client as pkg
    from src.mcp_client import ingest
    from src.mcp_client.client import Server

    server = Server("c1", "srv_c1", "Junk", "http://x/mcp")
    monkeypatch.setattr(pkg, "servers_for_user", lambda uid: [server])
    monkeypatch.setattr("src.mcp_client.client.list_tools", _async([
        {"name": "list_widgets", "description": "d", "inputSchema": {"type": "object"}}]))
    monkeypatch.setattr("src.mcp_client.client.call_tool", _async({
        "ok": True, "blocks": ['{"nope": 1}', '{"also": "no id"}'], "text": ""}))

    result = ingest.ingest_from_mcp("u1", ["c1"])
    assert result["nodes_added"] == 0
    assert result["skipped"] == 2, "unmappable records must be reported"


def test_nested_values_are_flattened_out_of_props():
    """Neo4j properties cannot hold nested maps; a driver error mid-load would abort
    everything after it."""
    from src.mcp_client.ingest import _props

    out = _props({"id": "a", "_label": "Service", "tags": ["x", "y"],
                  "nested": {"deep": 1}, "rows": [{"a": 1}], "n": 3, "ok": True})
    assert out == {"id": "a", "tags": ["x", "y"], "n": 3, "ok": True}


def _async(value):
    """Helper: a coroutine function returning `value`."""
    async def _fn(*a, **k):
        return value
    return _fn


# ── RCA evidence injection ────────────────────────────────────────────────────

def test_rca_evidence_is_bounded_and_read_only(monkeypatch):
    """RCA must only call tools that read, and must never be delayed by a server that
    misbehaves — an incident analysis waiting on a flaky connector is worse than one
    without the extra evidence."""
    import asyncio

    from src.agents import rca_agent
    from src.mcp_client.registry import Bundle

    called: list[str] = []

    def _tool(name):
        def fn(**kwargs):
            called.append(name)
            return {"result": f"data from {name}"}
        return fn

    bundle = Bundle(
        defs=[{"name": n} for n in ("mcp__s__get_service_health", "mcp__s__delete_everything")],
        dispatch={"mcp__s__get_service_health": _tool("get_service_health"),
                  "mcp__s__delete_everything": _tool("delete_everything")})

    async def fake_bundle(user_id, reserved=None):
        return bundle

    monkeypatch.setattr("src.mcp_client.bundle_for_user", fake_bundle)

    out = asyncio.run(rca_agent._mcp_evidence("u1", "checkout-svc", lambda m: None))
    assert "get_service_health" in called
    assert "delete_everything" not in called, "RCA must not call write-shaped tools"
    assert "data from get_service_health" in out


def test_rca_evidence_is_empty_without_a_user():
    import asyncio

    from src.agents import rca_agent
    assert asyncio.run(rca_agent._mcp_evidence("", "svc", lambda m: None)) == ""


def test_rca_evidence_survives_a_broken_server(monkeypatch):
    import asyncio

    from src.agents import rca_agent

    async def boom(user_id, reserved=None):
        raise ConnectionError("server down")

    monkeypatch.setattr("src.mcp_client.bundle_for_user", boom)
    assert asyncio.run(rca_agent._mcp_evidence("u1", "svc", lambda m: None)) == ""


def test_ingest_refuses_to_run_on_the_event_loop():
    """It drives the async MCP client with asyncio.run() and writes to Neo4j with
    blocking calls, so it must run on a worker thread.

    Called from a coroutine it used to fail inside the first tool call with
    "asyncio.run() cannot be called from a running event loop", reported per-server as
    though the SERVER had failed — a misleading symptom that cost real debugging time
    on a deployed environment. Now it says what is wrong.
    """
    import asyncio

    from src.mcp_client.ingest import ingest_from_mcp

    async def main():
        return ingest_from_mcp("u1", ["c1"])

    with pytest.raises(RuntimeError, match="run_in_executor"):
        asyncio.run(main())


def test_disabled_jobs_do_not_advertise_a_next_run():
    """`next_run` is derived from the cron string, not from APScheduler, so without
    this it promises "tomorrow at 02:00" for a job that is not scheduled at all."""
    from src.scheduler.jobs import get_job_list

    by_id = {j["id"]: j for j in get_job_list()}
    for job_id in ("ontology_delta_job", "correlation_refresh_job"):
        job = by_id[job_id]
        assert job["enabled"] is False
        assert job["next_run"] is None, f"{job_id} advertises a run it will not make"
        assert job["schedule_human"] == "Disabled"


# ── Every connector must be visible ───────────────────────────────────────────
# A connector that shows on the Connectors page and then silently is not on the MCP
# screen or the Data Loader leaves the user comparing two lists and guessing which of
# their servers vanished. Existence and usability are separate questions.

def _rows():
    return [
        {"connectorId": "c1", "type": "mcp", "name": "Configured",
         "config": {"endpoint": "http://mcp-sre:8081/mcp"}},
        {"connectorId": "c2", "type": "mcp", "name": "UI Style",
         "config": {"baseUrl": "http://mcp-shop:8082/mcp"}},   # what the form writes
        {"connectorId": "c3", "type": "mcp", "name": "Not Finished", "config": {}},
        {"connectorId": "c4", "type": "git", "name": "Not MCP", "config": {}},
    ]


def test_mcp_rows_returns_unconfigured_connectors_too(monkeypatch):
    from src.mcp_client import registry

    monkeypatch.setattr("src.database.dynamo_client.query_items",
                        lambda *a, **k: _rows())
    rows = registry.mcp_rows("u1")
    assert [r["name"] for r in rows] == ["Configured", "UI Style", "Not Finished"]


def test_servers_for_user_excludes_only_the_undiallable(monkeypatch):
    from src.mcp_client import registry

    monkeypatch.setattr("src.database.dynamo_client.query_items",
                        lambda *a, **k: _rows())
    names = [s.name for s in registry.servers_for_user("u1")]
    assert names == ["Configured", "UI Style"], "baseUrl rows must still be diallable"


def test_both_endpoint_and_baseurl_are_accepted():
    """The API and McpConnector use `endpoint`; the Connectors form writes `baseUrl`.
    A row created either way must be visible to the other."""
    from src.mcp_client.registry import endpoint_of

    assert endpoint_of({"config": {"endpoint": "http://a/mcp"}}) == "http://a/mcp"
    assert endpoint_of({"config": {"baseUrl": "http://b/mcp"}}) == "http://b/mcp"
    assert endpoint_of({"config": {}}) == ""


def test_the_forms_auth_token_is_actually_used():
    """The Connectors form offers an optional Auth Token and writes config.token.
    Nothing read it, so a connector configured with a token authenticated with nothing
    and failed with a bare 401 — with the token sitting right there in the form."""
    from src.mcp_client.registry import to_server

    plain = to_server({"connectorId": "c1", "name": "S",
                       "config": {"endpoint": "http://a/mcp", "token": "abc123"}})
    assert plain.headers["Authorization"] == "Bearer abc123"

    # Already prefixed by the user — must not become "Bearer Bearer ...".
    prefixed = to_server({"connectorId": "c1", "name": "S",
                          "config": {"endpoint": "http://a/mcp", "token": "Bearer xyz"}})
    assert prefixed.headers["Authorization"] == "Bearer xyz"

    # An explicit header wins over the token field.
    explicit = to_server({"connectorId": "c1", "name": "S",
                          "config": {"endpoint": "http://a/mcp", "token": "abc",
                                     "headers": {"Authorization": "Basic zzz"}}})
    assert explicit.headers["Authorization"] == "Basic zzz"


def test_a_row_with_no_endpoint_is_not_diallable():
    from src.mcp_client.registry import to_server
    assert to_server({"connectorId": "c3", "name": "S", "config": {}}) is None
