"""Payment router — InsCore Payment Platform."""
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from typing import List
from ..models.payment import PaymentRequest, PaymentResponse, PaymentSummary, RefundRequest
from ..services.payment_service import PaymentService
from ..services.audit_service import AuditService
from ..services.fraud_detection_service import FraudDetectionService
from ..services.notification_service import NotificationService

router = APIRouter(prefix="/payments", tags=["payments"])

payment_svc = PaymentService()
audit_svc = AuditService()
fraud_svc = FraudDetectionService()
notification_svc = NotificationService()


@router.post("/", response_model=PaymentResponse, status_code=201)
async def create_payment(req: PaymentRequest, background_tasks: BackgroundTasks):
    """Process a new insurance premium payment."""
    # Fraud check before processing
    fraud_score = await fraud_svc.score(req)
    if fraud_score > 0.85:
        raise HTTPException(status_code=422, detail="Payment flagged for manual review")

    payment = await payment_svc.create(req)
    background_tasks.add_task(audit_svc.log_payment, payment)
    background_tasks.add_task(notification_svc.send_confirmation, payment)
    return payment


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(payment_id: str):
    """Retrieve payment details by ID."""
    payment = await payment_svc.get_by_id(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@router.get("/policy/{policy_id}", response_model=List[PaymentResponse])
async def get_policy_payments(policy_id: str, limit: int = 50, offset: int = 0):
    """Get all payments for a given policy."""
    return await payment_svc.get_by_policy(policy_id, limit=limit, offset=offset)


@router.get("/policy/{policy_id}/summary", response_model=PaymentSummary)
async def get_policy_summary(policy_id: str):
    """Get payment summary and balance for a policy."""
    return await payment_svc.get_summary(policy_id)


@router.post("/{payment_id}/refund", response_model=PaymentResponse)
async def refund_payment(payment_id: str, req: RefundRequest, background_tasks: BackgroundTasks):
    """Issue a full or partial refund."""
    payment = await payment_svc.refund(payment_id, req)
    background_tasks.add_task(audit_svc.log_refund, payment, req)
    background_tasks.add_task(notification_svc.send_refund_confirmation, payment)
    return payment


@router.post("/{payment_id}/cancel")
async def cancel_payment(payment_id: str):
    """Cancel a pending payment."""
    payment = await payment_svc.cancel(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found or not cancellable")
    return {"message": "Payment cancelled", "payment_id": payment_id}
