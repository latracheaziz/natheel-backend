"""Provider-agnostic mailbox contract.

Everything above this layer (the service, the routes, the admin page) speaks only in the shapes
defined here, so a second provider — Microsoft Graph, IMAP — is added by implementing
`MailProvider` and registering it, without touching the API or the frontend.

Message shapes are plain dicts, matching the sibling `app/news` module, and are what the API
returns verbatim:

summary: id, thread_id, subject, from, to, date, snippet, is_read, has_attachments,
         attachment_count, provider_url, reply_url
message: the summary plus body_html (sanitised), body_text, cc, attachments[]

A field the provider does not supply is `None`; nothing is invented.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any


class MailProviderError(Exception):
    """A provider call failed. `status` is the upstream HTTP status when there was one."""

    code = "MAIL_PROVIDER_ERROR"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        if code:
            self.code = code


class MailNotConfigured(MailProviderError):
    """The provider's credentials are missing from the environment."""

    code = "MAIL_NOT_CONFIGURED"


class MailNotConnected(MailProviderError):
    """No mailbox has been authorised yet."""

    code = "MAIL_NOT_CONNECTED"


class MailAuthError(MailProviderError):
    """Tokens are expired or revoked: the mailbox has to be reconnected."""

    code = "MAIL_AUTH_REQUIRED"


@dataclass(slots=True)
class MailQuery:
    """Normalised filter set. Each provider translates this into its own search syntax."""

    status: str = "all"  # all | read | unread
    sender: str | None = None
    subject: str | None = None
    keywords: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    has_attachment: bool = False

    def is_empty(self) -> bool:
        return (
            self.status == "all"
            and not self.sender
            and not self.subject
            and not self.keywords
            and not self.date_from
            and not self.date_to
            and not self.has_attachment
        )


@dataclass(slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    scopes: list[str] | None = None


class MailProvider(ABC):
    """One mailbox provider. Implementations hold no per-request state."""

    name: str = ""
    label: str = ""
    # Settings an operator must fill in before the connect flow can start.
    required_settings: tuple[str, ...] = ()
    # Only what reading mail and toggling read/unread needs — never sending.
    scopes: tuple[str, ...] = ()
    # True when the provider can push a read/unread change back to the mailbox.
    supports_read_sync: bool = False
    supports_attachment_filter: bool = False

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether the environment holds the credentials this provider needs."""

    @abstractmethod
    def authorization_url(self, state: str, redirect_uri: str) -> str:
        """Where to send the admin's browser to grant access."""

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        """Trade the callback's code for tokens."""

    @abstractmethod
    async def refresh_access_token(self, refresh_token: str) -> TokenSet:
        """Mint a new access token from a stored refresh token."""

    @abstractmethod
    async def profile(self, access_token: str) -> dict[str, Any]:
        """Identify the mailbox: {"email": ..., "total": int | None, "unread": int | None}."""

    @abstractmethod
    async def list_messages(
        self, access_token: str, query: MailQuery, *, page_size: int, page_token: str | None,
        mailbox_email: str | None = None,
    ) -> dict[str, Any]:
        """{"items": [summary, ...], "next_page_token": str | None, "estimated_total": int | None}."""

    @abstractmethod
    async def get_message(
        self, access_token: str, message_id: str, mailbox_email: str | None = None
    ) -> dict[str, Any] | None:
        """One full message, or None when the id does not exist in this mailbox."""

    @abstractmethod
    async def set_read(self, access_token: str, message_id: str, read: bool) -> None:
        """Push a read/unread change back to the mailbox."""

    async def revoke(self, token: str) -> None:  # pragma: no cover - optional capability
        """Best-effort token revocation when the mailbox is disconnected."""
        return None
