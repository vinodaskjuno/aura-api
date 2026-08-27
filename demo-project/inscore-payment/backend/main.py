"""InsCore Payment Platform — FastAPI Backend
Production microservice handling payment processing, user management,
and audit trail for AIG InsCore insurance platform.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import payment, user, audit, health
from .services.notification_service import NotificationService
from .services.fraud_detection_service import FraudDetectionService

app = FastAPI(title="InsCore Payment API", version="2.4.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://inscore.aig.com", "https://portal.aig.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(payment.router)
app.include_router(user.router)
app.include_router(audit.router)
app.include_router(health.router)

notification_svc = NotificationService()
fraud_svc = FraudDetectionService()


@app.on_event("startup")
async def startup():
    await notification_svc.connect()
    await fraud_svc.load_model()


@app.on_event("shutdown")
async def shutdown():
    await notification_svc.disconnect()
