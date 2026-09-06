"""
app/services/storage_service.py — Local filesystem image storage.

Saves uploaded image bytes to  ./media/uploads/{shop_id}/{uuid}.{ext}
and returns the (image_url, image_key) tuple that the caller persists in the DB.

Swap this module for an S3 implementation later without touching any other code.
"""
import io
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from app.config import get_settings


class StorageError(Exception):
    """Raised when saving the image to local storage fails."""


class InvalidImageURLError(StorageError):
    """Raised when downloading or validating an image from an external URL fails."""


def _extension_from_content_type(content_type: str) -> str:
    mapping = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
    return mapping.get(content_type.lower(), "bin")


def save_image(
    image_bytes: bytes,
    content_type: str,
    shop_id: str | None = None,
) -> tuple[str, str]:
    """
    Write *image_bytes* to local storage and return (image_url, image_key).

    Args:
        image_bytes:  Raw bytes of the uploaded image.
        content_type: MIME type of the image (e.g. "image/jpeg").
        shop_id:      Optional shop identifier used as a sub-directory.

    Returns:
        (image_url, image_key)
        image_key  → relative path inside the media directory,
                     e.g. "uploads/SHOP-102/3fa8…jpg"
        image_url  → fully-qualified URL the client can use to view the image,
                     e.g. "http://localhost:8000/media/uploads/SHOP-102/3fa8…jpg"

    Raises:
        StorageError: if the write fails for any reason.
    """
    settings = get_settings()

    # Build sub-directory: uploads/{shop_id or 'unassigned'}
    folder_name = shop_id.strip() if shop_id and shop_id.strip() else "unassigned"
    ext = _extension_from_content_type(content_type)
    filename = f"{uuid.uuid4().hex}.{ext}"

    # image_key is the path relative to the media root (served by FastAPI)
    image_key = f"uploads/{folder_name}/{filename}"

    # Absolute path on disk
    media_root = Path(settings.storage_media_dir).parent  # e.g. ./media
    abs_path = media_root / image_key

    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(image_bytes)
    except OSError as exc:
        raise StorageError(f"Failed to write image to {abs_path}: {exc}") from exc

    # Build the public URL
    base = settings.storage_base_url.rstrip("/")
    image_url = f"/media/{image_key}"

    return image_url, image_key


def _detect_image_content_type(data: bytes, header_content_type: str | None) -> str:
    """
    Detect the MIME type using header content-type or binary magic bytes.
    """
    if header_content_type:
        clean_type = header_content_type.split(";")[0].strip().lower()
        if clean_type in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
            return "image/jpeg" if clean_type == "image/jpg" else clean_type

    # Inspect magic bytes
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"

    return header_content_type.split(";")[0].strip().lower() if header_content_type else "application/octet-stream"


def download_and_save_image(
    url: str,
    shop_id: str | None = None,
) -> tuple[str, str]:
    """
    Download an image from a public HTTP/HTTPS URL, validate it,
    save it to local storage, and return (image_url, image_key).

    Args:
        url: Public HTTP/HTTPS URL of the image.
        shop_id: Optional shop identifier for directory partitioning.

    Returns:
        (image_url, image_key)

    Raises:
        InvalidImageURLError: If the URL is invalid, unreachable, not a supported image,
                             or exceeds the maximum file size.
        StorageError: If saving the file locally fails.
    """
    settings = get_settings()

    url_str = (url or "").strip()
    if not url_str:
        raise InvalidImageURLError("Image URL cannot be empty.")

    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidImageURLError(
            f"Invalid URL scheme '{parsed.scheme}'. Only HTTP and HTTPS URLs are supported."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
    }

    req = urllib.request.Request(url_str, headers=headers)
    timeout = float(settings.openrouter_timeout_seconds)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise InvalidImageURLError(
                    f"Failed to fetch image from URL. Server responded with HTTP status {response.status}."
                )

            # Check Content-Length header if available
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                if int(content_length) > settings.max_upload_size_bytes:
                    raise InvalidImageURLError(
                        f"Image from URL is too large ({int(content_length) / 1024 / 1024:.1f} MB). "
                        f"Maximum allowed: {settings.max_upload_size_mb} MB."
                    )

            # Read stream with size limit safeguard
            buffer = io.BytesIO()
            max_bytes = settings.max_upload_size_bytes
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                buffer.write(chunk)
                if buffer.tell() > max_bytes:
                    raise InvalidImageURLError(
                        f"Image from URL exceeds maximum allowed size ({settings.max_upload_size_mb} MB)."
                    )

            image_bytes = buffer.getvalue()
            raw_content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else response.headers.get("Content-Type")

    except urllib.error.HTTPError as exc:
        raise InvalidImageURLError(
            f"HTTP error {exc.code} when downloading image from '{url_str}': {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise InvalidImageURLError(
            f"Failed to connect to image URL '{url_str}': {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise InvalidImageURLError(
            f"Timed out while downloading image from '{url_str}'."
        ) from exc
    except OSError as exc:
        raise InvalidImageURLError(
            f"Network or I/O error while downloading image from '{url_str}': {exc}"
        ) from exc

    if not image_bytes:
        raise InvalidImageURLError(f"Image from URL '{url_str}' is empty (0 bytes).")

    content_type = _detect_image_content_type(image_bytes, raw_content_type)
    if content_type not in settings.allowed_image_types:
        raise InvalidImageURLError(
            f"Unsupported image type: '{content_type}'. "
            f"Allowed types: {', '.join(settings.allowed_image_types)}"
        )

    return save_image(
        image_bytes=image_bytes,
        content_type=content_type,
        shop_id=shop_id,
    )

