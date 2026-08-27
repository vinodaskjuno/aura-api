"""Audit service — immutable audit trail for all payment events."""
import logging
import uuid
from datetime import datetime, timezone
import boto3
from ..models.payment import PaymentResponse, RefundRequest

logger = logging.getLogger(__name__)


class AuditService:
    """Writes payment events to DynamoDB audit table.

    All records are immutable — no updates or deletes allowed.
    Used for compliance, SOX, and regulatory reporting.
    """

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = self.dynamodb.Table("inscore-audit-trail")

    async def log_payment(self, payment: PaymentResponse) -> None:
        self._write({
            "eventType": "PAYMENT_CREATED",
            "paymentId": payment.payment_id,
            "policyId": payment.policy_id,
            "amount": str(payment.amount),
            "method": payment.method.value,
        })

    async def log_refund(self, payment: PaymentResponse, req: RefundRequest) -> None:
        self._write({
            "eventType": "PAYMENT_REFUNDED",
            "paymentId": payment.payment_id,
            "policyId": payment.policy_id,
            "refundAmount": str(req.amount or payment.amount),
            "reason": req.reason,
        })

    def _write(self, event: dict) -> None:
        event["auditId"] = str(uuid.uuid4())
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            self.table.put_item(Item=event)
        except Exception as exc:
            logger.error("Audit write failed: %s", exc)
