"""Payment domain models — InsCore Payment Platform."""
from pydantic import BaseModel, field_validator
from decimal import Decimal
from datetime import datetime
from enum import Enum
from typing import Optional


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class PaymentMethod(str, Enum):
    CREDIT_CARD = "credit_card"
    ACH = "ach"
    WIRE = "wire"
    CHECK = "check"


class PaymentRequest(BaseModel):
    policy_id: str
    amount: Decimal
    method: PaymentMethod
    currency: str = "USD"
    description: str = ""
    metadata: dict = {}

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError("Amount must be positive")
        if v > Decimal("1000000.00"):
            raise ValueError("Amount exceeds maximum allowed")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v):
        allowed = {"USD", "CAD", "EUR", "GBP"}
        if v not in allowed:
            raise ValueError(f"Currency must be one of {allowed}")
        return v


class PaymentResponse(BaseModel):
    payment_id: str
    policy_id: str
    amount: Decimal
    status: PaymentStatus
    method: PaymentMethod
    created_at: datetime
    processed_at: Optional[datetime] = None
    transaction_ref: Optional[str] = None
    error_message: Optional[str] = None


class PaymentSummary(BaseModel):
    policy_id: str
    total_paid: Decimal
    total_pending: Decimal
    payment_count: int
    last_payment_date: Optional[datetime] = None
    next_due_date: Optional[datetime] = None


class RefundRequest(BaseModel):
    payment_id: str
    reason: str
    amount: Optional[Decimal] = None  # None = full refund
