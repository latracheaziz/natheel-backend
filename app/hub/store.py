"""Local record of hub posts. This is the copy kept on our platform."""
from __future__ import annotations

import json
from pathlib import Path

_POSTS_FILE = Path(__file__).resolve().parents[2] / "data" / "hub-posts.json"


def _read() -> list[dict]:
    if not _POSTS_FILE.exists():
        return []
    try:
        data = json.loads(_POSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write(posts: list[dict]) -> None:
    _POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _POSTS_FILE.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")


def list_posts() -> list[dict]:
    return _read()


def save_post(post: dict) -> dict:
    posts = [item for item in _read() if item.get("id") != post.get("id")]
    posts.insert(0, post)
    _write(posts)
    return post


def delete_post(post_id: str) -> bool:
    posts = _read()
    kept = [item for item in posts if item.get("id") != post_id]
    if len(kept) == len(posts):
        return False
    _write(kept)
    return True
