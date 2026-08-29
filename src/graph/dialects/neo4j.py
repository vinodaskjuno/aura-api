"""Neo4j 5 — the reference dialect. Every query in the codebase is written for it,
so `adapt` is the identity and there is nothing to translate."""
from __future__ import annotations

from src.graph.dialects.base import Dialect

NEO4J = Dialect(
    name="neo4j",
    supports_fulltext=True,
    supports_apoc=True,
    supports_multi_database=True,
    rewrites=(),
)
