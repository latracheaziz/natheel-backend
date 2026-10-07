"""Storage abstraction. Business code only depends on `StorageBackend`.

To add S3 / Cloudflare R2 / MinIO / Azure Blob, implement this interface (e.g. `S3Storage`
using multipart uploads + ranged GETs) and select it in `get_storage()`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    async def save(self, key: str, source_path: Path, content_type: str) -> None:
        """Persist the local file `source_path` under `key` (moves/copies; caller may delete the source)."""

    @abstractmethod
    async def size(self, key: str) -> int: ...

    @abstractmethod
    async def read_range(self, key: str, start: int, length: int) -> bytes: ...

    @abstractmethod
    def iter_chunks(self, key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]: ...

    @abstractmethod
    async def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    def public_url(self, key: str) -> str:
        """URL at which social platforms can download the object (must be publicly reachable)."""
