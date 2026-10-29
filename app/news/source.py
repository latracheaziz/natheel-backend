"""Ministry of Tourism news, read server-side from the CMS that powers mt.gov.sa's media centre.

mt.gov.sa is an Angular app whose pages are rendered by a flaky SSR layer: requests to the site
itself (including `mt.gov.sa/api/...`) often answer with a 500 "System Unavailable" HTML page. The
app does not host its own API — a request interceptor rewrites every relative `api/...` call onto
the `baseUrl` from its runtime config, which is the Strapi CMS at `cms.tourism.sa`. That host is
the one the public news page actually reads, it answers reliably, and it needs no credentials, so
this module calls it directly instead of going through mt.gov.sa.

Article links still point at `mt.gov.sa/ar/media-center/news/{slug}`, the page a reader opens.

Responses are kept in memory so repeat visits do not hit the source again. Nothing is copied to a
database; the CMS stays the single source of truth. Fields the source leaves empty stay `None` in
the output. Nothing is filled in with guessed text.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

SOURCE_NAME = "وزارة السياحة"
SOURCE_SITE = "https://mt.gov.sa"
CMS_SITE = "https://cms.tourism.sa"
NEWS_API = f"{CMS_SITE}/api/news"
ARTICLE_PAGE = f"{SOURCE_SITE}/ar/media-center/news"
USER_AGENT = "NatheelAdmin/1.0 (+https://natheelco.com)"
LOCALE = "ar"

# The feed is re-read from the CMS whenever a cached copy is older than this, so a newly
# published article shows up on its own within the window without anyone pressing anything.
LIST_TTL_SECONDS = 5 * 60
ARTICLE_TTL_SECONDS = 30 * 60
SUMMARY_CHARS = 220
REQUEST_ATTEMPTS = 2
CMS_MAX_LIMIT = 100  # the CMS silently clamps pagination[limit] to this
MAX_SOURCE_PAGES = 10  # a ceiling so a misbehaving source cannot loop forever
SUPPORTED_LOCALES = ("ar", "en")

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class NewsUnavailable(Exception):
    """The source could not be read and no cached copy exists."""


def _cached(key: str, ttl: int) -> dict[str, Any] | None:
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def _stale(key: str) -> dict[str, Any] | None:
    entry = _cache.get(key)
    return entry[1] if entry else None


def _store(key: str, value: dict[str, Any]) -> None:
    _cache[key] = (time.monotonic(), value)


async def _request_json(params: list[tuple[str, str]]) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Accept-Language": "ar", "User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers=headers, follow_redirects=True) as client:
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                response = await client.get(NEWS_API, params=params)
            except httpx.HTTPError as exc:
                logger.warning("news source request failed", extra={"error": type(exc).__name__, "attempt": attempt + 1})
            else:
                # The source answers an outage with an HTML "System Unavailable" page, not JSON.
                if response.status_code == 200 and "json" in response.headers.get("content-type", ""):
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        return payload
                logger.warning("news source returned unusable response",
                               extra={"status": response.status_code, "attempt": attempt + 1})
            if attempt + 1 < REQUEST_ATTEMPTS:
                await asyncio.sleep(1.0)
    raise NewsUnavailable


def _absolute_url(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    # Strapi serves uploads as absolute CDN URLs; a relative one belongs to the CMS host.
    return f"{CMS_SITE}/{path.lstrip('/')}"


def _media(value: Any) -> dict[str, Any] | None:
    """Strapi wraps media either flat or as {data: {attributes: ...}}; accept both."""
    if not isinstance(value, dict):
        return None
    if "data" in value:
        value = value.get("data") or {}
        if not isinstance(value, dict):
            return None
    value = value.get("attributes", value)
    url = _absolute_url(value.get("url"))
    if not url:
        return None
    # Originals run to 2500px; the card grid uses the smaller rendition when the CMS made one.
    formats = value.get("formats") if isinstance(value.get("formats"), dict) else {}
    thumbnail = None
    for name in ("small", "medium", "thumbnail"):
        candidate = formats.get(name)
        if isinstance(candidate, dict):
            thumbnail = _absolute_url(candidate.get("url"))
            if thumbnail:
                break
    return {
        "url": url,
        "thumbnail": thumbnail,
        "alt": value.get("alternativeText") or None,
        "name": value.get("name") or None,
    }


def _inline_text(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    parts = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("text"), str):
            parts.append(node["text"])
        else:
            parts.append(_inline_text(node.get("children")))
    return "".join(parts).strip()


def _blocks_to_content(blocks: Any) -> list[dict[str, Any]]:
    """Convert Strapi rich-text blocks into a small, renderer-friendly list. Text stays as published."""
    content: list[dict[str, Any]] = []
    if not isinstance(blocks, list):
        return content
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "image":
            image = _media(block.get("image"))
            if image:
                content.append({"type": "image", "url": image["url"], "alt": image["alt"]})
            continue
        if kind == "list":
            items = [_inline_text(item.get("children")) for item in block.get("children") or [] if isinstance(item, dict)]
            items = [item for item in items if item]
            if items:
                content.append({"type": "list", "ordered": block.get("format") == "ordered", "items": items})
            continue
        text = _inline_text(block.get("children"))
        if not text:
            continue
        if kind == "heading":
            content.append({"type": "heading", "level": block.get("level", 2), "text": text})
        elif kind == "quote":
            content.append({"type": "quote", "text": text})
        else:
            content.append({"type": "paragraph", "text": text})
    return content


def _clip(text: str) -> str:
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS].rstrip() + "…"


def _summary(short_description: Any, title: Any, content: list[dict[str, Any]]) -> str | None:
    """Prefer the CMS summary field, but fall back when it merely repeats the title."""
    if isinstance(short_description, str) and short_description.strip():
        candidate = short_description.strip()
        if not isinstance(title, str) or candidate != title.strip():
            return _clip(candidate)
    for block in content:
        if block["type"] in ("paragraph", "quote", "heading") and block.get("text"):
            return _clip(block["text"])
    if isinstance(short_description, str) and short_description.strip():
        return _clip(short_description.strip())
    return None


def _article_url(slug: Any, locale: str) -> str | None:
    """The public page a reader opens. The site prefixes its routes with the language."""
    if not isinstance(slug, str) or not slug.strip():
        return None
    return f"{SOURCE_SITE}/{locale}/media-center/news/{slug.strip()}"


def _normalize(item: dict[str, Any], locale: str = LOCALE) -> dict[str, Any]:
    data = item.get("attributes", item)
    slug = data.get("slug") if isinstance(data.get("slug"), str) else None
    content = _blocks_to_content(data.get("description"))
    file = _media(data.get("file"))
    item_locale = data.get("locale") if isinstance(data.get("locale"), str) else locale
    return {
        "id": item.get("documentId") or item.get("id"),
        "slug": slug,
        "locale": item_locale,
        "title": data.get("title") or None,
        "date": data.get("date") or data.get("publishedAt") or None,
        "summary": _summary(data.get("shortDescription"), data.get("title"), content),
        "content": content,
        "image": _media(data.get("image")),
        "file": {"url": file["url"], "name": file["name"]} if file else None,
        "url": _article_url(slug, item_locale),
        "source": {"name": SOURCE_NAME, "url": SOURCE_SITE},
    }


def as_list_item(article: dict[str, Any]) -> dict[str, Any]:
    """Card-sized view: the body and attachment only matter once an article is opened."""
    return {key: value for key, value in article.items() if key not in ("content", "file")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_every_page(locale: str) -> tuple[list[dict[str, Any]], int]:
    """Walk the CMS pages. It caps `pagination[limit]` at 100, so a full read needs several."""
    collected: list[dict[str, Any]] = []
    total: int | None = None
    start = 0
    for _ in range(MAX_SOURCE_PAGES):
        params = [
            ("locale", locale),
            ("sort[0]", "date:desc"),
            ("pagination[start]", str(start)),
            ("pagination[limit]", str(CMS_MAX_LIMIT)),
            ("populate[image]", "true"),
            ("withCount", "true"),
        ]
        payload = await _request_json(params)
        entries = [entry for entry in payload.get("data") or [] if isinstance(entry, dict)]
        collected.extend(_normalize(entry, locale) for entry in entries)
        pagination = (payload.get("meta") or {}).get("pagination") or {}
        if isinstance(pagination.get("total"), int):
            total = pagination["total"]
        if not entries or len(entries) < CMS_MAX_LIMIT:
            break
        start += len(entries)
        if total is not None and len(collected) >= total:
            break
    return collected, total if total is not None else len(collected)


async def fetch_all(locale: str = LOCALE, *, force: bool = False) -> dict[str, Any]:
    """Every published article for one locale, newest first.

    The whole set is read because the admin page reports counts (new / read / unread) and filters
    across all of it, which per-page reads could not answer. It is ~160 short records.

    `force` skips the cached copy, for an explicit "check the source now" refresh.
    """
    cache_key = f"all:{locale}"
    if not force and (fresh := _cached(cache_key, LIST_TTL_SECONDS)) is not None:
        return fresh

    try:
        articles, total = await _fetch_every_page(locale)
    except NewsUnavailable:
        if (stale := _stale(cache_key)) is not None:
            return {**stale, "stale": True}
        raise

    result = {"articles": articles, "total": total, "stale": False, "fetched_at": _now()}
    _store(cache_key, result)
    return result


async def fetch_all_linked(locale: str = LOCALE, *, force: bool = False) -> dict[str, Any]:
    """`fetch_all`, plus a fallback source link for articles with no slug in this language.

    A handful of Arabic articles are published without a slug, so the Ministry's Arabic route
    cannot address them — but the same article does have a slug in English. Those get a
    `fallback_url` (clearly marked with its own language) instead of no link at all.
    """
    # The Arabic feed reliably contains a few slug-less articles, so the other language is read
    # alongside it rather than after it: the CMS answers in seconds and this halves a cold load.
    # Both results are cached, so repeat calls within the TTL cost nothing.
    others = [other for other in SUPPORTED_LOCALES if other != locale]
    results = await asyncio.gather(
        fetch_all(locale, force=force),
        *(fetch_all(other, force=force) for other in others),
        return_exceptions=True,
    )

    feed = results[0]
    if isinstance(feed, BaseException):
        raise feed
    if all(article.get("url") for article in feed["articles"]):
        return feed

    twins: dict[Any, dict[str, Any]] = {}
    for alternate in results[1:]:
        if isinstance(alternate, BaseException):
            continue  # the fallback link is a nicety; never fail the feed over it
        for article in alternate["articles"]:
            if article.get("url"):
                twins.setdefault(article["id"], article)

    articles = []
    for article in feed["articles"]:
        twin = None if article.get("url") else twins.get(article["id"])
        if twin is None:
            articles.append(article)
        else:
            articles.append({**article, "fallback_url": twin["url"], "fallback_locale": twin["locale"]})
    return {**feed, "articles": articles}


async def get_article(key: str, locale: str = LOCALE) -> dict[str, Any] | None:
    """Look one article up by its `documentId` or its `slug`, with any published translations.

    Some published articles carry no slug at all, so `documentId` is the only key that always
    identifies one. Both are accepted in a single query so existing slug links keep working.

    The CMS keeps one `documentId` per article across languages, so asking it to populate
    `localizations` returns the other languages' titles and bodies in the same request. Only the
    languages the Ministry actually published come back — nothing is machine-translated here.
    """
    cache_key = f"article:{locale}:{key}"
    if (fresh := _cached(cache_key, ARTICLE_TTL_SECONDS)) is not None:
        return fresh

    params = [
        ("locale", locale),
        ("filters[$or][0][documentId][$eq]", key),
        ("filters[$or][1][slug][$eq]", key),
        ("populate[image]", "true"),
        ("populate[file]", "true"),
        ("populate[localizations]", "true"),
    ]
    try:
        payload = await _request_json(params)
    except NewsUnavailable:
        if (stale := _stale(cache_key)) is not None:
            return {**stale, "stale": True}
        raise

    entries = [entry for entry in payload.get("data") or [] if isinstance(entry, dict)]
    if not entries:
        return None

    raw = entries[0]
    article = _normalize(raw, locale)
    translations = []
    for other in raw.get("localizations") or []:
        if not isinstance(other, dict):
            continue
        translated = _normalize(other, other.get("locale") or "")
        # Localization entries carry no media of their own; the article's image is shared.
        translated["image"] = translated["image"] or article["image"]
        if translated["locale"] and translated["locale"] != article["locale"]:
            translations.append(translated)

    # When this language has no public page, offer the one a translation does have.
    fallback = next((t for t in translations if t.get("url")), None) if not article.get("url") else None

    result = {
        **article,
        "translations": translations,
        "available_locales": [article["locale"], *(t["locale"] for t in translations)],
        "fallback_url": fallback["url"] if fallback else None,
        "fallback_locale": fallback["locale"] if fallback else None,
        "stale": False,
        "fetched_at": _now(),
    }
    _store(cache_key, result)
    return result
