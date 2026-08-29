"""Engine dialects. Import the registry, never a specific engine, above this layer."""
from src.graph.dialects.base import Dialect
from src.graph.dialects.memgraph import MEMGRAPH
from src.graph.dialects.neo4j import NEO4J

DIALECTS: dict[str, Dialect] = {d.name: d for d in (NEO4J, MEMGRAPH)}

__all__ = ["Dialect", "DIALECTS", "NEO4J", "MEMGRAPH"]
