"""User service — policyholder account management."""
import uuid
import logging
import boto3
from typing import Optional

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = self.dynamodb.Table("inscore-users")

    async def get_by_id(self, user_id: str) -> Optional[dict]:
        resp = self.table.get_item(Key={"userId": user_id})
        return resp.get("Item")

    async def find_by_email(self, email: str) -> Optional[dict]:
        resp = self.table.query(
            IndexName="email-index",
            KeyConditionExpression="email = :e",
            ExpressionAttributeValues={":e": email},
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    async def create(self, req) -> dict:
        user_id = str(uuid.uuid4())
        user = {
            "userId": user_id,
            "email": req.email,
            "full_name": req.full_name,
            "policy_ids": [],
            "notification_email": req.notification_email,
            "notification_sms": False,
        }
        self.table.put_item(Item=user)
        return user

    async def update_preferences(self, user_id: str, prefs: dict) -> dict:
        self.table.update_item(
            Key={"userId": user_id},
            UpdateExpression="SET notification_email = :ne, notification_sms = :ns",
            ExpressionAttributeValues={
                ":ne": prefs.get("notification_email", True),
                ":ns": prefs.get("notification_sms", False),
            },
        )
        return {"updated": True}
