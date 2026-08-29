"""Named graph backends: one driver, one dialect, one lifecycle per engine.

Replaces the single module-level driver that `neo4j_client` used to hold. A
deployment configures one engine or several; with one configured the others are
simply absent, which is what a per-client install looks like.

Every statement is translated by the backend's dialect on the way out — see
`_AdaptingSession`. Doing it here rather than at each call site means the 23 raw
`with session()` blocks that live outside this package are covered too, and so is
every query written in future.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generator

from src.graph.dialects import DIALECTS, Dialect

log = logging.getLogger(__name__)

DEFAULT_BACKEND = "neo4j"


@dataclass(frozen=True)
class BackendConfig:
    name: str
    uri: str
    user: str
    password: str
    database: str = "neo4j"
    dialect_name: str = "neo4j"

    @property
    def dialect(self) -> Dialect:
        return DIALECTS.get(self.dialect_name, DIALECTS[DEFAULT_BACKEND])


class _AdaptingSession:
    """Proxies a driver session, translating each statement for the dialect.

    Only `run` is intercepted; everything else (transactions, `close`, context
    management) passes through untouched.
    """

    def __init__(self, session, dialect: Dialect):
        self._session = session
        self._dialect = dialect

    def run(self, query, parameters=None, **kwargs):
        return self._session.run(self._dialect.adapt(query), parameters, **kwargs)

    def __getattr__(self, item):
        return getattr(self._session, item)


class Backend:
    """One engine. Builds its driver lazily and caches it until `close()`."""

    def __init__(self, config: BackendConfig):
        self.config = config
        self._driver: Any = None
        # `attempted` distinguishes "not tried yet" from "tried and failed", so a
        # dead engine is not retried on every single call.
        self._attempted = False
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def dialect(self) -> Dialect:
        return self.config.dialect

    def driver(self):
        if self._driver is not None:
            return self._driver
        with self._lock:
            if self._driver is not None:
                return self._driver
            if self._attempted:
                return None
            self._attempted = True
            try:
                from neo4j import GraphDatabase
                driver = GraphDatabase.driver(
                    self.config.uri,
                    auth=(self.config.user, self.config.password),
                )
                driver.verify_connectivity()
                self._driver = driver
                log.info("Graph backend %r connected at %s (dialect=%s)",
                         self.name, self.config.uri, self.dialect.name)
            except Exception as exc:  # noqa: BLE001 — the app stays up without a graph
                log.warning("Graph backend %r unavailable: %s", self.name, exc)
                self._driver = None
        return self._driver

    def is_available(self) -> bool:
        return self.driver() is not None

    @contextmanager
    def session(self) -> Generator:
        driver = self.driver()
        if driver is None:
            raise RuntimeError(f"Graph backend {self.name!r} is not available")
        kwargs = self.dialect.session_kwargs(self.config.database)
        with driver.session(**kwargs) as raw:
            yield _AdaptingSession(raw, self.dialect)

    def close(self):
        with self._lock:
            if self._driver:
                try:
                    self._driver.close()
                except Exception as exc:  # noqa: BLE001
                    log.debug("closing backend %r: %s", self.name, exc)
            self._driver = None
            # Reset so the next call retries — this is what lets a backend recover
            # after the engine comes back without restarting the process.
            self._attempted = False


# Statements that change data. A read replayed on every secondary would double the
# query load for nothing, so the fan-out only mirrors writes.
_WRITE_KEYWORDS = ("MERGE", "CREATE", "SET ", "DELETE", "REMOVE", "DROP")


def is_write(cypher: str) -> bool:
    upper = (cypher or "").upper()
    return any(kw in upper for kw in _WRITE_KEYWORDS)


class _FanOutSession:
    """Runs on the primary, mirrors writes to secondaries.

    Mirroring replays the same statement and parameters rather than re-deriving
    anything, which is why this works with no changes at any of the 102 call sites.
    It is safe because every value the app computes (timestamps, ids, hashes) is
    already passed as a parameter, so the replayed statement is deterministic.

    A secondary failure never propagates: the primary has committed, and the write
    goes to the outbox instead.
    """

    def __init__(self, primary, secondaries, dialect):
        self._primary = primary
        self._secondaries = secondaries
        self._dialect = dialect

    def run(self, query, parameters=None, **kwargs):
        result = self._primary.run(self._dialect.adapt(query), parameters, **kwargs)
        if self._secondaries and is_write(query):
            # The driver accepts parameters as a dict OR as keyword arguments, and
            # this codebase uses both — `s.run(cypher, eid=..., props=...)` is the
            # common form. Forwarding only `parameters` silently mirrored every
            # write with no parameters at all, which the secondary then rejected.
            merged = {**(parameters or {}), **kwargs}
            for backend in self._secondaries:
                self._mirror(backend, query, merged)
        return result

    @staticmethod
    def _mirror(backend, query, parameters):
        from src.graph import outbox
        try:
            with backend.session() as s:
                s.run(query, parameters)
        except Exception as exc:  # noqa: BLE001 — never fail the caller for a shadow
            log.warning("mirror to %s failed, queued: %s", backend.name, exc)
            outbox.enqueue(backend.name, query, parameters or {}, str(exc))

    def __getattr__(self, item):
        return getattr(self._primary, item)


@contextmanager
def routed_session() -> Generator:
    """A session honouring the runtime read-source and write-target config."""
    from src.graph import graph_config
    config = graph_config.get_config()

    primary = get_backend(config.read_source or None)
    if primary is None:
        raise RuntimeError("No graph backend is configured")

    secondaries = [b for b in (get_backend(name) for name in config.write_targets)
                   if b is not None and b.name != primary.name]

    driver = primary.driver()
    if driver is None:
        raise RuntimeError(f"Graph backend {primary.name!r} is not available")
    kwargs = primary.dialect.session_kwargs(primary.config.database)
    with driver.session(**kwargs) as raw:
        # The primary's own statements are adapted by _FanOutSession; each
        # secondary adapts independently inside its own session.
        yield _FanOutSession(raw, secondaries, primary.dialect)


# ── Registry ─────────────────────────────────────────────────────────────────

_backends: dict[str, Backend] = {}
_registry_lock = threading.Lock()


def _configs_from_settings() -> dict[str, BackendConfig]:
    """Connection details still come from Settings — they are deployment facts.

    Which backend is *active* deliberately does not live here: get_settings() is
    lru_cached, so anything read from it cannot change without a restart.
    """
    from src.config_settings import get_settings
    s = get_settings()
    configs: dict[str, BackendConfig] = {}
    if getattr(s, "neo4j_enabled", False):
        configs["neo4j"] = BackendConfig(
            name="neo4j", uri=s.neo4j_uri, user=s.neo4j_user,
            password=s.neo4j_password, database=s.neo4j_database,
            dialect_name="neo4j",
        )
    if getattr(s, "memgraph_enabled", False):
        configs["memgraph"] = BackendConfig(
            name="memgraph", uri=s.memgraph_uri, user=s.memgraph_user,
            password=s.memgraph_password, database="memgraph",
            dialect_name="memgraph",
        )
    return configs


def get_backend(name: str | None = None) -> Backend | None:
    """The named backend, or the configured default. None if not configured."""
    with _registry_lock:
        if not _backends:
            for cfg_name, cfg in _configs_from_settings().items():
                _backends[cfg_name] = Backend(cfg)
        if name is None:
            name = DEFAULT_BACKEND if DEFAULT_BACKEND in _backends else next(iter(_backends), "")
        return _backends.get(name)


def configured_names() -> list[str]:
    get_backend()          # force registry population
    with _registry_lock:
        return sorted(_backends)


def reset(name: str | None = None):
    """Close and forget backends. Used by tests, and by a config change that
    repoints a connection at runtime."""
    with _registry_lock:
        targets = [name] if name else list(_backends)
        for key in targets:
            backend = _backends.pop(key, None)
            if backend:
                backend.close()
