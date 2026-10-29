"""Filtering, searching and counting over the Ministry of Tourism news feed.

The CMS has no notion of our admins, so "new", "read" and "unread" are worked out here: `new` from
the article's own publication date against a configurable window, `read` from this admin's rows in
`news_reads`. Counts are computed over the whole feed (not the current page) so the sidebar badge
and the page header agree with each other.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from app.news import reads, source

STATUSES = ("all", "new", "unread", "read")

# Arabic writes the same word several ways; fold the variants so a search for "السياحه" also
# finds "السياحة". Harakat and tatweel are decoration and never typed in a search box.
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي",
    "ة": "ه",
    "ؤ": "و",
})


def search_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _DIACRITICS.sub("", folded).translate(_FOLD)


def _article_haystack(article: dict[str, Any]) -> str:
    parts = [article.get("title") or "", article.get("summary") or ""]
    for block in article.get("content") or []:
        if block.get("text"):
            parts.append(block["text"])
        for item in block.get("items") or []:
            parts.append(item)
    return search_key(" ".join(parts))


def parse_date(value: Any) -> date | None:
    """Accept the CMS's `YYYY-MM-DD` as well as a full ISO timestamp."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_new(published: date | None, cutoff: date) -> bool:
    return published is not None and published >= cutoff


def _annotate(article: dict[str, Any], read_set: set[str], cutoff: date) -> dict[str, Any]:
    """Return a copy. The article dicts live in a shared cache and must not be mutated."""
    published = parse_date(article.get("date"))
    return {
        **article,
        "is_read": str(article.get("id")) in read_set,
        "is_new": _is_new(published, cutoff),
    }


def _matches(
    article: dict[str, Any],
    *,
    status: str,
    needle: str,
    date_from: date | None,
    date_to: date | None,
    haystack: str,
) -> bool:
    if status == "new" and not article["is_new"]:
        return False
    if status == "read" and not article["is_read"]:
        return False
    if status == "unread" and article["is_read"]:
        return False
    if date_from or date_to:
        published = parse_date(article.get("date"))
        if published is None:
            return False
        if date_from and published < date_from:
            return False
        if date_to and published > date_to:
            return False
    if needle and needle not in haystack:
        return False
    return True


async def _snapshot(admin_id: str, locale: str, new_days: int, force: bool = False):
    feed = await source.fetch_all_linked(locale, force=force)
    stored = await reads.read_ids(admin_id)
    cutoff = datetime.now().date() - timedelta(days=max(new_days, 0))
    annotated = [_annotate(article, stored or set(), cutoff) for article in feed.get("articles") or []]
    return feed, annotated, stored is not None


def _latest(annotated: list[dict[str, Any]]) -> dict[str, Any]:
    """A cheap fingerprint of the feed's head, so a client can spot newly published articles.

    The feed is sorted newest-first by the CMS, so its first entry plus the total is enough to
    notice that something was added without re-sending the whole list.
    """
    head = annotated[0] if annotated else None
    return {
        "latest_id": head["id"] if head else None,
        "latest_date": head.get("date") if head else None,
        "total": len(annotated),
    }


def _counts(annotated: list[dict[str, Any]]) -> dict[str, int]:
    read = sum(1 for a in annotated if a["is_read"])
    return {
        "total": len(annotated),
        "new": sum(1 for a in annotated if a["is_new"]),
        "unread": len(annotated) - read,
        "read": read,
        "new_unread": sum(1 for a in annotated if a["is_new"] and not a["is_read"]),
    }


async def browse(
    admin_id: str,
    *,
    status: str = "all",
    query: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 12,
    locale: str = source.LOCALE,
    new_days: int = 14,
    force: bool = False,
) -> dict[str, Any]:
    feed, annotated, read_state_available = await _snapshot(admin_id, locale, new_days, force)
    needle = search_key(query or "")

    selected = [
        article
        for article in annotated
        if _matches(
            article,
            status=status,
            needle=needle,
            date_from=date_from,
            date_to=date_to,
            haystack=_article_haystack(article),
        )
    ]

    start = (page - 1) * page_size
    window = selected[start : start + page_size]
    return {
        "items": [source.as_list_item(article) for article in window],
        "page": page,
        "page_size": page_size,
        "matching": len(selected),
        "has_more": start + len(window) < len(selected),
        "counts": _counts(annotated),
        "feed": _latest(annotated),
        "locale": locale,
        "new_days": new_days,
        "synced_at": feed.get("fetched_at"),
        "stale": bool(feed.get("stale")),
        "read_state_available": read_state_available,
        "source": {"name": source.SOURCE_NAME, "url": source.SOURCE_SITE},
    }


async def counters(
    admin_id: str, *, locale: str = source.LOCALE, new_days: int = 14, force: bool = False
) -> dict[str, Any]:
    """The badge numbers plus the feed fingerprint — what a periodic poll needs."""
    feed, annotated, read_state_available = await _snapshot(admin_id, locale, new_days, force)
    return {
        "counts": _counts(annotated),
        "feed": _latest(annotated),
        "synced_at": feed.get("fetched_at"),
        "stale": bool(feed.get("stale")),
        "new_days": new_days,
        "read_state_available": read_state_available,
    }
