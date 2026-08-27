"""
AURA Demo Setup Script
Run this once after AWS credentials are refreshed:
  cd c:/Git/IS/genai-aig-infra-structure-api-7419
  python demo-project/setup_demo.py

Creates:
  1. Super admin user (if not exists)
  2. 4 sample projects with pre-built knowledge graphs
  3. 1 local demo project (InsCore Payment Platform) pointing to demo-project/inscore-payment/
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
DEMO_PROJECT_PATH = BASE / "inscore-payment"


def setup():
    print("=" * 60)
    print("  AURA Platform — Demo Setup")
    print("=" * 60)

    # ── Step 1: Seed auth ─────────────────────────────────────────
    print("\n[1/3] Seeding auth (super_admin user + 5 roles)...")
    try:
        from src.services.auth_service import seed_default_data, list_users
        seed_default_data()
        users = list_users()
        admin = next((u for u in users if u.get("username") == "admin"), None)
        print(f"  ✓ Admin user: admin / Admin@123  (role: super_admin)")
    except Exception as e:
        print(f"  ✗ Auth seed failed: {e}")
        return

    # ── Step 2: Sample projects ───────────────────────────────────
    print("\n[2/3] Creating 4 sample projects...")
    try:
        from src.database.dynamo_client import scan_items, put_item
        from src.routers.projects import SAMPLE_PROJECTS
        import json

        existing = scan_items("projects", limit=200)
        existing_names = {p.get("name") for p in existing}

        for sample in SAMPLE_PROJECTS:
            if sample["name"] in existing_names:
                print(f"  →  Already exists: {sample['name']}")
                continue
            project_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            project = {
                "projectId": project_id,
                "userId": admin["userId"],
                "username": admin["username"],
                "name": sample["name"],
                "description": sample["description"],
                "environment": sample["environment"],
                "status": "analyzed",
                "createdAt": now,
                "updatedAt": now,
                "repoCount": len(sample["repos"]),
                "mcpEndpoints": [],
                "isSample": True,
                "knowledgeGraph": json.dumps({**sample["knowledge_graph"], "project_id": project_id}),
                "techStack": sample["knowledge_graph"]["code"]["tech_stack"],
                "services": sample["knowledge_graph"]["code"]["services"],
            }
            put_item("projects", project)
            for repo in sample["repos"]:
                put_item("connectors", {
                    "connectorId": str(uuid.uuid4()),
                    "projectId": project_id,
                    "connectorType": "git",
                    "sourceType": repo.get("sourceType", "git"),
                    "repoType": repo["repoType"],
                    "repoUrl": repo.get("repoUrl", ""),
                    "localPath": repo.get("localPath", ""),
                    "branch": repo.get("branch", "main"),
                    "hasToken": False,
                    "createdAt": now,
                })
            print(f"  ✓ Created: {sample['name']}")
    except Exception as e:
        print(f"  ✗ Sample projects failed: {e}")

    # ── Step 3: Local demo project ────────────────────────────────
    print("\n[3/3] Registering local demo project (InsCore Payment Platform)...")
    try:
        from src.database.dynamo_client import put_item, scan_items

        existing = scan_items("projects", limit=200)
        if any(p.get("isLocalDemo") for p in existing):
            print("  →  Local demo project already registered")
        else:
            project_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            put_item("projects", {
                "projectId": project_id,
                "userId": admin["userId"],
                "username": admin["username"],
                "name": "InsCore Payment Platform (LOCAL DEMO)",
                "description": "Live local project — Python FastAPI, React UI, Mule ESB, K8s infra. Fully analysable.",
                "environment": "demo",
                "status": "pending",
                "createdAt": now, "updatedAt": now,
                "repoCount": 4, "mcpEndpoints": [],
                "isSample": False, "isLocalDemo": True,
            })
            repos = [
                {"repoType": "backend", "sourceType": "local", "localPath": str(DEMO_PROJECT_PATH / "backend"), "repoUrl": ""},
                {"repoType": "ui",      "sourceType": "local", "localPath": str(DEMO_PROJECT_PATH / "ui"),      "repoUrl": ""},
                {"repoType": "mule",    "sourceType": "local", "localPath": str(DEMO_PROJECT_PATH / "mule"),    "repoUrl": ""},
                {"repoType": "infra",   "sourceType": "local", "localPath": str(DEMO_PROJECT_PATH / "infra"),   "repoUrl": ""},
            ]
            for r in repos:
                put_item("connectors", {
                    "connectorId": str(uuid.uuid4()), "projectId": project_id,
                    "connectorType": "git", **r, "branch": "main",
                    "hasToken": False, "createdAt": now,
                })
            print(f"  ✓ Local project created: {project_id}")
            print(f"     Path: {DEMO_PROJECT_PATH}")
    except Exception as e:
        print(f"  ✗ Local project failed: {e}")

    # ── Summary ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE")
    print("=" * 60)
    print("""
  Login:     http://localhost:5173
  Username:  admin
  Password:  Admin@123

  Demo Steps:
  1. Dev Workspace → open 'InsCore Payment Platform (LOCAL DEMO)'
  2. Click 'Re-Analyse' to run live code analysis
  3. View Ontology Graph → hover nodes → expand/collapse
  4. QA Workspace → select any project → Generate Tests
  5. AIOps → Trigger RCA
""")


if __name__ == "__main__":
    setup()
