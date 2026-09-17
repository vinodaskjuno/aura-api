"""Pointing a locally-run app at Aura — and saying honestly whether it worked.

Three separate ways this can be silently wrong, all guarded here:

  1. injecting over a developer's own OTEL configuration, because `extra_env` beats
     `os.environ` in a way dict-ordering intuition gets backwards;
  2. handing a stock SDK a base URL and then having no way to attribute its spend,
     because no SDK will send a custom header from an environment variable;
  3. an always-200 ingest making "nothing sent yet" and "your key was refused" look
     identical to everyone, including the developer whose app is reporting.
"""
from __future__ import annotations

import pytest


# ── 1. Never clobber a developer's own collector ────────────────────────────

def test_injection_leaves_an_existing_otel_endpoint_alone(monkeypatch):
    """`RunningApps._start` merges {**toolpath.env(), **extra_env, **spec.env}, and
    `toolpath.env()` is {**os.environ, ...} — so extra_env WINS. Injecting blindly
    would silently redirect a developer's existing exporter at Aura."""
    from src.qatest import agent

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://my-collector:4318")
    command = {"telemetry": {"env": {
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://aura/otlp/v1/traces",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    }}}

    env = agent._telemetry_env(command, "p1")

    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in env
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"


def test_injection_is_empty_when_the_server_sent_nothing(monkeypatch):
    from src.qatest import agent
    assert agent._telemetry_env({}, "p1") == {}


# ── The env the server builds ───────────────────────────────────────────────

@pytest.fixture
def telemetry(monkeypatch):
    from src.routers import qa
    from src.services import gateway_service

    monkeypatch.setattr(gateway_service, "get_or_create_tool_key",
                        lambda *a, **k: {"key": "gw-secret", "keyId": "k1"})
    return qa._telemetry_for


def test_the_endpoint_is_signal_specific(telemetry, monkeypatch):
    """The generic OTEL_EXPORTER_OTLP_ENDPOINT would also route metrics and logs to
    /otlp/v1/metrics and /otlp/v1/logs — Claude Code usage-reconciliation endpoints
    that feed `usage_rollup` under this key's user id. A stranger's FastAPI metrics
    landing there is a data-quality incident nobody would trace back to this feature."""
    from src.config_settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "public_base_url", "https://aura.example", raising=False)

    env = telemetry("p1", {"userId": "u1"})["env"]

    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "https://aura.example/otlp/v1/traces"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert env["OTEL_METRICS_EXPORTER"] == "none"
    assert env["OTEL_LOGS_EXPORTER"] == "none"


def test_the_protocol_is_pinned_to_http(telemetry, monkeypatch):
    """opentelemetry-distro defaults to gRPC and this server speaks HTTP only. Wrong
    here means zero spans, no error, and nothing anywhere for the developer to see —
    the exporter retries forever inside their own process."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "public_base_url", "https://aura.example",
                        raising=False)
    env = telemetry("p1", {"userId": "u1"})["env"]
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"


def test_the_project_id_is_what_joins_the_trace_to_the_project(telemetry, monkeypatch):
    """`aiobs.service.project_of` reads `aura.project` BEFORE service.name, and it is
    the id — not the name — that joins. Names get edited; ids do not."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "public_base_url", "https://aura.example",
                        raising=False)
    env = telemetry("proj-42", {"userId": "u1"})["env"]
    assert "aura.project=proj-42" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_loopback_needs_no_https(telemetry, monkeypatch):
    """The guard exists because the key crosses a network. Over localhost it does not:
    the server handing it out, the app receiving it and the person running both are one
    machine. Demanding TLS there protects nothing and blocks the setup this feature is
    most used in."""
    from src.config_settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "allow_insecure_telemetry_keys", False, raising=False)

    for base in ("http://localhost:8000", "http://127.0.0.1:8000", ""):
        monkeypatch.setattr(s, "public_base_url", base, raising=False)
        out = telemetry("p1", {"userId": "u1"})
        assert out["env"], f"loopback base {base!r} should inject"
        assert "localhost" in out["env"]["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] \
            or "127.0.0.1" in out["env"]["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]


