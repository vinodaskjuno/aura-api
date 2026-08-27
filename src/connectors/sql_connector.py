import uuid
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from src.connectors.base import AbstractConnector, SyncResult
DATA_NS = "http://ontology.aura.com/data#"


class SqlConnector(AbstractConnector):
    """SQL connector using SQLAlchemy (synchronous engine)."""

    def _get_engine(self):
        conn_str = self.config.get("connection_string", "")
        if not conn_str:
            raise ValueError("No connection_string configured")
        return create_engine(conn_str)

    def test_connection(self) -> tuple[bool, str]:
        try:
            engine = self._get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return True, "Connected"
        except ValueError as exc:
            return False, str(exc)
        except SQLAlchemyError as exc:
            return False, f"Database error: {exc}"
        except Exception as exc:
            return False, f"Unexpected error: {exc}"

    def sync(self) -> SyncResult:
        result = SyncResult()
        try:
            engine = self._get_engine()
            inspector = inspect(engine)
            table_names = inspector.get_table_names()
            connector_name = self.config.get("name", "sql_source")
            source_id = self.config.get("source_id", connector_name.replace(" ", "_"))

            ttl_triples = []
            for table in table_names:
                safe_table = table.replace("-", "_").replace(" ", "_")
                iri = f"{DATA_NS}{source_id}_{safe_table}"
                ttl_triples.append(
                    f'<{iri}> a data:DataSource ;\n'
                    f'    core:name "{table}" ;\n'
                    f'    data:sourceType "sql_table" ;\n'
                    f'    data:parentSource "{source_id}" .'
                )
                result.entities_added += 1

            engine.dispose()
        except ValueError as exc:
            result.errors.append(str(exc))
        except SQLAlchemyError as exc:
            result.errors.append(f"Database error: {exc}")
        except Exception as exc:
            result.errors.append(f"Sync error: {exc}")
        return result

    def get_metadata(self) -> list[dict]:
        try:
            engine = self._get_engine()
            inspector = inspect(engine)
            table_names = inspector.get_table_names()
            engine.dispose()
            return [{"table": t} for t in table_names]
        except Exception as exc:
            return [{"error": str(exc)}]
