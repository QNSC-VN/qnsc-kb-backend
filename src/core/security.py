import bcrypt
if not hasattr(bcrypt, "__about__"):
    class About:
        __version__ = bcrypt.__version__
    bcrypt.__about__ = About()

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from jose import jwt
from passlib.context import CryptContext
from src.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(subject: str | Any, expires_delta: timedelta | None = None, auth_version: int = 0) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"exp": expire, "sub": str(subject), "type": "access", "av": auth_version}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")
    return encoded_jwt

def create_refresh_token(subject: str | Any, auth_version: int = 0) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    # `jti` makes every refresh token unique. Without it the claims are {exp, sub, type,
    # av} and `exp` has one-second resolution, so two tokens minted for the same user in
    # the same second are BYTE-IDENTICAL — and refresh_sessions.token_hash is UNIQUE, so
    # storing the replacement raised UniqueViolationError and the refresh returned 500.
    #
    # That is not a rare race. Rotation revokes the old row and inserts a new one in the
    # same request, so any refresh landing in the same second as the mint it replaces
    # fails — signing in and refreshing immediately, or two tabs refreshing at once.
    # Revoked rows keep their hash, so the collision outlives the session it replaced.
    return jwt.encode(
        {"exp": expire, "sub": str(subject), "type": "refresh", "av": auth_version, "jti": uuid.uuid4().hex},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
