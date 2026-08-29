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

    def adapt(self, cypher: str) -> str:
        """Translate a statement written in the reference dialect (Neo4j 5)."""
        for pattern, replacement in self.rewrites:
            cypher = pattern.sub(replacement, cypher)
        return cypher
