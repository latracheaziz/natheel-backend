"""Gmail / Google Workspace mailbox via the Gmail API and OAuth 2.0.

Chosen because natheelco.com's MX records point at Google (aspmx.l.google.com), so the mailbox is
Google Workspace. The project already registers a Google OAuth client for YouTube, and the same
client can carry the Gmail scope — see .env.example for the setup steps.

Scope: `gmail.modify` only. That covers reading messages and toggling the UNREAD label, which is
everything this page does. No send scope is requested, because replies are handed to Gmail itself
rather than sent from here.

Filtering is delegated to Gmail's own search syntax, so every filter the admin page offers is
resolved by the mailbox instead of being post-processed over a partial download.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.emails.base import (
    MailAuthError, MailProvider, MailProviderError, MailQuery, TokenSet)
from app.emails.sanitize import html_to_text, sanitize_html

logger = get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAIL_UI = "https://mail.google.com/mail/"

# Headers are the only part of a message needed to render a row in the list.
LIST_HEADERS = ("From", "To", "Subject", "Date")
# Enough concurrency to keep a page snappy without tripping Gmail's per-user rate limit.
DETAIL_CONCURRENCY = 8


def _b64url(data: str | None) -> bytes:
    if not data:
        return b""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode())
    except (binascii.Error, ValueError):
        return b""


def _decode(part: dict[str, Any]) -> str | None:
    raw = _b64url((part.get("body") or {}).get("data"))
    if not raw:
        return None
    for encoding in ("utf-8", "cp1256", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in payload.get("headers") or []
        if isinstance(h, dict)
    }


def _is_email(value: str) -> bool:
    """A conservative check, because `getaddresses` happily returns a bare token as an address.

    Treating such a token as an address would produce a Reply link the provider rejects, so a
    value that is not plausibly an address is shown as a display name instead.
    """
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def _addresses(raw: str | None) -> list[dict[str, str | None]]:
    if not raw:
        return []
    people = []
    for name, email in getaddresses([raw]):
        name, email = name.strip(), email.strip()
        if not name and not email:
            continue
        if email and not _is_email(email):
            # Keep what the header said, but never present it as an addressable mailbox.
            name, email = name or email, ""
        people.append({"name": name or None, "email": email or None})
    return people


def _received_at(headers: dict[str, str], internal_date: str | None) -> str | None:
    """Prefer Gmail's own receive timestamp; fall back to the Date header."""
    if internal_date:
        try:
            moment = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
            return moment.isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    raw = headers.get("date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
    return None


def _walk(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [payload]
    queue = list(payload.get("parts") or [])
    while queue:
        part = queue.pop(0)
        if not isinstance(part, dict):
            continue
        parts.append(part)
        queue.extend(part.get("parts") or [])
    return parts


def _bodies_and_attachments(payload: dict[str, Any]) -> tuple[str | None, str | None, list[dict]]:
    html_parts: list[str] = []
    text_parts: list[str] = []
    attachments: list[dict] = []

    for part in _walk(payload):
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        is_attachment = bool(filename) or bool(body.get("attachmentId"))

        if is_attachment:
            attachments.append({
                "filename": filename or None,
                "mime_type": mime or None,
                "size": body.get("size") if isinstance(body.get("size"), int) else None,
                # Deliberately not downloadable through this API: reading attachment bytes is a
                # wider permission surface than this page needs. Open the mail in Gmail for that.
                "inline": not filename and bool(part.get("headers")),
            })
            continue

        if mime == "text/html":
            decoded = _decode(part)
            if decoded:
                html_parts.append(decoded)
        elif mime == "text/plain":
            decoded = _decode(part)
            if decoded:
                text_parts.append(decoded)

    html = sanitize_html("\n".join(html_parts)) if html_parts else None
    text = "\n".join(text_parts).strip() if text_parts else None
    return html, (text or html_to_text(html)), attachments


class GmailProvider(MailProvider):
    name = "gmail"
    label = "Gmail / Google Workspace"
    required_settings = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    scopes = ("https://www.googleapis.com/auth/gmail.modify",)
    supports_read_sync = True
    supports_attachment_filter = True

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------ configuration
    def is_configured(self) -> bool:
        return bool(
            self.settings.google_client_id
            and self.settings.google_client_secret.get_secret_value()
        )

    # ------------------------------------------------------------------ OAuth
    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "access_type": "offline",   # we need a refresh token to keep reading later
            "prompt": "consent",        # force a refresh token even on a repeat authorisation
            "include_granted_scopes": "true",
        }
        if self.settings.email_login_hint:
            params["login_hint"] = self.settings.email_login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    def _tokens(self, data: dict[str, Any]) -> TokenSet:
        if not data.get("access_token"):
            raise MailProviderError("Google did not return an access token.")
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in") if isinstance(data.get("expires_in"), int) else None,
            scopes=(data.get("scope") or "").split() or list(self.scopes),
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret.get_secret_value(),
        }
        async with self._client() as client:
            return self._tokens(await self._json(client, "POST", TOKEN_URL, data=form))

    async def refresh_access_token(self, refresh_token: str) -> TokenSet:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret.get_secret_value(),
        }
        async with self._client() as client:
            return self._tokens(await self._json(client, "POST", TOKEN_URL, data=form))

    async def revoke(self, token: str) -> None:
        try:
            async with self._client() as client:
                await client.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            logger.info("gmail token revocation could not be delivered")

    # ------------------------------------------------------------------ HTTP plumbing
    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(self.settings.provider_http_timeout_seconds)
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def _json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        token: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise MailProviderError(f"Gmail is unreachable: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            # 403 also covers a revoked grant and a scope the admin declined.
            detail = self._error_detail(response)
            raise MailAuthError(f"Gmail refused the stored credentials: {detail}",
                                status=response.status_code)
        if response.status_code == 404:
            return {}
        if response.status_code == 429:
            raise MailProviderError("Gmail rate limit reached. Try again in a moment.", status=429)
        if response.status_code >= 400:
            raise MailProviderError(f"Gmail request failed: {self._error_detail(response)}",
                                    status=response.status_code)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise MailProviderError("Gmail returned a malformed response.") from exc
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Upstream message only — never the request body, which may hold a token."""
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or response.status_code)
        return str(error or response.status_code)

    # ------------------------------------------------------------------ mailbox reads
    async def profile(self, access_token: str) -> dict[str, Any]:
        async with self._client() as client:
            me = await self._json(client, "GET", f"{API}/profile", token=access_token)
            inbox = await self._json(client, "GET", f"{API}/labels/INBOX", token=access_token)
        return {
            "email": me.get("emailAddress"),
            "total": inbox.get("messagesTotal") if isinstance(inbox.get("messagesTotal"), int) else None,
            "unread": inbox.get("messagesUnread") if isinstance(inbox.get("messagesUnread"), int) else None,
        }

    @staticmethod
    def build_query(query: MailQuery) -> str:
        """Translate the normalised filters into Gmail search syntax."""
        terms: list[str] = []
        if query.status == "unread":
            terms.append("is:unread")
        elif query.status == "read":
            terms.append("is:read")
        if query.sender:
            terms.append(f'from:"{query.sender.strip()}"')
        if query.subject:
            terms.append(f'subject:"{query.subject.strip()}"')
        if query.has_attachment:
            terms.append("has:attachment")
        if query.date_from:
            terms.append(f"after:{query.date_from:%Y/%m/%d}")
        if query.date_to:
            # Gmail's `before:` is exclusive, so shift a day to make the admin's end date inclusive.
            terms.append(f"before:{query.date_to + timedelta(days=1):%Y/%m/%d}")
        if query.keywords:
            terms.append(query.keywords.strip())
        return " ".join(terms)

    async def list_messages(
        self, access_token: str, query: MailQuery, *, page_size: int, page_token: str | None,
        mailbox_email: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": page_size, "labelIds": "INBOX"}
        search = self.build_query(query)
        if search:
            params["q"] = search
        if page_token:
            params["pageToken"] = page_token

        async with self._client() as client:
            listing = await self._json(client, "GET", f"{API}/messages", token=access_token,
                                       params=params)
            ids = [m.get("id") for m in listing.get("messages") or [] if isinstance(m, dict)]
            ids = [i for i in ids if i]

            # Gmail's list call returns ids only; headers need one metadata read per message.
            gate = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def head(message_id: str) -> dict[str, Any] | None:
                async with gate:
                    metadata_params = [("format", "metadata")]
                    metadata_params += [("metadataHeaders", h) for h in LIST_HEADERS]
                    raw = await self._json(client, "GET", f"{API}/messages/{message_id}",
                                           token=access_token, params=metadata_params)
                    return raw or None

            fetched = await asyncio.gather(*(head(i) for i in ids), return_exceptions=True)

        items = []
        for raw in fetched:
            if isinstance(raw, BaseException) or not raw:
                continue  # one unreadable message must not blank the whole page
            items.append(self._summary(raw, mailbox_email))

        estimate = listing.get("resultSizeEstimate")
        return {
            "items": items,
            "next_page_token": listing.get("nextPageToken"),
            # Gmail documents this as an estimate, so it is labelled as such in the UI.
            "estimated_total": estimate if isinstance(estimate, int) else None,
            "query": search or None,
        }

    def _summary(self, raw: dict[str, Any], mailbox_email: str | None = None) -> dict[str, Any]:
        payload = raw.get("payload") or {}
        headers = _headers(payload)
        labels = [l for l in raw.get("labelIds") or [] if isinstance(l, str)]
        senders = _addresses(headers.get("from"))
        message_id = raw.get("id")
        return {
            "id": message_id,
            "thread_id": raw.get("threadId"),
            "subject": headers.get("subject") or None,
            "from": senders[0] if senders else {"name": None, "email": None},
            "to": _addresses(headers.get("to")),
            "date": _received_at(headers, raw.get("internalDate")),
            "snippet": raw.get("snippet") or None,
            "is_read": "UNREAD" not in labels,
            "is_starred": "STARRED" in labels,
            "has_attachments": self._flags_attachment(payload),
            "attachment_count": None,  # metadata reads cannot see parts; filled in on open
            "provider_url": self.message_url(message_id, mailbox_email),
            "reply_url": self.reply_url(headers.get("from"), headers.get("subject"), mailbox_email),
        }

    @staticmethod
    def _flags_attachment(payload: dict[str, Any]) -> bool:
        """A metadata read exposes only the top-level mime type, which is enough for a hint."""
        mime = (payload.get("mimeType") or "").lower()
        return mime.startswith("multipart/mixed") or mime.startswith("multipart/related")

    async def get_message(
        self, access_token: str, message_id: str, mailbox_email: str | None = None
    ) -> dict[str, Any] | None:
        async with self._client() as client:
            raw = await self._json(client, "GET", f"{API}/messages/{message_id}",
                                   token=access_token, params={"format": "full"})
        if not raw or not raw.get("id"):
            return None

        payload = raw.get("payload") or {}
        headers = _headers(payload)
        html, text, attachments = _bodies_and_attachments(payload)
        summary = self._summary(raw, mailbox_email)
        real_attachments = [a for a in attachments if a.get("filename")]
        return {
            **summary,
            "cc": _addresses(headers.get("cc")),
            "reply_to": _addresses(headers.get("reply-to")),
            "body_html": html,
            "body_text": text,
            "attachments": real_attachments,
            "has_attachments": bool(real_attachments),
            "attachment_count": len(real_attachments),
        }

    async def set_read(self, access_token: str, message_id: str, read: bool) -> None:
        change = {"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]}
        async with self._client() as client:
            await self._json(client, "POST", f"{API}/messages/{message_id}/modify",
                             token=access_token, json=change)

    # ------------------------------------------------------------------ provider links
    def message_url(self, message_id: str | None, mailbox_email: str | None = None) -> str | None:
        """Deep link to the real message in Gmail's own UI.

        `authuser` pins the link to this mailbox, so it still opens the right message for someone
        signed into several Google accounts.
        """
        if not message_id:
            return None
        if mailbox_email:
            return f"{MAIL_UI}u/?authuser={quote(mailbox_email)}#all/{message_id}"
        return f"{MAIL_UI}#all/{message_id}"

    def reply_url(
        self, from_header: str | None, subject: str | None, mailbox_email: str | None = None
    ) -> str | None:
        """Gmail compose, pre-addressed. Returns None rather than a link that would not work."""
        senders = _addresses(from_header)
        address = senders[0]["email"] if senders else None
        if not address:
            return None
        subject = (subject or "").strip()
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
        params = {"view": "cm", "fs": "1", "to": address}
        if reply_subject and reply_subject != "Re:":
            params["su"] = reply_subject
        if mailbox_email:
            params["authuser"] = mailbox_email
        return f"{MAIL_UI}?{urlencode(params)}"
