"""Resolve a free-text alert/alarm name to a real service name.

Replaces the two hardcoded copies of `_infer_service()` (routers/aiops.py and
agents/aiops_agent.py), both of which map "payment" -> "PaymentService" from a fixed
table. Every correlation downstream inherits that guess, so it is load-bearing and
worth getting right.

Resolution order: exact Service node -> provider-reported service -> substring match
against known names -> keyword table (last resort, preserved for compatibility).
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Last-resort table, kept identical in spirit to the original _infer_service().
_KEYWORDS: list[tuple[str, str]] = [
    ("payment", "PaymentService"),
    ("fraud", "FraudDetectionService"),
    ("auth", "AuthService"),
    ("login", "AuthService"),
    ("order", "OrderService"),
    ("checkout", "CheckoutService"),
    ("notification", "NotificationService"),
    ("k8s", "Kubernetes"),
    ("kube", "Kubernetes"),
    ("eks", "Kubernetes"),
    ("rds", "Database"),
    ("postgres", "Database"),
    ("mysql", "Database"),
    ("dynamo", "Database"),
    ("lambda", "LambdaFunctions"),
    ("api", "ApiGateway"),
    ("gateway", "ApiGateway"),
]


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def known_services() -> list[str]:
    try:
        from src.graph.neo4j_client import list_service_names
        return list_service_names()
    except Exception as exc:  # noqa: BLE001
        log.debug("known_services unavailable: %s", exc)
        return []


def resolve(text: str, candidates: list[str] | None = None) -> str:
    """Best-effort service name for an alarm/alert/incident title."""
    if not text:
        return "UnknownService"
    names = list(candidates or []) or known_services()
    norm = _normalize(text)

    # 1. exact (case-insensitive) match on a known service name
    for n in names:
        if n and n.lower() == text.strip().lower():
            return n
    # 2. a known service name appears as a whole token in the text
    tokens = set(norm.split())
    for n in names:
        if _normalize(n) in norm or (n and n.lower() in tokens):
            return n
    # 3. longest known name that is a substring
    subs = sorted([n for n in names if n and _normalize(n) and _normalize(n) in norm],
                  key=len, reverse=True)
    if subs:
        return subs[0]
    # 4. keyword table
    for kw, svc in _KEYWORDS:
        if kw in norm:
            return svc
    return "UnknownService"


def resolve_all(texts: list[str]) -> dict[str, str]:
    names = known_services()
    return {t: resolve(t, names) for t in texts}
