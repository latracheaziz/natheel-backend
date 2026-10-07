from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from app.storage.base import StorageBackend


class LocalStorage(StorageBackend):
    def __init__(self, root: str | Path, public_base_url: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:  # defence against path traversal via crafted keys
            raise ValueError("Invalid storage key")
        return path

    async def save(self, key: str, source_path: Path, content_type: str) -> None:
        dest = self._path(key)

        def _move() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, dest)

        await asyncio.to_thread(_move)

    async def size(self, key: str) -> int:
        return await asyncio.to_thread(lambda: self._path(key).stat().st_size)

    async def read_range(self, key: str, start: int, length: int) -> bytes:
        def _read() -> bytes:
            with self._path(key).open("rb") as fh:
                fh.seek(start)
                return fh.read(length)

        return await asyncio.to_thread(_read)

    async def read_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def iter_chunks(self, key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        offset = 0
        while True:
            chunk = await self.read_range(key, offset, chunk_size)
            if not chunk:
                return
            offset += len(chunk)
            yield chunk

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self._path(key).unlink(missing_ok=True))

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"
