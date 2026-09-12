import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    generate_reset_token,
    hash_reset_token,
)
from app.core.config import APP_BASE_URL, RESET_TOKEN_EXPIRE_MINUTES
from app.core.database import get_db
from app.models.db_models import PasswordResetToken, User
from app.services.email_service import send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8

# Generic response for /forgot-password — identical whether or not the email
# is registered, so the endpoint can't be used to enumerate accounts.
FORGOT_PASSWORD_RESPONSE = {
    "message": "If an account exists for that email, a password reset link has been sent."
}


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str = "member"


@router.post("/signup", response_model=TokenResponse, status_code=201)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    email = body.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = User(user_id=str(uuid.uuid4()), email=email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # signup race on the unique email index
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    return TokenResponse(
        access_token=create_access_token(user.user_id, user.role),
        email=user.email,
        role=user.role,
    )


@router.post("/login", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """OAuth2 password flow — `username` carries the email."""
    email = form.username.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    # Same error for both cases — never reveal whether the email exists.
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    return TokenResponse(
        access_token=create_access_token(user.user_id, user.role),
        email=user.email,
        role=user.role,
    )


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {
        "user_id": user.user_id,
        "email": user.email,
        "created_at": user.created_at,
        "role": user.role,
        "status": user.status,
    }


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password")
def forgot_password(
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Always returns the same generic message — never reveals whether the
    email is registered (same principle as /login's single error message)."""
    email = body.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()

    # Active check mirrors get_current_user: a suspended/banned account
    # shouldn't be able to regain access via password reset either.
    if user and user.status == "active":
        # Invalidate any outstanding tokens from earlier requests so only the
        # newest link is live — closes the window where a stale, still-valid
        # link from an old request could reset the password after this one.
        now = datetime.now(timezone.utc)
        db.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == user.user_id,
            PasswordResetToken.used_at.is_(None),
        ).update({"used_at": now})

        raw_token = generate_reset_token()
        reset_row = PasswordResetToken(
            token_id=str(uuid.uuid4()),
            user_id=user.user_id,
            token_hash=hash_reset_token(raw_token),
            expires_at=now + timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES),
        )
        db.add(reset_row)
        db.commit()

        reset_link = f"{APP_BASE_URL}/reset-password?token={raw_token}"
        # Sent after the response so response timing can't be used to infer
        # whether the account exists (the DB write above still costs a bit of
        # time either way, but the outbound HTTP call to Resend is the
        # expensive part).
        background_tasks.add_task(
            send_password_reset_email, user.email, reset_link, RESET_TOKEN_EXPIRE_MINUTES
        )

    return FORGOT_PASSWORD_RESPONSE


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    token_hash = hash_reset_token(body.token)
    reset_row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()

    if reset_row is None or reset_row.used_at is not None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")

    now = datetime.now(timezone.utc)
    expires_at = reset_row.expires_at if reset_row.expires_at.tzinfo else reset_row.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")

    user = db.query(User).filter(User.user_id == reset_row.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")

    user.password_hash = hash_password(body.new_password)
    # Single-use: mark this token spent, and every other outstanding token for
    # this user too — a password reset invalidates any other in-flight link.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.user_id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": now})
    db.commit()

    return {"message": "Your password has been reset. You can now log in with your new password."}
