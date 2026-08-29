"""Memgraph — Bolt-compatible, so the same driver connects, but the Cypher differs.

Each rewrite below corresponds to something Memgraph rejects outright:

  elementId(x)  Neo4j 5 only. Memgraph's id() returns an integer, so it is wrapped
                in toString() to keep the identifier type consistent with Neo4j's
                string ids — otherwise every caller comparing an id to a string
                silently stops matching.

  startNode/    Named differently. These appear inside the same RETURN maps as
  endNode       elementId, so they must be rewritten in the same pass.

Not expressible as a rewrite, and handled by capability flags instead:

  Full-text     No db.index.fulltext equivalent. search_runbooks falls back to a
                portable CONTAINS scan (see supports_fulltext).
  APOC          Not present. neo4j_client already carries non-APOC fallbacks for
                both apoc.path.subgraphNodes call sites.
  database=     Community has no multi-database; passing it is an error.
"""
from __future__ import annotations

import re

from src.graph.dialects.base import Dialect


class _MemgraphDialect(Dialect):
    def node_id_expr(self, var: str = "n") -> str:
        return f"toString(id({var}))"

    def constraint_ddl(self, label: str, prop: str = "externalId") -> str:
        # Memgraph has no named constraints and no IF NOT EXISTS; re-running is
        # harmless because ensure_schema already tolerates per-statement failure.
        return f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE"


MEMGRAPH = _MemgraphDialect(
    name="memgraph",
    supports_fulltext=False,
    supports_apoc=False,
    supports_multi_database=False,
    rewrites=(
        (re.compile(r"\belementId\s*\(\s*([A-Za-z_]\w*)\s*\)"), r"toString(id(\1))"),
        # Nested form: elementId(startNode(r)) -> toString(id(startNode(r))).
        # Needs its own pattern because the rule above only matches a bare
        # identifier inside the parens, so it never sees a nested call.
        (re.compile(r"\belementId\s*\(\s*(startNode|endNode)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*\)"),
         r"toString(id(\1(\2)))"),
    ),
)
