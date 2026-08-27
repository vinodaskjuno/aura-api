"""Tests for PaymentService — InsCore Payment Platform."""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from ..models.payment import PaymentRequest, PaymentMethod, PaymentStatus
from ..services.payment_service import PaymentService


@pytest.fixture
def payment_service():
    with patch("boto3.resource"), patch("boto3.client"):
        svc = PaymentService()
        svc.table = MagicMock()
        svc.lambda_client = MagicMock()
        return svc


@pytest.fixture
def valid_payment_request():
    return PaymentRequest(
        policy_id="POL-2024-001",
        amount=Decimal("250.00"),
        method=PaymentMethod.CREDIT_CARD,
    )


@pytest.mark.asyncio
async def test_create_payment_success(payment_service, valid_payment_request):
    """Valid payment request should create a payment record."""
    payment_service.table.put_item = MagicMock()
    payment_service.lambda_client.invoke = MagicMock()

    result = await payment_service.create(valid_payment_request)

    assert result.payment_id is not None
    assert result.status == PaymentStatus.PENDING
    assert result.amount == Decimal("250.00")
    assert result.policy_id == "POL-2024-001"
    payment_service.table.put_item.assert_called_once()
    payment_service.lambda_client.invoke.assert_called_once()


@pytest.mark.asyncio
async def test_create_payment_invalid_amount():
    """Zero or negative amount should raise validation error."""
    with pytest.raises(Exception):
        PaymentRequest(
            policy_id="POL-2024-001",
            amount=Decimal("0.00"),
            method=PaymentMethod.CREDIT_CARD,
        )


@pytest.mark.asyncio
async def test_create_payment_exceeds_max():
    """Amount over $1M should be rejected."""
    with pytest.raises(Exception):
        PaymentRequest(
            policy_id="POL-2024-001",
            amount=Decimal("1500000.00"),
            method=PaymentMethod.WIRE,
        )


@pytest.mark.asyncio
async def test_get_payment_not_found(payment_service):
    """Non-existent payment ID should return None."""
    payment_service.table.get_item = MagicMock(return_value={"Item": None})
    result = await payment_service.get_by_id("NONEXISTENT-ID")
    assert result is None


@pytest.mark.asyncio
async def test_cancel_pending_payment(payment_service, valid_payment_request):
    """Cancel a pending payment should succeed."""
    payment_service.table.put_item = MagicMock()
    payment_service.lambda_client.invoke = MagicMock()
    payment_service.table.update_item = MagicMock()

    created = await payment_service.create(valid_payment_request)
    payment_service.table.get_item = MagicMock(return_value={"Item": {
        "paymentId": created.payment_id,
        "policyId": "POL-2024-001",
        "amount": "250.00",
        "status": "pending",
        "method": "credit_card",
        "createdAt": created.created_at.isoformat(),
    }})

    result = await payment_service.cancel(created.payment_id)
    assert result is not None
    assert result.status == PaymentStatus.CANCELLED


@pytest.mark.asyncio
async def test_refund_completed_payment(payment_service):
    """Refund should only work on completed payments."""
    from ..models.payment import RefundRequest
    payment_service.table.get_item = MagicMock(return_value={"Item": {
        "paymentId": "PAY-001",
        "policyId": "POL-2024-001",
        "amount": "500.00",
        "status": "completed",
        "method": "credit_card",
        "createdAt": "2024-01-15T10:00:00+00:00",
    }})
    payment_service.table.update_item = MagicMock()

    refund_req = RefundRequest(payment_id="PAY-001", reason="Customer request")
    result = await payment_service.refund("PAY-001", refund_req)
    assert result.status == PaymentStatus.REFUNDED


@pytest.mark.asyncio
async def test_boundary_minimum_amount():
    """Minimum valid amount $0.01 should pass validation."""
    req = PaymentRequest(
        policy_id="POL-TEST",
        amount=Decimal("0.01"),
        method=PaymentMethod.ACH,
    )
    assert req.amount == Decimal("0.01")


@pytest.mark.asyncio
async def test_boundary_maximum_amount():
    """Maximum valid amount $1,000,000.00 should pass."""
    req = PaymentRequest(
        policy_id="POL-TEST",
        amount=Decimal("1000000.00"),
        method=PaymentMethod.WIRE,
    )
    assert req.amount == Decimal("1000000.00")


@pytest.mark.asyncio
async def test_negative_invalid_currency():
    """Unsupported currency should be rejected."""
    with pytest.raises(Exception):
        PaymentRequest(
            policy_id="POL-TEST",
            amount=Decimal("100.00"),
            method=PaymentMethod.CREDIT_CARD,
            currency="JPY",
        )
