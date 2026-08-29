"""Engine-neutral name for the graph API. Prefer this in new code.

`neo4j_client` is the historical name and still holds the implementation, because
45 modules import it and keeping that surface intact is what lets existing code run
unchanged against either engine. The module name is a misnomer now — it has not
been Neo4j-specific since connection handling moved to `src/graph/backends.py` —
so new code should import from here and the old name can retire gradually.
"""
from src.graph.neo4j_client import *          # noqa: F401,F403
from src.graph import neo4j_client as _impl

__all__ = [name for name in dir(_impl) if not name.startswith("_")]
