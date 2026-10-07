from __future__ import annotations

from app.core.exceptions import UnprocessableError
from app.models.enums import Platform


def parse_platform(value: str) -> Platform:
    try:
        return Platform(value.lower())
    except ValueError as exc:
        raise UnprocessableError(f"Unknown platform '{value}'.", code="UNKNOWN_PLATFORM") from exc


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
