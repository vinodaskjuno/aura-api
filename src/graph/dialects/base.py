"""What differs between graph engines, expressed as data rather than branching.

The product is deployed per client, and each client permits a different engine, so
no module above this layer may name an engine. Call sites ask the dialect for a
capability or a fragment of syntax; they never check `if engine == "neo4j"`.

Translation happens at the session boundary (see `adapt`) instead of at the ~41
individual call sites that embed `elementId(...)`. That is a deliberate trade: a
narrow, well-tested rewrite in one place beats 41 edits plus every future query
someone writes without remembering to use a helper — including the 23 raw
`with session()` blocks that live outside the graph package entirely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dialect:
    name: str

    # ── Capabilities ────────────────────────────────────────────────────────
    # Callers branch on capability, never on engine name, so adding an engine
    # never means editing a caller.
    supports_fulltext: bool = True
    supports_apoc: bool = True
    supports_multi_database: bool = True

    # ── Syntax ──────────────────────────────────────────────────────────────
    # Ordered (pattern, replacement) pairs applied to every statement.
    rewrites: tuple[tuple[re.Pattern, str], ...] = field(default=())

    def session_kwargs(self, database: str) -> dict:
        """Keyword arguments for driver.session(). Engines without multi-database
        support reject `database=` outright rather than ignoring it."""
        return {"database": database} if self.supports_multi_database else {}

    def node_id_expr(self, var: str = "n") -> str:
        """Expression yielding a stable-within-this-engine node identifier."""
        return f"elementId({var})"

    def constraint_ddl(self, label: str, prop: str = "externalId") -> str:
        slug = label.lower().replace(" ", "_")
        return (f"CREATE CONSTRAINT {slug}_eid IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")

    def node_index_ddl(self, label: str, prop: str) -> str:
        name = f"idx_{label.lower()}_{prop.lower()}"
        return f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"

    def edge_index_ddl(self, rel_type: str, prop: str) -> str | None:
        """None means the engine cannot express this index; the caller skips it.

        Index DDL is rendered from a declarative spec rather than translated as a
        string, because the two engines disagree on shape, not just spelling —
        Memgraph has no named indexes and no IF NOT EXISTS."""
        name = f"idx_rel_{rel_type.lower()}_{prop.lower()}"
        return (f"CREATE INDEX {name} IF NOT EXISTS "
                f"FOR ()-[r:{rel_type}]-() ON (r.{prop})")

    def bulk_delete_statement(self, match: str) -> str:
        """Delete every node the `match` clause selects, plus its relationships.

        Batched on Neo4j because the container runs a 1 GB heap
        (docker/neo4j.Dockerfile) and a single-transaction DETACH DELETE over a few
        thousand connected nodes OOMs the JVM and takes the task down with it.

        `match` is built from a fixed predicate in graph/wipe.py, never from user
        input — this interpolates it directly and would be an injection point
        otherwise.
        """
        # `CALL (n) { … }` rather than the older `CALL { WITH n … }`: the latter is
        # deprecated and warns on every call. Requires Neo4j 5.23+ — verified against
        # the deployed 5.26.30 and local 2026.07.1.
        return f"{match} CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 5000 ROWS"

    def adapt(self, cypher: str) -> str:
        """Translate a statement written in the reference dialect (Neo4j 5)."""
        for pattern, replacement in self.rewrites:
            cypher = pattern.sub(replacement, cypher)
        return cypher
