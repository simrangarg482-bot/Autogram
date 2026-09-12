"""
OAuth2 password-flow authentication with JWT bearer tokens.

- Passwords: salted PBKDF2-HMAC-SHA256 (200k iterations) — stdlib only,
  constant-time verification. Never stored or logged in plaintext.
- Tokens: HS256 JWTs carrying the user_id (sub) with expiry.
- get_current_user: FastAPI dependency that turns a Bearer token into a User
  row or raises 401 — every user-scoped endpoint depends on it. Also enforces
  account status: a suspended/banned user is rejected here with 403.
- require_role / get_current_moderator / get_current_admin: role gates layered
  on top of get_current_user (Phase 0 RBAC — see db_models.VALID_USER_ROLES).
  The token carries `role` only as a frontend hint; this module always trusts
  the freshly-read User row, never the claim.
"""

import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import JWT_SECRET, JWT_EXPIRE_MINUTES
from app.core.database import get_db, read_with_reconnect
from app.models.db_models import User

_PBKDF2_ITERATIONS = 200_000

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ---------- passwords ----------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    recomputed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(recomputed.hex(), digest_hex)


# ---------- password reset tokens ----------
# The raw token only ever exists in memory and in the emailed link — the DB
# stores just its SHA-256 hash (a fast hash is fine here, unlike passwords:
# the token is 256 bits of CSPRNG entropy, not something a human chose, so it
# isn't subject to dictionary/brute-force attack the way a password is).

def generate_reset_token() -> str:
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- tokens ----------

def create_access_token(user_id: str, role: str = "member") -> str:
    payload = {
        "sub": user_id,
        "role": role,  # frontend hint only — get_current_user re-reads the DB row
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=401,
        detail="Invalid or expired token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
    except jwt.PyJWTError:
        raise credentials_error

    # Every user-scoped request starts here, so a connection Neon killed while
    # idle in the pool surfaces on this query first. It's a pure read, so it's
    # safe to replay once on a fresh connection rather than 500-ing the request.
    user = read_with_reconnect(
        db, lambda s: s.query(User).filter(User.user_id == user_id).first()
    )
    if not user:
        raise credentials_error

    # Account status gate (Phase 0). A suspended/banned user holds a valid
    # token but must not be able to act — 403 with a reason they can read,
    # distinct from the 401 an expired/invalid token gets.
    if user.status == "banned":
        raise HTTPException(status_code=403, detail="This account has been banned.")
    if user.status == "suspended":
        raise HTTPException(status_code=403, detail="This account is suspended.")
    return user


def require_role(*roles: str, hide: bool = False):
    """Build a dependency that admits only the given roles.

    `hide=True` returns 404 instead of 403 for the wrong role — used by the
    admin console so its endpoints don't even confirm they exist to a
    non-admin. Community moderator controls use the default 403 (the member
    already knows the feature exists; they just can't use it).
    """
    allowed = set(roles)

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            if hide:
                raise HTTPException(status_code=404, detail="Not found.")
            raise HTTPException(status_code=403, detail="Insufficient permissions.")
        return user

    return dependency


get_current_moderator = require_role("moderator", "admin")
get_current_admin = require_role("admin", hide=True)
