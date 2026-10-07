"""OAuth connection flow: state generation/validation, code exchange, secure token storage."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import TokenCipher, TokenDecryptionError
from app.core.exceptions import AppError, ConflictError, NotFoundError, OAuthStateError
from app.core.logging import get_logger
from app.core.security import generate_code_verifier, generate_oauth_state, hash_secret
from app.integrations.social.base import ProviderError
from app.integrations.social.registry import get_provider
from app.integrations.social.youtube import pkce_challenge
from app.models.enums import AccountStatus, Platform
from app.models.oauth_state import OAuthState
from app.models.social_account import SocialAccount
from app.models.base import utcnow
from app.repositories.schedule_repository import OAuthStateRepository
from app.repositories.social_account_repository import SocialAccountRepository
from app.schemas.social_account import ConnectResponse
from app.services.errors import provider_error_to_app_error
from app.services.token_service import apply_tokens

logger = get_logger(__name__)


class OAuthService:
    def __init__(self, session: AsyncSession, cipher: TokenCipher, settings: Settings) -> None:
        self.session, self.cipher, self.settings = session, cipher, settings
        self.states = OAuthStateRepository(session)
        self.accounts = SocialAccountRepository(session)

    # ------------------------------------------------------------------ step 1: authorization URL
    async def start(self, platform: Platform) -> ConnectResponse:
        provider = get_provider(platform)
        if not provider.is_configured():
            raise ConflictError(f"{platform.value.capitalize()} OAuth credentials are not configured.",
                                code=f"{platform.value.upper()}_NOT_CONFIGURED")
        now = utcnow()
        await self.states.purge_expired(now)

        state = generate_oauth_state()
        verifier = generate_code_verifier() if provider.uses_pkce else None
        self.states.add(OAuthState(
            state_hash=hash_secret(state), platform=platform.value, code_verifier=verifier,
            expires_at=now + timedelta(seconds=self.settings.oauth_state_ttl_seconds)))
        await self.session.commit()

        url = provider.connect(state, self.settings.oauth_redirect_uri(platform.value),
                               pkce_challenge(verifier) if verifier else None)
        logger.info("oauth_started", extra={"platform": platform.value})
        return ConnectResponse(platform=platform.value, authorization_url=url,
                               expires_in=self.settings.oauth_state_ttl_seconds)

    # ------------------------------------------------------------------ step 2: callback
    async def complete(self, platform: Platform, *, code: str | None, state: str | None,
                       error: str | None = None) -> list[SocialAccount]:
        if not state:
            raise OAuthStateError("Missing OAuth state.")
        row = await self.states.consume(hash_secret(state))
        await self.session.commit()  # the state is burned whatever happens next (single use)
        if row is None:
            raise OAuthStateError("Invalid or already used OAuth state.")
        if row.expires_at < utcnow():
            raise OAuthStateError("OAuth state expired. Start the connection again.", code="OAUTH_STATE_EXPIRED")
        if row.platform != platform.value:
            raise OAuthStateError("OAuth state does not belong to this platform.")
        if error:
            # e.g. access_denied. The provider's free-text description is deliberately not forwarded.
            raise AppError("Authorization was denied or cancelled.", code="OAUTH_AUTHORIZATION_DENIED",
                           status_code=400)
        if not code:
            raise AppError("Missing authorization code.", code="OAUTH_CODE_MISSING", status_code=400)

        provider = get_provider(platform)
        try:
            tokens = await provider.exchange_code(code, self.settings.oauth_redirect_uri(platform.value),
                                                  row.code_verifier)
            infos = await provider.get_account_info(tokens)
        except ProviderError as exc:
            logger.warning("oauth_exchange_failed", extra={"platform": platform.value, "error_code": exc.code})
            raise provider_error_to_app_error(exc) from exc

        saved: list[SocialAccount] = []
        for info in infos:
            token_set = info.token_override or tokens
            account = await self.accounts.get_by_platform_account(platform.value, info.platform_account_id)
            if account is None:
                account = SocialAccount(platform=platform.value, platform_account_id=info.platform_account_id,
                                        scopes=[], account_metadata={})
                self.accounts.add(account)
            if not token_set.refresh_token and tokens.refresh_token and info.token_override is None:
                token_set.refresh_token = tokens.refresh_token
            account.username = info.username
            account.display_name = info.display_name
            account.account_metadata = info.metadata
            account.connected_at = utcnow()
            if not token_set.refresh_token and account.status == AccountStatus.DISCONNECTED.value:
                account.refresh_token_encrypted = None
            apply_tokens(account, token_set, self.cipher)
            saved.append(account)
        await self.session.commit()
        logger.info("oauth_connected", extra={"platform": platform.value, "accounts": len(saved)})
        return saved

    # ------------------------------------------------------------------ disconnect
    async def disconnect(self, account_id: uuid.UUID) -> SocialAccount:
        account = await self.accounts.get(account_id)
        if account is None:
            raise NotFoundError("Social account not found.", code="SOCIAL_ACCOUNT_NOT_FOUND")
        if account.status != AccountStatus.DISCONNECTED.value:
            access = refresh = None
            try:
                access = self.cipher.decrypt(account.access_token_encrypted) if account.access_token_encrypted else None
                refresh = self.cipher.decrypt(account.refresh_token_encrypted) if account.refresh_token_encrypted else None
            except TokenDecryptionError:
                pass
            try:
                await get_provider(account.platform).disconnect(access, refresh)
            except Exception as exc:  # revocation is best effort; local disconnect must always succeed
                logger.warning("remote_revoke_failed", extra={"platform": account.platform,
                                                              "error_type": type(exc).__name__})
            # Keep the row (publication history references it) but destroy all credentials.
            account.access_token_encrypted = None
            account.refresh_token_encrypted = None
            account.token_expires_at = None
            account.status = AccountStatus.DISCONNECTED.value
            await self.session.commit()
            logger.info("account_disconnected", extra={"platform": account.platform, "account_id": str(account.id)})
        return account
