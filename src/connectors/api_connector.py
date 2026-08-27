import httpx
from src.connectors.base import AbstractConnector, SyncResult


class ApiConnector(AbstractConnector):
    """REST API connector using httpx (synchronous)."""

    def test_connection(self) -> tuple[bool, str]:
        base_url = self.config.get("base_url", "")
        if not base_url:
            return False, "No base_url configured"
        try:
            response = httpx.get(base_url, timeout=10.0)
            response.raise_for_status()
            return True, "Connected"
        except httpx.HTTPStatusError as exc:
            return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        except httpx.TimeoutException:
            return False, f"Connection timed out connecting to {base_url}"
        except httpx.RequestError as exc:
            return False, f"Request error: {exc}"
        except Exception as exc:
            return False, f"Unexpected error: {exc}"

    def sync(self) -> SyncResult:
        return SyncResult()

    def get_metadata(self) -> list[dict]:
        return [{"url": self.config.get("base_url", "")}]
