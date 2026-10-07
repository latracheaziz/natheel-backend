"""Low-level security primitives: credential comparison, session cookies, OAuth state."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.config import Settings

_SESSION_SALT = "natheel-admin-session-v1"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def verify_admin_credentials(settings: Settings, username: str, password: str) -> bool:
    """Constant-time comparison. Both fields are always compared to avoid leaking which was wrong."""
    user_ok = hmac.compare_digest(_digest(username), _digest(settings.admin_username))
    pass_ok = hmac.compare_digest(_digest(password), _digest(settings.admin_password.get_secret_value()))
    return user_ok and pass_ok


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.admin_session_secret.get_secret_value(), salt=_SESSION_SALT)


def create_session_token(settings: Settings) -> str:
    return _serializer(settings).dumps({"sub": "admin", "nonce": secrets.token_hex(8), "iat": int(time.time())})


def verify_session_token(settings: Settings, token: str) -> bool:
    try:
        data = _serializer(settings).loads(token, max_age=settings.admin_session_ttl_seconds)
    except BadSignature:  # includes SignatureExpired
        return False
    return isinstance(data, dict) and data.get("sub") == "admin"


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def hash_secret(value: str) -> str:
    """States are stored hashed so a database leak cannot be replayed against the callback."""
    return hashlib.sha256(value.encode()).hexdigest()
