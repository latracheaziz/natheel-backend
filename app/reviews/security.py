"""Password hashing and short-lived admin session tokens."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

_HASH_ROUNDS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _HASH_ROUNDS).hex()
    return f"pbkdf2_sha256${_HASH_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
    return hmac.compare_digest(check, digest)


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().admin_session_secret.get_secret_value()
    return URLSafeTimedSerializer(secret, salt="natheel-review-admin")


def issue_admin_token(admin_id: str) -> str:
    return _serializer().dumps({"sub": admin_id})


def read_admin_token(token: str) -> str | None:
    try:
        data = _serializer().loads(token, max_age=12 * 60 * 60)
    except (BadSignature, SignatureExpired):
        return None
    subject = data.get("sub") if isinstance(data, dict) else None
    return str(subject) if subject else None
