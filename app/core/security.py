"""Password hashing and JWT issuing/verification.

Uses `bcrypt` and `PyJWT` directly rather than the older `passlib`/`python-jose`
wrappers: both of those are effectively unmaintained, and passlib is incompatible
with bcrypt 5.x. Fewer, maintained dependencies matter more than usual in a
healthcare-facing codebase.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings


def _prepare(password: str) -> bytes:
    """Normalize a password to a fixed 44-byte value safe for bcrypt.

    bcrypt rejects secrets longer than 72 bytes. SHA-256 pre-hashing (then base64)
    accepts passwords of any length without silent truncation, which would otherwise
    make two different long passwords collide.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain_password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(*, user_id: int, role: str) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user_id), "role": role, "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT. Raises ValueError on any invalid or expired token."""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise ValueError("Invalid or expired token") from exc
