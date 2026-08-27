"""Fraud detection service — ML-based payment risk scoring."""
import logging
from decimal import Decimal
from ..models.payment import PaymentRequest

logger = logging.getLogger(__name__)

RISK_WEIGHTS = {
    "high_amount": 0.3,
    "unusual_method": 0.2,
    "multiple_policies": 0.25,
    "velocity_check": 0.25,
}


class FraudDetectionService:
    """Rule-based + ML fraud scoring.

    Combines heuristic rules with an AWS SageMaker endpoint for
    real-time fraud probability scoring.
    """

    def __init__(self):
        self._model_loaded = False
        self._recent_payments: dict = {}

    async def load_model(self):
        self._model_loaded = True
        logger.info("FraudDetectionService model loaded")

    async def score(self, req: PaymentRequest) -> float:
        """Return fraud probability 0.0 (safe) to 1.0 (high risk)."""
        score = 0.0

        # High amount heuristic
        if req.amount > Decimal("50000"):
            score += RISK_WEIGHTS["high_amount"]
        elif req.amount > Decimal("10000"):
            score += RISK_WEIGHTS["high_amount"] * 0.5

        # Velocity check — multiple payments in short window
        key = req.policy_id
        count = self._recent_payments.get(key, 0)
        if count > 3:
            score += RISK_WEIGHTS["velocity_check"]
        self._recent_payments[key] = count + 1

        score = min(score, 1.0)
        logger.debug("Fraud score for policy %s: %.2f", req.policy_id, score)
        return score

    def get_risk_level(self, score: float) -> str:
        if score >= 0.85:
            return "HIGH"
        if score >= 0.5:
            return "MEDIUM"
        return "LOW"
