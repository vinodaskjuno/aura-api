"""One-time script: delete the admin user so seed_default_data() recreates it with the correct hash.

Usage (from repo root):
    python -m src.scripts.reset_admin
"""
import sys
from src.database.dynamo_client import scan_items, delete_item

users = scan_items("users")
admin_users = [u for u in users if u.get("username") == "admin"]

if not admin_users:
    print("No admin user found — nothing to delete. Restart the backend to seed fresh.")
    sys.exit(0)

for u in admin_users:
    delete_item("users", {"userId": u["userId"]})
    print(f"Deleted admin user: userId={u['userId']}")

print("Done. Restart the backend — it will re-seed admin / Admin@123 with bcrypt.")
