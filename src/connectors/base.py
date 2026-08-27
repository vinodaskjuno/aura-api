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
