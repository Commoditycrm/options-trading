import uuid

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole
    display_name: str | None = Field(default=None, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    role: UserRole
    display_name: str | None
    is_active: bool
    # True only for a trader the admin flagged as solo (no fan-out + solo toolset).
    solo_mode: bool = False

    model_config = {"from_attributes": True}


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class MessageOut(BaseModel):
    message: str
