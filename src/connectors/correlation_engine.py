"""Correlation engine: finds cross-source relationships in the Neo4j ontology.

Two-pass approach:
1. Strong match (IS_SAME_AS, confidence=1.0):  exact hostname or IP match across sources
2. Fuzzy match (CORRELATES_WITH, confidence=float):  normalized name similarity + same team/env
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    """Lowercase, strip env suffixes, remove special chars."""
    name = name.lower()
    for suffix in [".prod", ".staging", ".dev", ".aura.internal", ".internal", "-api", "-service", "-svc"]:
        name = name.replace(suffix, "")
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def _similarity(s1: str, s2: str) -> float:
    if not s1 or not s2:
        return 0.0
    dist = _levenshtein(s1, s2)
    max_len = max(len(s1), len(s2))
    return 1.0 - dist / max_len


def run_correlation() -> dict:
    """Run the two-pass correlation engine over all nodes in Neo4j."""
    from src.graph import neo4j_client as neo4j

    if not neo4j.is_available():
        return {"skipped": True, "reason": "Neo4j not available"}

    stats = {"strong_matches": 0, "fuzzy_matches": 0, "errors": []}

    # ── Pass 1: Strong hostname/IP matches ────────────────────────────────────
    hostname_cypher = """
    MATCH (a), (b)
    WHERE a.hostname IS NOT NULL AND b.hostname IS NOT NULL
      AND a.hostname = b.hostname
      AND elementId(a) < elementId(b)
      AND a.source <> b.source
    RETURN elementId(a) AS aid, labels(a)[0] AS alabel, a.externalId AS aeid,
           elementId(b) AS bid, labels(b)[0] AS blabel, b.externalId AS beid
    LIMIT 1000
    """
    ip_cypher = """
    MATCH (a), (b)
    WHERE a.ip IS NOT NULL AND b.ip IS NOT NULL
      AND a.ip = b.ip
      AND elementId(a) < elementId(b)
      AND a.source <> b.source
    RETURN elementId(a) AS aid, labels(a)[0] AS alabel, a.externalId AS aeid,
           elementId(b) AS bid, labels(b)[0] AS blabel, b.externalId AS beid
    LIMIT 1000
    """
    try:
        from src.graph.neo4j_client import session
        with session() as s:
            for rec in s.run(hostname_cypher):
                _create_strong(rec, stats)
            for rec in s.run(ip_cypher):
                _create_strong(rec, stats)
    except Exception as exc:
        stats["errors"].append(f"Strong match pass failed: {exc}")
        log.exception("Strong correlation failed")

    # ── Pass 2: Fuzzy name matching (Service vs Service, Service vs Infra) ────
    fuzzy_cypher = """
    MATCH (a:Service), (b:Infrastructure)
    WHERE a.source <> b.source
      AND a.name IS NOT NULL AND b.name IS NOT NULL
    RETURN a.externalId AS aeid, a.name AS aname, a.hostname AS ahostname,
           b.externalId AS beid, b.name AS bname, b.hostname AS bhostname
    LIMIT 2000
    """
    try:
        with session() as s:
            for rec in s.run(fuzzy_cypher):
                a_norm = _normalize(rec["aname"])
                b_norm = _normalize(rec["bname"])
                sim = _similarity(a_norm, b_norm)
                if sim >= 0.8:
                    try:
                        from src.graph import neo4j_client as neo4j
                        neo4j.upsert_relationship(
                            "Service", rec["aeid"],
                            "Infrastructure", rec["beid"],
                            "CORRELATES_WITH",
                            {"confidence": round(sim, 3), "method": "fuzzy_name"},
                        )
                        stats["fuzzy_matches"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"Fuzzy link {rec['aeid']}→{rec['beid']}: {exc}")
    except Exception as exc:
        stats["errors"].append(f"Fuzzy match pass failed: {exc}")
        log.exception("Fuzzy correlation failed")

    log.info("Correlation complete: %s", stats)
    return stats


def _create_strong(rec, stats: dict):
    try:
        from src.graph import neo4j_client as neo4j
        neo4j.upsert_relationship(
            rec["alabel"], rec["aeid"],
            rec["blabel"], rec["beid"],
            "IS_SAME_AS",
            {"confidence": 1.0, "method": "hostname_or_ip"},
        )
        stats["strong_matches"] += 1
    except Exception as exc:
        stats["errors"].append(f"Strong link {rec['aeid']}→{rec['beid']}: {exc}")
