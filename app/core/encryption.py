"""Symmetric encryption of OAuth tokens at rest (Fernet / AES-128-CBC + HMAC)."""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


class TokenDecryptionError(Exception):
    pass


class TokenCipher:
    """Encrypts with the first key, decrypts with any (supports key rotation)."""

    def __init__(self, keys: str) -> None:
        fernets = [Fernet(k.strip().encode()) for k in keys.split(",") if k.strip()]
        if not fernets:
            raise ValueError("ENCRYPTION_KEY is empty")
        self._fernet = MultiFernet(fernets)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise TokenDecryptionError("Stored token cannot be decrypted with the configured key.") from exc


@lru_cache
def get_cipher() -> TokenCipher:
    return TokenCipher(get_settings().encryption_key.get_secret_value())


def generate_key() -> str:
    return Fernet.generate_key().decode()
