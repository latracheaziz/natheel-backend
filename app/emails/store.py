"""Persistence for the connected mailbox: encrypted tokens and single-use OAuth states.

Tokens are encrypted with the project's existing Fernet cipher (`app/core/encryption.py`) before
they touch the database and are never returned by any API response, logged, or included in a
repr. OAuth states are stored hashed, so a database leak cannot be replayed against the callback.

Tables are created on demand with `CREATE TABLE IF NOT EXISTS`, the approach the reviews and news
modules already use in this project.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.core.encryption import TokenDecryptionError, get_cipher
from app.core.logging import get_logger
from app.core.security import generate_oauth_state, hash_secret

logger = get_logger(__name__)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS email_accounts (
        provider TEXT PRIMARY KEY,
        email TEXT,
        access_token_encrypted TEXT NOT NULL,
        refresh_token_encrypted TEXT,
        token_expires_at TIMESTAMPTZ,
        scopes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'connected',
        connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_oauth_states (
        state_hash TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
)

_schema_ready = False


@dataclass(slots=True)
class MailAccount:
    provider: str
    email: str | None
    access_token: str
    refresh_token: str | None
    token_expires_at: datetime | None
    scopes: list[str]
    status: str
    connected_at: datetime | None

    def access_token_expired(self, leeway_seconds: int = 120) -> bool:
        if self.token_expires_at is None:
            return False  # unknown expiry: let the provider's 401 drive the refresh
        return self.token_expires_at <= datetime.now(timezone.utc) + timedelta(seconds=leeway_seconds)

    def __repr__(self) -> str:  # never include token material
        return f"<MailAccount {self.provider} {self.email or 'unknown'} {self.status}>"


async def _ensure_schema(session) -> None:
    global _schema_ready
    if _schema_ready:
        return
    for statement in SCHEMA_STATEMENTS:
        await session.execute(text(statement))
    await session.commit()
    _schema_ready = True


# --------------------------------------------------------------------------- account


async def get_account(provider: str) -> MailAccount | None:
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        row = (
            await session.execute(
                text(
                    """
                    SELECT provider, email, access_token_encrypted, refresh_token_encrypted,
                           token_expires_at, scopes, status, connected_at
                    FROM email_accounts WHERE provider = :provider
                    """
                ),
                {"provider": provider},
            )
        ).first()

    if row is None:
        return None

    cipher = get_cipher()
    try:
        access = cipher.decrypt(row.access_token_encrypted)
        refresh = cipher.decrypt(row.refresh_token_encrypted) if row.refresh_token_encrypted else None
    except TokenDecryptionError:
        # Usually ENCRYPTION_KEY changed. Reconnecting is the only remedy; say so rather than crash.
        logger.warning("stored mailbox tokens cannot be decrypted", extra={"provider": provider})
        return MailAccount(provider=row.provider, email=row.email, access_token="",
                           refresh_token=None, token_expires_at=None, scopes=[],
                           status="reauthorize", connected_at=row.connected_at)

    return MailAccount(
        provider=row.provider,
        email=row.email,
        access_token=access,
        refresh_token=refresh,
        token_expires_at=row.token_expires_at,
        scopes=[s for s in (row.scopes or "").split() if s],
        status=row.status,
        connected_at=row.connected_at,
    )


async def save_account(
    provider: str,
    *,
    email: str | None,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: list[str] | None,
) -> None:
    """Store a freshly authorised mailbox, replacing any previous grant for this provider.

    A re-authorisation that returns no refresh token keeps the stored one, because Google only
    issues it on the first consent for a given client.
    """
    cipher = get_cipher()
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text(
                """
                INSERT INTO email_accounts (provider, email, access_token_encrypted,
                                            refresh_token_encrypted, token_expires_at, scopes,
                                            status, updated_at)
                VALUES (:provider, :email, :access, :refresh, :expires, :scopes, 'connected', NOW())
                ON CONFLICT (provider) DO UPDATE SET
                    email = EXCLUDED.email,
                    access_token_encrypted = EXCLUDED.access_token_encrypted,
                    refresh_token_encrypted = COALESCE(EXCLUDED.refresh_token_encrypted,
                                                       email_accounts.refresh_token_encrypted),
                    token_expires_at = EXCLUDED.token_expires_at,
                    scopes = EXCLUDED.scopes,
                    status = 'connected',
                    updated_at = NOW()
                """
            ),
            {
                "provider": provider,
                "email": email,
                "access": cipher.encrypt(access_token),
                "refresh": cipher.encrypt(refresh_token) if refresh_token else None,
                "expires": expires_at,
                "scopes": " ".join(scopes or []),
            },
        )
        await session.commit()
    logger.info("mailbox connected", extra={"provider": provider})


async def update_access_token(provider: str, access_token: str, expires_at: datetime | None) -> None:
    cipher = get_cipher()
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text(
                """
                UPDATE email_accounts
                SET access_token_encrypted = :access, token_expires_at = :expires,
                    status = 'connected', updated_at = NOW()
                WHERE provider = :provider
                """
            ),
            {"provider": provider, "access": cipher.encrypt(access_token), "expires": expires_at},
        )
        await session.commit()


async def mark_status(provider: str, status: str) -> None:
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text("UPDATE email_accounts SET status = :status, updated_at = NOW() WHERE provider = :provider"),
            {"provider": provider, "status": status},
        )
        await session.commit()


async def delete_account(provider: str) -> None:
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text("DELETE FROM email_accounts WHERE provider = :provider"), {"provider": provider}
        )
        await session.commit()
    logger.info("mailbox disconnected", extra={"provider": provider})


# --------------------------------------------------------------------------- oauth state


async def create_state(provider: str, ttl_seconds: int) -> str:
    """Issue a single-use CSRF state for the connect flow and return the plaintext value."""
    state = generate_oauth_state()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        await session.execute(
            text("DELETE FROM email_oauth_states WHERE expires_at < NOW()")
        )
        await session.execute(
            text(
                """
                INSERT INTO email_oauth_states (state_hash, provider, expires_at)
                VALUES (:hash, :provider, :expires)
                ON CONFLICT (state_hash) DO NOTHING
                """
            ),
            {"hash": hash_secret(state), "provider": provider, "expires": expires_at},
        )
        await session.commit()
    return state


async def consume_state(state: str) -> str | None:
    """Burn the state and return the provider it was issued for, or None if it is not valid."""
    async with get_sessionmaker()() as session:
        await _ensure_schema(session)
        row = (
            await session.execute(
                text(
                    """
                    DELETE FROM email_oauth_states
                    WHERE state_hash = :hash AND expires_at >= NOW()
                    RETURNING provider
                    """
                ),
                {"hash": hash_secret(state)},
            )
        ).first()
        await session.commit()
    return row.provider if row else None
