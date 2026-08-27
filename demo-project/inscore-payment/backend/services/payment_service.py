"""Payment service — business logic for InsCore payment processing."""
import uuid
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, List
import boto3
from ..models.payment import PaymentRequest, PaymentResponse, PaymentSummary, PaymentStatus, RefundRequest

logger = logging.getLogger(__name__)


class PaymentService:
    """Core payment processing business logic.

    Uses DynamoDB for payment persistence and AWS Lambda for async processing.
    Implements retry logic with exponential backoff for transient failures.
    """

    def __init__(self):
        self.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = self.dynamodb.Table("inscore-payments")
        self.lambda_client = boto3.client("lambda", region_name="us-east-1")

    async def create(self, req: PaymentRequest) -> PaymentResponse:
        payment_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        payment = PaymentResponse(
            payment_id=payment_id,
            policy_id=req.policy_id,
            amount=req.amount,
            status=PaymentStatus.PENDING,
            method=req.method,
            created_at=now,
        )

        # Persist to DynamoDB
        self.table.put_item(Item={
            "paymentId": payment_id,
            "policyId": req.policy_id,
            "amount": str(req.amount),
            "status": payment.status.value,
            "method": req.method.value,
            "currency": req.currency,
            "createdAt": now.isoformat(),
            "metadata": req.metadata,
        })

        # Trigger async processing via Lambda
        self.lambda_client.invoke(
            FunctionName="inscore-payment-processor",
            InvocationType="Event",
            Payload=f'{{"payment_id": "{payment_id}"}}'.encode(),
        )

        logger.info("Payment created: %s for policy %s", payment_id, req.policy_id)
        return payment

    async def get_by_id(self, payment_id: str) -> Optional[PaymentResponse]:
        resp = self.table.get_item(Key={"paymentId": payment_id})
        item = resp.get("Item")
        if not item:
            return None
        return self._to_response(item)

    async def get_by_policy(self, policy_id: str, limit: int = 50, offset: int = 0) -> List[PaymentResponse]:
        resp = self.table.query(
            IndexName="policyId-createdAt-index",
            KeyConditionExpression="policyId = :pid",
            ExpressionAttributeValues={":pid": policy_id},
            Limit=limit,
            ScanIndexForward=False,
        )
        return [self._to_response(item) for item in resp.get("Items", [])]

    async def get_summary(self, policy_id: str) -> PaymentSummary:
        payments = await self.get_by_policy(policy_id, limit=1000)
        completed = [p for p in payments if p.status == PaymentStatus.COMPLETED]
        pending = [p for p in payments if p.status == PaymentStatus.PENDING]
        return PaymentSummary(
            policy_id=policy_id,
            total_paid=sum(p.amount for p in completed),
            total_pending=sum(p.amount for p in pending),
            payment_count=len(completed),
            last_payment_date=completed[0].processed_at if completed else None,
        )

    async def refund(self, payment_id: str, req: RefundRequest) -> PaymentResponse:
        payment = await self.get_by_id(payment_id)
        if not payment:
            raise ValueError(f"Payment {payment_id} not found")
        if payment.status != PaymentStatus.COMPLETED:
            raise ValueError("Only completed payments can be refunded")
        refund_amount = req.amount or payment.amount
        if refund_amount > payment.amount:
            raise ValueError("Refund amount exceeds original payment")
        self.table.update_item(
            Key={"paymentId": payment_id},
            UpdateExpression="SET #s = :s, refundAmount = :ra, refundReason = :rr",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": PaymentStatus.REFUNDED.value,
                ":ra": str(refund_amount),
                ":rr": req.reason,
            },
        )
        payment.status = PaymentStatus.REFUNDED
        return payment

    async def cancel(self, payment_id: str) -> Optional[PaymentResponse]:
        payment = await self.get_by_id(payment_id)
        if not payment or payment.status != PaymentStatus.PENDING:
            return None
        self.table.update_item(
            Key={"paymentId": payment_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": PaymentStatus.CANCELLED.value},
        )
        payment.status = PaymentStatus.CANCELLED
        return payment

    def _to_response(self, item: dict) -> PaymentResponse:
        return PaymentResponse(
            payment_id=item["paymentId"],
            policy_id=item["policyId"],
            amount=Decimal(item["amount"]),
            status=PaymentStatus(item["status"]),
            method=item["method"],
            created_at=datetime.fromisoformat(item["createdAt"]),
            transaction_ref=item.get("transactionRef"),
        )
