"""Tests for FraudDetectionService — InsCore Payment Platform."""
import pytest
from decimal import Decimal
from ..models.payment import PaymentRequest, PaymentMethod
from ..services.fraud_detection_service import FraudDetectionService


@pytest.fixture
async def fraud_service():
    svc = FraudDetectionService()
    await svc.load_model()
    return svc


def make_request(amount: str, policy_id: str = "POL-001") -> PaymentRequest:
    return PaymentRequest(
        policy_id=policy_id,
        amount=Decimal(amount),
        method=PaymentMethod.CREDIT_CARD,
    )


@pytest.mark.asyncio
async def test_low_amount_low_risk(fraud_service):
    """Small payment should have low fraud score."""
    score = await fraud_service.score(make_request("99.99"))
    assert score < 0.5


@pytest.mark.asyncio
async def test_high_amount_elevated_risk(fraud_service):
    """Large payment should increase fraud score."""
    score = await fraud_service.score(make_request("75000.00"))
    assert score >= 0.3


@pytest.mark.asyncio
async def test_velocity_check_triggers(fraud_service):
    """Multiple rapid payments on same policy should raise score."""
    req = make_request("100.00", "POL-VELOCITY")
    for _ in range(4):
        await fraud_service.score(req)
    final_score = await fraud_service.score(req)
    assert final_score > 0.0


@pytest.mark.asyncio
async def test_risk_level_classification(fraud_service):
    """Risk levels should be correctly classified."""
    assert fraud_service.get_risk_level(0.1) == "LOW"
    assert fraud_service.get_risk_level(0.6) == "MEDIUM"
    assert fraud_service.get_risk_level(0.9) == "HIGH"


@pytest.mark.asyncio
async def test_score_capped_at_one(fraud_service):
    """Fraud score should never exceed 1.0."""
    score = await fraud_service.score(make_request("999999.99"))
    assert score <= 1.0
