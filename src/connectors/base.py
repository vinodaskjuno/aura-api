from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SyncResult:
    entities_added: int = 0
    entities_updated: int = 0
    errors: list[str] = field(default_factory=list)


class AbstractConnector(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]: ...

    @abstractmethod
    def sync(self) -> SyncResult: ...

    @abstractmethod
    def get_metadata(self) -> list[dict]: ...

    # The provenance pipeline a subclass's writes belong to. Overridden by the
    # connectors that write nodes; the default is the generic API pipeline.
    pipeline: str = "api"

    def run_sync(self, *, actor: str = "", trigger: str = "manual") -> SyncResult:
        """Call this, not `sync()` directly.

        The three connectors that write to the graph (kubernetes, pagerduty,
        confluence) are not wired to a route yet. When they are, going through here
        means their nodes are attributed from the first commit rather than after
        someone notices they are not — which is the failure this whole module
        exists to prevent.
        """
        from src.graph import provenance
        with provenance.ensure_run(
            self.pipeline,
            trigger=trigger,
            actor=actor,
            source=type(self).__name__.replace("Connector", "").lower(),
            sourceDetail=str(self.config.get("url") or self.config.get("context") or ""),
            writtenBy=f"connectors.{type(self).__name__}",
        ):
            return self.sync()
