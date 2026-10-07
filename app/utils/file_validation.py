"""Media validation by content inspection. Client-provided MIME types are never trusted."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.core.exceptions import UnprocessableError
from app.models.enums import MediaType

ALLOWED_IMAGE_EXTENSIONS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4": "video/mp4"}
_MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"MSNV"}
_PIL_FORMAT_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass
class ValidatedMedia:
    file_type: MediaType
    mime_type: str
    extension: str
    metadata: dict = field(default_factory=dict)


def _sniff(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[4:8] == b"ftyp":
        return "video/mp4"
    return None


def _parse_mp4(path: Path) -> dict:
    """Walks top-level MP4 boxes: requires a known ftyp brand, moov and mdat atoms."""
    size_total = path.stat().st_size
    seen: set[bytes] = set()
    meta: dict = {}
    with path.open("rb") as fh:
        offset = 0
        while offset + 8 <= size_total:
            fh.seek(offset)
            header = fh.read(16)
            if len(header) < 8:
                break
            box_size, box_type = struct.unpack(">I4s", header[:8])
            header_len = 8
            if box_size == 1:
                if len(header) < 16:
                    break
                box_size = struct.unpack(">Q", header[8:16])[0]
                header_len = 16
            elif box_size == 0:
                box_size = size_total - offset
            if box_size < header_len or offset + box_size > size_total:
                raise UnprocessableError("Video file is truncated or corrupt.", code="INVALID_VIDEO")
            seen.add(box_type)
            if box_type == b"ftyp":
                fh.seek(offset + header_len)
                brand = fh.read(4)
                if brand not in _MP4_BRANDS:
                    raise UnprocessableError("Unsupported MP4 brand; upload an H.264 MP4 file.",
                                             code="INVALID_VIDEO")
                meta["brand"] = brand.decode("ascii", "replace").strip()
            elif box_type == b"moov":
                fh.seek(offset + header_len)
                moov = fh.read(min(box_size - header_len, 1024 * 1024))
                idx = moov.find(b"mvhd")
                if idx != -1:
                    base = idx + 4
                    version = moov[base]
                    try:
                        if version == 1:
                            timescale, duration = struct.unpack(">IQ", moov[base + 20:base + 32])
                        else:
                            timescale, duration = struct.unpack(">II", moov[base + 12:base + 20])
                        if timescale:
                            meta["duration_seconds"] = round(duration / timescale, 2)
                    except struct.error:
                        pass
            offset += box_size
    if not {b"ftyp", b"moov", b"mdat"} <= seen:
        raise UnprocessableError("File is not a valid MP4 video.", code="INVALID_VIDEO")
    return meta


def validate_media_file(path: Path, filename: str, *, max_image_bytes: int, max_video_bytes: int,
                        min_dimension: int, max_dimension: int) -> ValidatedMedia:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_IMAGE_EXTENSIONS and extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise UnprocessableError("Unsupported file extension. Allowed: jpg, jpeg, png, webp, mp4.",
                                 code="UNSUPPORTED_FILE_TYPE")
    with path.open("rb") as fh:
        sniffed = _sniff(fh.read(16))
    if sniffed is None:
        raise UnprocessableError("File content is not a supported image or video.",
                                 code="UNSUPPORTED_FILE_TYPE")
    expected = ALLOWED_IMAGE_EXTENSIONS.get(extension) or ALLOWED_VIDEO_EXTENSIONS[extension]
    if sniffed != expected:
        raise UnprocessableError("File extension does not match file content.", code="FILE_TYPE_MISMATCH")

    size = path.stat().st_size
    if size == 0:
        raise UnprocessableError("File is empty.", code="INVALID_FILE")

    if sniffed.startswith("image/"):
        if size > max_image_bytes:
            raise UnprocessableError(f"Image exceeds the {max_image_bytes // (1024 * 1024)} MB limit.",
                                     code="FILE_TOO_LARGE", status_code=413)
        try:
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:  # verify() invalidates the handle; reopen for metadata
                width, height = img.size
                if _PIL_FORMAT_TO_MIME.get(img.format or "") != sniffed:
                    raise UnprocessableError("Image format does not match its content.", code="FILE_TYPE_MISMATCH")
        except UnprocessableError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise UnprocessableError("Image is corrupt or unreadable.", code="INVALID_IMAGE") from exc
        if min(width, height) < min_dimension or max(width, height) > max_dimension:
            raise UnprocessableError(
                f"Image dimensions must be between {min_dimension}px and {max_dimension}px.",
                code="INVALID_IMAGE_DIMENSIONS")
        return ValidatedMedia(MediaType.IMAGE, sniffed, "jpg" if sniffed == "image/jpeg" else extension,
                              {"width": width, "height": height})

    if size > max_video_bytes:
        raise UnprocessableError(f"Video exceeds the {max_video_bytes // (1024 * 1024)} MB limit.",
                                 code="FILE_TOO_LARGE", status_code=413)
    return ValidatedMedia(MediaType.VIDEO, sniffed, "mp4", _parse_mp4(path))
