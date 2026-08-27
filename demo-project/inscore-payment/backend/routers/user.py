"""User management router — InsCore Payment Platform."""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from typing import List, Optional
from ..services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])
user_svc = UserService()


class UserProfile(BaseModel):
    user_id: str
    email: str
    full_name: str
    policy_ids: List[str] = []
    preferred_payment_method: Optional[str] = None
    notification_email: bool = True
    notification_sms: bool = False


class CreateUserRequest(BaseModel):
    email: str
    full_name: str
    password: str
    notification_email: bool = True


@router.get("/{user_id}", response_model=UserProfile)
async def get_user(user_id: str):
    user = await user_svc.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/", response_model=UserProfile, status_code=201)
async def create_user(req: CreateUserRequest):
    existing = await user_svc.find_by_email(req.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    return await user_svc.create(req)


@router.put("/{user_id}/preferences")
async def update_preferences(user_id: str, preferences: dict):
    return await user_svc.update_preferences(user_id, preferences)
