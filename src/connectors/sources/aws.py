"""AWS connector — discovers EC2, RDS, Lambda, EKS, S3, and IAM resources."""
from __future__ import annotations
from typing import Any
from ..base import AbstractConnector, SyncResult


class AwsConnector(AbstractConnector):
    """
    Config keys:
      region: str                     default us-east-1
      access_key_id: str | None       uses env/profile if not set
      secret_access_key: str | None
      services: list[str]             default ['ec2', 'rds', 'lambda', 'eks', 's3', 'iam']
    """

    _DEFAULT_SERVICES = ["ec2", "rds", "lambda", "eks", "s3", "iam"]

    def test_connection(self) -> tuple[bool, str]:
        try:
            import boto3
            session = self._session()
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            return True, f"Account: {identity['Account']}, ARN: {identity['Arn']}"
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        services = self.config.get("services", self._DEFAULT_SERVICES)
        for svc in services:
            try:
                resources = self._discover(svc)
                result.entities_added += len(resources)
            except Exception as exc:
                result.errors.append(f"{svc}: {exc}")
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        try:
            return self._discover("ec2")[:5]
        except Exception:
            return []

    def _session(self):
        import boto3
        kwargs: dict[str, Any] = {"region_name": self.config.get("region", "us-east-1")}
        if self.config.get("access_key_id"):
            kwargs["aws_access_key_id"] = self.config["access_key_id"]
            kwargs["aws_secret_access_key"] = self.config["secret_access_key"]
        return boto3.Session(**kwargs)

    def _discover(self, service: str) -> list[dict[str, Any]]:
        session = self._session()
        if service == "ec2":
            client = session.client("ec2")
            resp = client.describe_instances(MaxResults=100)
            return [r for res in resp["Reservations"] for r in res["Instances"]]
        if service == "rds":
            client = session.client("rds")
            return client.describe_db_instances()["DBInstances"]
        if service == "lambda":
            client = session.client("lambda")
            return client.list_functions()["Functions"]
        if service == "s3":
            client = session.client("s3")
            return client.list_buckets()["Buckets"]
        if service == "eks":
            client = session.client("eks")
            names = client.list_clusters()["clusters"]
            return [{"name": n} for n in names]
        if service == "iam":
            client = session.client("iam")
            return client.list_roles()["Roles"][:50]
        return []
