"""Terraform / CloudFormation parser — extracts infra resources, region, account."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class TerraformParser:
    def parse(self, local_path: str) -> dict[str, Any]:
        root = Path(local_path)
        result: dict[str, Any] = {
            "service_name": root.name,
            "tech_stack": ["Terraform"],
            "apis": [],
            "databases": [],
            "downstream_calls": [],
            "dependencies": [],
            "topics": [],
            "environments": [],
            "cloud_resources": [],
            "networks": [],
            "k8s_clusters": [],
            "servers": [],
            "description_hints": "",
        }

        tf_files = list(root.rglob("*.tf"))
        tfvars_files = list(root.rglob("*.tfvars"))
        cfn_files = list(root.rglob("*.yml")) + list(root.rglob("*.yaml"))

        for f in tf_files:
            self._parse_tf_file(f, result)

        for f in tfvars_files:
            self._parse_tfvars(f, result)

        for f in cfn_files:
            self._parse_cloudformation(f, result)

        result["tech_stack"] = list(set(result["tech_stack"]))
        result["description_hints"] = (
            f"Infrastructure repo '{result['service_name']}' managing "
            f"{len(result['cloud_resources'])} cloud resource(s) across "
            f"{', '.join(result['environments']) or 'unknown'} environment(s)."
        )
        return result

    def _parse_tf_file(self, f: Path, result: dict) -> None:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return

        # Provider → region / account
        for provider, region in re.findall(
            r'provider\s+"(aws|azurerm|google)"\s*\{[^}]*region\s*=\s*"([^"]+)"',
            content, re.S
        ):
            if provider == "aws":
                result["tech_stack"].append("AWS")
            elif provider == "azurerm":
                result["tech_stack"].append("Azure")
            elif provider == "google":
                result["tech_stack"].append("GCP")
            result["cloud_resources"].append({"type": f"{provider}_provider", "region": region})

        account_id = re.search(r'account_id\s*=\s*"(\d+)"', content)
        if account_id:
            result["cloud_resources"].append({"type": "aws_account", "id": account_id.group(1)})

        # Backend → environment
        backend = re.search(r'backend\s+"s3"\s*\{[^}]*key\s*=\s*"([^"]+)"', content, re.S)
        if backend:
            env = self._env_from_path(backend.group(1))
            if env and env not in result["environments"]:
                result["environments"].append(env)

        # All resource blocks
        for rtype, rname, rbody in re.findall(
            r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
            content, re.S
        ):
            self._classify_resource(rtype, rname, rbody, f.name, result)

        # Module calls → sub-environments
        for mname in re.findall(r'module\s+"([^"]+)"\s*\{', content):
            env = self._env_from_path(mname)
            if env and env not in result["environments"]:
                result["environments"].append(env)

    def _classify_resource(self, rtype: str, rname: str, body: str, source: str, result: dict) -> None:
        region = re.search(r'region\s*=\s*"([^"]+)"', body)
        region_val = region.group(1) if region else ""

        if rtype == "aws_eks_cluster":
            result["k8s_clusters"].append({"name": rname, "region": region_val, "source": source})
            result["tech_stack"].append("Kubernetes")

        elif rtype in ("aws_rds_instance", "aws_rds_cluster", "aws_db_instance"):
            engine = re.search(r'engine\s*=\s*"([^"]+)"', body)
            instance = re.search(r'instance_class\s*=\s*"([^"]+)"', body)
            result["databases"].append({
                "name": rname,
                "engine": engine.group(1) if engine else "rds",
                "instanceClass": instance.group(1) if instance else "",
                "region": region_val,
                "source": source,
            })
            if engine:
                result["tech_stack"].append(engine.group(1).title())

        elif rtype in ("aws_vpc", "aws_subnet", "aws_security_group",
                       "azurerm_virtual_network", "google_compute_network"):
            cidr = re.search(r'cidr_block\s*=\s*"([^"]+)"', body)
            result["networks"].append({
                "type": rtype, "name": rname,
                "cidr": cidr.group(1) if cidr else "",
                "region": region_val,
            })

        elif rtype in ("aws_instance", "azurerm_virtual_machine", "google_compute_instance"):
            itype = re.search(r'instance_type\s*=\s*"([^"]+)"', body)
            result["servers"].append({
                "name": rname,
                "instanceType": itype.group(1) if itype else "",
                "region": region_val,
            })

        elif rtype == "aws_lambda_function":
            runtime = re.search(r'runtime\s*=\s*"([^"]+)"', body)
            result["cloud_resources"].append({
                "type": "Lambda", "name": rname,
                "runtime": runtime.group(1) if runtime else "",
                "region": region_val,
            })

        elif rtype in ("aws_s3_bucket", "google_storage_bucket", "azurerm_storage_account"):
            result["cloud_resources"].append({"type": "Storage", "name": rname, "region": region_val})

        elif rtype in ("aws_sqs_queue", "aws_sns_topic"):
            result["topics"].append(rname)

        elif rtype in ("aws_elasticache_cluster", "aws_elasticache_replication_group"):
            engine = re.search(r'engine\s*=\s*"([^"]+)"', body)
            result["cloud_resources"].append({
                "type": "Cache", "name": rname,
                "engine": engine.group(1) if engine else "redis",
                "region": region_val,
            })

        elif rtype.startswith("azurerm_"):
            result["tech_stack"].append("Azure")
            result["cloud_resources"].append({"type": rtype, "name": rname, "region": region_val})
        elif rtype.startswith("google_"):
            result["tech_stack"].append("GCP")
            result["cloud_resources"].append({"type": rtype, "name": rname, "region": region_val})
        else:
            result["cloud_resources"].append({"type": rtype, "name": rname, "region": region_val})

    def _parse_tfvars(self, f: Path, result: dict) -> None:
        env = self._env_from_path(f.stem)
        if env and env not in result["environments"]:
            result["environments"].append(env)
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            for region in re.findall(r'region\s*=\s*"([^"]+)"', content):
                if not any(cr.get("region") == region for cr in result["cloud_resources"]):
                    result["cloud_resources"].append({"type": "region", "name": region})
        except Exception:
            pass

    def _parse_cloudformation(self, f: Path, result: dict) -> None:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        if "AWSTemplateFormatVersion" not in content and "Transform: AWS::Serverless" not in content:
            return
        result["tech_stack"].append("CloudFormation")
        for rtype in re.findall(r"Type:\s*(AWS::[\w:]+)", content):
            rname = rtype.split("::")[-1]
            result["cloud_resources"].append({"type": rtype, "name": rname})
            if "RDS" in rtype:
                result["databases"].append({"name": rname, "engine": "rds", "source": f.name})
            elif "EKS" in rtype:
                result["k8s_clusters"].append({"name": rname})
            elif "Lambda" in rtype:
                result["cloud_resources"].append({"type": "Lambda", "name": rname})

    def _env_from_path(self, s: str) -> str | None:
        m = re.search(r"(dev|test|staging|stage|prod|uat|qa|local|sit)", s, re.I)
        return m.group(1).lower() if m else None
