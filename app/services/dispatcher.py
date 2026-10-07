"""Hand-off of publications to the job queue. Abstracted so tests can use an in-memory recorder."""
from __future__ import annotations

import asyncio
import uuid
from typing import Protocol


class PublicationDispatcher(Protocol):
    async def dispatch(self, publication_id: uuid.UUID, platform: str) -> None: ...


class CeleryDispatcher:
    """Sends one independent Celery task per publication (e.g. publish_to_facebook)."""

    async def dispatch(self, publication_id: uuid.UUID, platform: str) -> None:
        from app.workers.publishing_tasks import PLATFORM_TASKS  # lazy: keeps API import light

        task = PLATFORM_TASKS[platform]
        # .delay() performs blocking network I/O to the broker.
        await asyncio.to_thread(task.delay, str(publication_id))


_dispatcher: PublicationDispatcher = CeleryDispatcher()


def get_dispatcher() -> PublicationDispatcher:
    return _dispatcher


def set_dispatcher(dispatcher: PublicationDispatcher | None) -> None:
    global _dispatcher
    _dispatcher = dispatcher or CeleryDispatcher()
