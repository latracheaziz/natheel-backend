"""Access-token lifecycle: decrypt on demand and refresh transparently before expiry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import TokenCipher, TokenDecryptionError
from app.core.logging import get_logger
from app.integrations.social.base import ProviderError, SocialMediaProvider, TokenExpiredError, TokenSet
from app.models.enums import AccountStatus
from app.models.social_account import SocialAccount
from app.repositories.social_account_repository import SocialAccountRepository

logger = get_logger(__name__)


def token_needs_refresh(account: SocialAccount, leeway_seconds: int) -> bool:
    return (account.token_expires_at is not None and
            account.token_expires_at - timedelta(seconds=leeway_seconds) <= datetime.now(timezone.utc))


def token_is_usable(account: SocialAccount, leeway_seconds: int = 0) -> bool:
    """Cheap, network-free check used before queueing: connected, has a token, and either unexpired
    or refreshable."""
    if account.status != AccountStatus.CONNECTED.value or not account.access_token_encrypted:
        return False
    if token_needs_refresh(account, leeway_seconds) and not account.refresh_token_encrypted:
        return False
    return True


def apply_tokens(account: SocialAccount, tokens: TokenSet, cipher: TokenCipher) -> None:
    account.access_token_encrypted = cipher.encrypt(tokens.access_token)
    if tokens.refresh_token:
        account.refresh_token_encrypted = cipher.encrypt(tokens.refresh_token)
    account.token_expires_at = tokens.expires_at
    if tokens.scopes:
        account.scopes = tokens.scopes
    account.status = AccountStatus.CONNECTED.value


class TokenService:
    def __init__(self, session: AsyncSession, cipher: TokenCipher, settings: Settings) -> None:
        self.session, self.cipher, self.settings = session, cipher, settings
        self.accounts = SocialAccountRepository(session)

    async def get_access_token(self, account: SocialAccount, provider: SocialMediaProvider) -> str:
        """Returns a decrypted, fresh access token. Raises TokenExpiredError when reconnection is needed."""
        if account.status != AccountStatus.CONNECTED.value or not account.access_token_encrypted:
            raise TokenExpiredError(account.platform)
        if token_needs_refresh(account, self.settings.token_refresh_leeway_seconds):
            await self.refresh(account, provider)
        try:
            return self.cipher.decrypt(account.access_token_encrypted)  # type: ignore[arg-type]
        except TokenDecryptionError as exc:
            raise ProviderError(f"{account.platform.upper()}_TOKEN_UNREADABLE", str(exc)) from exc

    async def refresh(self, account: SocialAccount, provider: SocialMediaProvider) -> None:
        # Capture identifiers now: a rollback expires ORM attributes and async code cannot lazy-load.
        account_id, platform = account.id, account.platform
        # Re-read under a row lock so concurrent tasks don't both rotate a refresh token.
        locked = await self.accounts.get(account_id, for_update=True)
        if locked is None:
            raise TokenExpiredError(platform)
        account = locked
        if not token_needs_refresh(account, self.settings.token_refresh_leeway_seconds):
            await self.session.commit()  # another task refreshed it first
            return
        if not account.refresh_token_encrypted:
            await self._mark_expired(account)
            raise TokenExpiredError(platform)
        refresh_plain = self.cipher.decrypt(account.refresh_token_encrypted)
        try:
            tokens = await provider.refresh_token(refresh_plain)
        except TokenExpiredError:
            await self._mark_expired(account)
            raise
        except ProviderError as exc:
            if exc.retryable:
                await self.session.rollback()
                raise
            await self._mark_expired(account)
            raise TokenExpiredError(platform) from exc
        apply_tokens(account, tokens, self.cipher)
        await self.session.commit()
        logger.info("token_refreshed", extra={"platform": platform, "account_id": str(account_id)})

    async def _mark_expired(self, account: SocialAccount) -> None:
        account.status = AccountStatus.EXPIRED.value
        await self.session.commit()
        logger.warning("account_token_expired", extra={"platform": account.platform, "account_id": str(account.id)})
