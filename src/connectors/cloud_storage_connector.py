import boto3
from botocore.exceptions import BotoCoreError, ClientError
from src.connectors.base import AbstractConnector, SyncResult
INFRA_NS = "http://ontology.aura.com/infra#"


class CloudStorageConnector(AbstractConnector):
    """S3 / cloud-storage connector using boto3."""

    def _get_client(self):
        kwargs = {"region_name": self.config.get("region", "us-east-1")}
        if self.config.get("aws_access_key_id"):
            kwargs["aws_access_key_id"] = self.config["aws_access_key_id"]
        if self.config.get("aws_secret_access_key"):
            kwargs["aws_secret_access_key"] = self.config["aws_secret_access_key"]
        if self.config.get("endpoint_url"):
            kwargs["endpoint_url"] = self.config["endpoint_url"]
        return boto3.client("s3", **kwargs)

    def test_connection(self) -> tuple[bool, str]:
        provider = self.config.get("provider", "s3")
        if provider not in ("s3", ""):
            return False, "Not implemented yet"
        try:
            client = self._get_client()
            client.list_buckets()
            return True, "Connected"
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"]["Message"]
            return False, f"AWS error [{code}]: {msg}"
        except BotoCoreError as exc:
            return False, f"BotoCore error: {exc}"
        except Exception as exc:
            return False, f"Unexpected error: {exc}"

    def sync(self) -> SyncResult:
        result = SyncResult()
        provider = self.config.get("provider", "s3")
        if provider not in ("s3", ""):
            result.errors.append("Not implemented yet")
            return result

        try:
            client = self._get_client()
            response = client.list_buckets()
            buckets = response.get("Buckets", [])

            ttl_triples = []
            for bucket in buckets:
                name = bucket.get("Name", "")
                created = bucket.get("CreationDate", "")
                safe_name = name.replace("-", "_").replace(".", "_")
                iri = f"{INFRA_NS}s3_bucket_{safe_name}"
                created_str = created.isoformat() if hasattr(created, "isoformat") else str(created)
                ttl_triples.append(
                    f'<{iri}> a infra:StorageResource ;\n'
                    f'    core:name "{name}" ;\n'
                    f'    infra:storageType "s3_bucket" ;\n'
                    f'    infra:createdAt "{created_str}"^^xsd:dateTime .'
                )
                result.entities_added += 1

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"]["Message"]
            result.errors.append(f"AWS error [{code}]: {msg}")
        except BotoCoreError as exc:
            result.errors.append(f"BotoCore error: {exc}")
        except Exception as exc:
            result.errors.append(f"Sync error: {exc}")

        return result

    def get_metadata(self) -> list[dict]:
        provider = self.config.get("provider", "s3")
        if provider not in ("s3", ""):
            return [{"error": "Not implemented yet"}]
        try:
            client = self._get_client()
            response = client.list_buckets()
            return [{"bucket": b["Name"], "created": str(b.get("CreationDate", ""))} for b in response.get("Buckets", [])]
        except Exception as exc:
            return [{"error": str(exc)}]
