"""Shared test fixtures.

The repo had no conftest.py, no pytest.ini and no CI, so nothing here could be
exercised without live AWS. These fakes are the minimum needed to test the
observability pipeline end to end offline.
"""
from __future__ import annotations

import os

# MUST run before any test module imports src.graph.neo4j_client. That module caches
# `_driver_attempted` for the process lifetime, so whichever test file imports it
# first decides whether the whole session sees a live graph. test_ontology_lens_api.py
# sets these at module scope and was silently relying on being collected first;
# conftest.py is imported before every test module, which makes it deterministic.
os.environ.setdefault("SKIP_BOOTSTRAP", "1")
os.environ.setdefault("NEO4J_ENABLED", "false")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "observability" / "eval" / "fixtures"


# ── No real LLM calls from tests ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Fail any bedrock-runtime call instead of letting a test spend real money.

    Several agents construct `boto3.client("bedrock-runtime")` directly. While
    BEDROCK_MODEL_ID pointed at a retired model these failed instantly; once it was
    fixed the same code paths started making genuine paid inference calls, and the
    suite went from 5s to 142s. Agents already handle an LLM failure by degrading,
    so raising here exercises the fallback rather than the wallet.

    Tests that need model output install a stub via the `stub_llm` fixture.
    """
    import boto3

    real_client = boto3.client

    def guarded(service_name, *args, **kwargs):
        if service_name in ("bedrock-runtime", "bedrock"):
            raise RuntimeError(
                "Real LLM call attempted in a test. Use the stub_llm fixture.")
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", guarded)


# ── In-memory DynamoDB ───────────────────────────────────────────────────────

def _key_attrs_for(table: str) -> tuple[str, ...]:
    """Key attributes for a table, so the double can replace like the real thing."""
    from src.database.dynamo_client import _COMPOSITE_TABLES, TABLE_SCHEMAS
    if table in _COMPOSITE_TABLES:
        return _COMPOSITE_TABLES[table]
    for schema in TABLE_SCHEMAS:
        if schema["name"] == table:
            return tuple(k for k in (schema.get("pk"), schema.get("sk")) if k)
    return ()


def _reject_floats(table: str, value, path: str = "") -> None:
    """Raise on any surviving Python float, the way boto3 does.

    boto3's own error names no field, so a nested float (a confidence score inside a
    findings list) fails the write with a traceback pointing at boto3 rather than at
    the data. This double reports the path, so a test failure is actionable.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise TypeError(f"DynamoDB would reject a float at {table}{path} "
                        f"({value!r}); use Decimal")
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_floats(table, v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_floats(table, v, f"{path}[{i}]")


class FakeDynamo:
    """Enough of dynamo_client's surface for the observability store."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}

    def put_item(self, table, item):
        # Mirror real DynamoDB: an empty string is not a legal value for ANY key
        # attribute. The production wrapper makes GSI keys sparse; anything still
        # empty afterwards is a genuine error and must surface here too, or this
        # double silently accepts writes that fail against real AWS.
        from src.database.dynamo_client import (
            _COMPOSITE_TABLES, _decimalize, _drop_empty_gsi_keys,
        )
        item = _decimalize(_drop_empty_gsi_keys(table, dict(item)))
        for key_attr in _COMPOSITE_TABLES.get(table, ()):
            if item.get(key_attr) == "":
                raise ValueError(
                    f"DynamoDB would reject this write: key attribute "
                    f"'{key_attr}' on table '{table}' is an empty string")
        _reject_floats(table, item)
        # Real put_item REPLACES the item with the same key; appending instead made
        # get_item return the stale first write, so any code relying on overwrite
        # semantics passed here and behaved differently against AWS.
        rows = self.tables.setdefault(table, [])
        key_attrs = _key_attrs_for(table)
        if key_attrs:
            for i, existing in enumerate(rows):
                if all(existing.get(k) == item.get(k) for k in key_attrs):
                    rows[i] = item
                    return
        rows.append(item)

    def get_item(self, table, key):
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in key.items()):
                return row
        return None

    def query_items(self, table, pk_name, pk_value, sk_name=None, sk_value=None,
                    index_name=None, limit=100):
        rows = [r for r in self.tables.get(table, []) if r.get(pk_name) == pk_value]
        return rows[:limit]

    def query_range(self, table, pk_name, pk_value, sk_name, sk_prefix=None,
                    sk_from=None, sk_to=None, index_name=None, limit=1000):
        return [r for r in self.tables.get(table, []) if r.get(pk_name) == pk_value][:limit]

    def scan_items(self, table, filter_expr=None, limit=500):
        return list(self.tables.get(table, []))[:limit]

    def update_item(self, table, key, updates):
        from src.database.dynamo_client import _decimalize
        updates = {k: _decimalize(v) for k, v in updates.items()}
        _reject_floats(table, updates)
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in key.items()):
                row.update(updates)
                return row
        return None

    def delete_item(self, table, key):
        rows = self.tables.get(table, [])
        self.tables[table] = [r for r in rows
                              if not all(r.get(k) == v for k, v in key.items())]

    def atomic_add(self, table, key, field, delta):
        row = self.get_item(table, key) or {**key}
        row[field] = float(row.get(field, 0)) + float(delta)
        if row not in self.tables.setdefault(table, []):
            self.tables[table].append(row)

    def put_item_if_absent(self, table, item, key_fields):
        for row in self.tables.get(table, []):
            if all(row.get(f) == item.get(f) for f in key_fields):
                return False
        self.put_item(table, item)
        return True


@pytest.fixture
def fake_dynamo(monkeypatch):
    fake = FakeDynamo()
    import src.database.dynamo_client as dc
    for name in ("put_item", "get_item", "query_items", "query_range", "scan_items",
                 "update_item", "delete_item", "atomic_add", "put_item_if_absent"):
        monkeypatch.setattr(dc, name, getattr(fake, name), raising=False)
    # The store and dispatcher import these by name at call time.
    import src.observability.store as store
    for name in ("put_item", "query_items", "query_range", "scan_items", "update_item"):
        monkeypatch.setattr(store, name, getattr(fake, name), raising=False)
    return fake


# ── In-memory S3 ─────────────────────────────────────────────────────────────

@pytest.fixture
def fake_s3(monkeypatch):
    blobs: dict[str, object] = {}

    def put_json(bucket, key, data):
        blobs[f"{bucket}/{key}"] = data
        return f"s3://{bucket}/{key}"

    def get_json(bucket, key):
        return blobs.get(f"{bucket}/{key}")

    def put_object(bucket, key, body, content_type="application/octet-stream"):
        blobs[f"{bucket}/{key}"] = body
        return f"s3://{bucket}/{key}"

    import src.storage.s3_client as s3
    monkeypatch.setattr(s3, "put_json", put_json)
    monkeypatch.setattr(s3, "get_json", get_json)
    monkeypatch.setattr(s3, "put_object", put_object)
    return blobs


# ── Graph stub ───────────────────────────────────────────────────────────────

@pytest.fixture
def fake_graph(monkeypatch):
    written: dict[str, list] = {"nodes": [], "rels": []}
    import src.graph.neo4j_client as g
    monkeypatch.setattr(g, "is_available", lambda: False, raising=False)
    monkeypatch.setattr(g, "upsert_node",
                        lambda label, eid, props: written["nodes"].append((label, eid, props)) or {},
                        raising=False)
    monkeypatch.setattr(g, "upsert_relationship",
                        lambda *a, **k: written["rels"].append((a, k)) or True, raising=False)
    monkeypatch.setattr(g, "run_query", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(g, "search_runbooks", lambda **k: [], raising=False)
    monkeypatch.setattr(g, "search_incidents", lambda **k: [], raising=False)
    monkeypatch.setattr(g, "service_neighbours",
                        lambda name, hops=2: {"upstream": ["api-gateway"],
                                              "downstream": ["payment-service"],
                                              "hops": hops, "source": "neo4j"}, raising=False)
    monkeypatch.setattr(g, "list_service_names",
                        lambda limit=500: ["checkout-service", "payment-service"], raising=False)
    return written


# ── Emitter / LLM doubles ────────────────────────────────────────────────────

@pytest.fixture
def captured_events():
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    emit.events = events  # type: ignore[attr-defined]
    return emit


@pytest.fixture
def stub_llm():
    """Install a scripted LLM and capture every MASKED prompt for leak assertions."""
    from src.observability.llm import set_llm_override
    captured: list[str] = []

    def install(responder):
        def fn(system, user, model, max_tokens):
            captured.append(system + "\n" + user)
            return responder(system, user)
        set_llm_override(fn)
        return captured

    yield install
    set_llm_override(None)


@pytest.fixture
def fixture_provider():
    from src.observability import registry
    from src.observability.eval.fixture_provider import FixtureProvider

    def install(name="checkout_oom.json", provider_id="fx-loki", provider_type="loki"):
        p = FixtureProvider.from_file(FIXTURES / name, provider_id=provider_id,
                                      provider_type=provider_type)
        registry.register_override(provider_id, p)
        return p

    yield install
    registry.clear_overrides()


@pytest.fixture
def no_notifications(monkeypatch):
    from src.services.notifications import dispatcher
    monkeypatch.setattr(dispatcher, "send_sync", lambda *a, **k: None)
