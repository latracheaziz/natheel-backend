"""Which mailbox provider this deployment talks to.

Selected by `EMAIL_PROVIDER`. Gmail is the default because natheelco.com's MX records point at
Google Workspace (aspmx.l.google.com), and the project already holds a Google OAuth client.

To add Microsoft 365 or IMAP later: implement `MailProvider` in this package and add it to
`_PROVIDERS` below. Nothing outside this package needs to change.
"""
from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.emails.base import MailProvider
from app.emails.gmail import GmailProvider

_PROVIDERS: dict[str, type[MailProvider]] = {
    GmailProvider.name: GmailProvider,
}

SUPPORTED = tuple(_PROVIDERS)


DEFAULT = GmailProvider.name


@lru_cache
def get_provider(name: str | None = None) -> MailProvider:
    # An EMAIL_PROVIDER line left blank in .env means "the default", not "no provider".
    key = (name or get_settings().email_provider or DEFAULT).strip().lower() or DEFAULT
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise ValueError(
            f"Unsupported EMAIL_PROVIDER {key!r}. Supported: {', '.join(SUPPORTED)}."
        )
    return provider()
