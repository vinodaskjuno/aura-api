"""Notification service — email/SMS alerts for payment events."""
import logging
import boto3
from ..models.payment import PaymentResponse

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends payment confirmations via SES (email) and SNS (SMS)."""

    def __init__(self):
        self.ses = boto3.client("ses", region_name="us-east-1")
        self.sns = boto3.client("sns", region_name="us-east-1")
        self.from_email = "payments@inscore.aig.com"
        self._connected = False

    async def connect(self):
        self._connected = True
        logger.info("NotificationService connected")

    async def disconnect(self):
        self._connected = False

    async def send_confirmation(self, payment: PaymentResponse) -> None:
        """Send payment confirmation email via SES."""
        try:
            self.ses.send_email(
                Source=self.from_email,
                Destination={"ToAddresses": [self._get_policy_email(payment.policy_id)]},
                Message={
                    "Subject": {"Data": f"Payment Confirmation — ${payment.amount}"},
                    "Body": {"Text": {"Data": self._confirmation_body(payment)}},
                },
            )
            logger.info("Confirmation sent for payment %s", payment.payment_id)
        except Exception as exc:
            logger.error("Failed to send confirmation: %s", exc)

    async def send_refund_confirmation(self, payment: PaymentResponse) -> None:
        """Send refund notification email."""
        try:
            self.ses.send_email(
                Source=self.from_email,
                Destination={"ToAddresses": [self._get_policy_email(payment.policy_id)]},
                Message={
                    "Subject": {"Data": f"Refund Processed — ${payment.amount}"},
                    "Body": {"Text": {"Data": f"Your refund of ${payment.amount} has been processed."}},
                },
            )
        except Exception as exc:
            logger.error("Failed to send refund confirmation: %s", exc)

    async def send_sms_alert(self, phone: str, message: str) -> None:
        """Send SMS via SNS."""
        try:
            self.sns.publish(PhoneNumber=phone, Message=message)
        except Exception as exc:
            logger.error("SMS send failed: %s", exc)

    def _get_policy_email(self, policy_id: str) -> str:
        return f"policyholder+{policy_id}@example.com"

    def _confirmation_body(self, payment: PaymentResponse) -> str:
        return (
            f"Payment ID: {payment.payment_id}\n"
            f"Policy: {payment.policy_id}\n"
            f"Amount: ${payment.amount} {payment.method.value}\n"
            f"Status: {payment.status.value}\n"
        )
