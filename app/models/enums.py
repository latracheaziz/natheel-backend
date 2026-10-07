"""Enumerations and state machines shared across the domain.

Statuses are stored as plain strings (not PostgreSQL enums) so adding a value never needs an
`ALTER TYPE` migration; validity is enforced here.
"""
from __future__ import annotations

from enum import StrEnum

from app.core.exceptions import InvalidStateTransition


class Platform(StrEnum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    LINKEDIN = "linkedin"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"


class AccountStatus(StrEnum):
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    DISCONNECTED = "DISCONNECTED"


class PostStatus(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    PUBLISHING = "PUBLISHING"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    SCHEDULED = "SCHEDULED"


class PublicationStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class ScheduleStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DISPATCHED = "DISPATCHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


P, Q = PostStatus, PublicationStatus

POST_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    P.DRAFT: frozenset({P.QUEUED, P.SCHEDULED}),
    P.SCHEDULED: frozenset({P.DRAFT, P.QUEUED, P.FAILED, P.SCHEDULED}),
    P.QUEUED: frozenset({P.PUBLISHING, P.PUBLISHED, P.PARTIALLY_PUBLISHED, P.FAILED}),
    P.PUBLISHING: frozenset({P.QUEUED, P.PUBLISHED, P.PARTIALLY_PUBLISHED, P.FAILED}),
    P.PARTIALLY_PUBLISHED: frozenset({P.QUEUED, P.PUBLISHING, P.PUBLISHED, P.FAILED}),
    P.PUBLISHED: frozenset({P.QUEUED, P.PUBLISHING, P.PARTIALLY_PUBLISHED}),
    P.FAILED: frozenset({P.DRAFT, P.QUEUED, P.PUBLISHING, P.SCHEDULED, P.PUBLISHED, P.PARTIALLY_PUBLISHED}),
}

PUBLICATION_TRANSITIONS: dict[PublicationStatus, frozenset[PublicationStatus]] = {
    Q.PENDING: frozenset({Q.QUEUED, Q.FAILED}),
    Q.QUEUED: frozenset({Q.PUBLISHING, Q.FAILED}),
    Q.PUBLISHING: frozenset({Q.PUBLISHED, Q.FAILED, Q.RETRYING}),
    Q.RETRYING: frozenset({Q.PUBLISHING, Q.FAILED}),
    Q.FAILED: frozenset({Q.QUEUED}),
    Q.PUBLISHED: frozenset(),
}

PUBLICATION_IN_FLIGHT = frozenset({Q.PENDING, Q.QUEUED, Q.PUBLISHING, Q.RETRYING})


def check_transition(current, target, table, entity: str) -> None:
    if current == target:
        return
    if target not in table[current]:
        raise InvalidStateTransition(f"{entity} cannot move from {current} to {target}.")


def check_post_transition(current: PostStatus, target: PostStatus) -> None:
    check_transition(current, target, POST_TRANSITIONS, "Post")


def check_publication_transition(current: PublicationStatus, target: PublicationStatus) -> None:
    check_transition(current, target, PUBLICATION_TRANSITIONS, "Publication")
