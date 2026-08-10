import bcrypt
if not hasattr(bcrypt, "__about__"):
    class About:
        __version__ = bcrypt.__version__
    bcrypt.__about__ = About()

from datetime import datetime, timedelta, timezone
from typing import Any
from jose import jwt
from src.core.config import settings

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False

def get_password_hash(password: str) -> str:
    # Use the installed bcrypt library directly. Passlib's legacy bcrypt
    # backend performs a long-password compatibility probe that newer bcrypt
    # releases reject, which can break every managed-user creation request.
    if len(password.encode("utf-8")) > 72:
        raise ValueError("password cannot be longer than 72 UTF-8 bytes")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

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
    return jwt.encode({"exp": expire, "sub": str(subject), "type": "refresh", "av": auth_version}, settings.SECRET_KEY, algorithm="HS256")
