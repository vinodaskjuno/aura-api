"""Terraform connector — parses .tf files and Terraform state to discover resources."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
from ..base import AbstractConnector, SyncResult


class TerraformConnector(AbstractConnector):
    """
    Config keys:
      path: str               root directory containing .tf files or terraform.tfstate
      state_file: str | None  explicit path to terraform.tfstate
    """

    def test_connection(self) -> tuple[bool, str]:
        root = self.config.get("path", ".")
        tf_files = list(Path(root).rglob("*.tf"))
        state_file = Path(self.config.get("state_file", os.path.join(root, "terraform.tfstate")))
        if tf_files or state_file.exists():
            return True, f"Found {len(tf_files)} .tf files"
        return False, f"No .tf files or state found in {root}"

    def sync(self) -> SyncResult:
        result = SyncResult()
        resources = self._parse_state() or self._parse_hcl()
        result.entities_added = len(resources)
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return (self._parse_state() or self._parse_hcl())[:5]

    def _parse_state(self) -> list[dict[str, Any]]:
        state_path = self.config.get("state_file",
                                     os.path.join(self.config.get("path", "."), "terraform.tfstate"))
        try:
            with open(state_path) as f:
                state = json.load(f)
            resources = []
            for resource in state.get("resources", []):
                for instance in resource.get("instances", []):
                    resources.append({
                        "type": resource["type"],
                        "name": resource["name"],
                        "provider": resource.get("provider", ""),
                        "attributes": instance.get("attributes", {}),
                    })
            return resources
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _parse_hcl(self) -> list[dict[str, Any]]:
        root = self.config.get("path", ".")
        resources: list[dict] = []
        for tf_file in Path(root).rglob("*.tf"):
            try:
                with open(tf_file, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                # Naive resource block detection (not a full HCL parser)
                import re
                for m in re.finditer(r'resource\s+"(\w+)"\s+"(\w+)"', content):
                    resources.append({"type": m.group(1), "name": m.group(2),
                                      "file": str(tf_file)})
            except OSError:
                pass
        return resources