def test_an_unset_base_url_falls_back_rather_than_refusing(telemetry, monkeypatch):
    """`opik_gateway._base_url()` already falls back to the local address for its
    onboarding snippets. This refused outright, so one machine got a working snippet
    and a dead Run-locally."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "public_base_url", "", raising=False)
    out = telemetry("p1", {"userId": "u1"})
    assert out["env"]
    assert out["skipped"] == ""


def test_no_key_is_injected_over_plain_http(telemetry, monkeypatch):
    """These values carry a bearer credential. Putting one into a developer's
    environment to travel in clear text is a decision someone takes knowingly."""
    from src.config_settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "public_base_url", "http://aura-dev.example", raising=False)
    monkeypatch.setattr(s, "allow_insecure_telemetry_keys", False, raising=False)

    out = telemetry("p1", {"userId": "u1"})

    assert out["env"] == {}
    assert "clear text" in out["skipped"]


def test_an_environment_may_opt_in_to_plain_http(telemetry, monkeypatch):
    from src.config_settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "public_base_url", "http://aura-dev.example", raising=False)
    monkeypatch.setattr(s, "allow_insecure_telemetry_keys", True, raising=False)
    assert telemetry("p1", {"userId": "u1"})["env"]


# ── 2. Attribution a stock SDK can actually produce ─────────────────────────

def test_the_key_attributes_spend_when_no_header_can(monkeypatch):
    """A stock Anthropic or OpenAI SDK sends no custom headers, so an app pointed at
    the gateway by ANTHROPIC_BASE_URL alone cannot declare a project. Carrying it on
    the key is the one channel that survives an unmodified SDK."""
    from src.routers.gateway import _attribution
    from src.services.gateway_service import GatewayUser

    class Req:
        headers: dict = {}

    user = GatewayUser("u1", "u", "user_dev", [], project_id="proj-9")
    assert _attribution(Req(), user)["project_id"] == "proj-9"


def test_an_explicit_header_still_wins(monkeypatch):
    """A QA run declares its own attribution and must not be overridden by whichever
    key it happened to use."""
    from src.routers.gateway import _attribution
    from src.services.gateway_service import GatewayUser

    class Req:
        headers = {"X-Aura-Project-Id": "from-header"}

    user = GatewayUser("u1", "u", "user_dev", [], project_id="from-key")
    assert _attribution(Req(), user)["project_id"] == "from-header"


# ── 3. Silence must be distinguishable from refusal ─────────────────────────

@pytest.fixture
def status_row(monkeypatch):
    from src.aiobs import ingest_status
    row: dict = {}
    monkeypatch.setattr(ingest_status, "_read", lambda: dict(row))
    monkeypatch.setattr(ingest_status, "_write", lambda patch: row.update(patch))
    return row


def test_nothing_sent_and_key_refused_are_different_answers(status_row):
    from src.aiobs import ingest_status

    assert ingest_status.status_for("p1")["state"] == "no-spans-yet"

    ingest_status.record_rejected("gw-abcd9f2c", "credential not resolvable")
    refused = ingest_status.status_for("p1")
    assert refused["state"] == "key-refused"
    # The hint, never the credential.
    assert "9f2c" in refused["detail"]
    assert "gw-abcd9f2c" not in refused["detail"]

    ingest_status.record_spans("p1", 3, "u1")
    assert ingest_status.status_for("p1")["state"] == "connected"


def test_a_whole_estate_costs_one_read(status_row, monkeypatch):
    """`status_for` is a read per project, which is right for one panel and wrong for
    a landing page that asks about every project a user has."""
    from src.aiobs import ingest_status

    reads = {"n": 0}
    real = ingest_status._read
    monkeypatch.setattr(ingest_status, "_read",
                        lambda: (reads.__setitem__("n", reads["n"] + 1), real())[1])

    ingest_status.record_spans("p1", 1)
    states = ingest_status.status_for_many(["p1", "p2", "p3"])

    assert states == {"p1": "connected", "p2": "no-spans-yet", "p3": "no-spans-yet"}
    assert reads["n"] == 1
