"""
app/services/storage_service.py — In-memory image processing and validation (Zero Disk Footprint).

Handles in-memory validation of S3/HTTP URLs, base64 strings, and binary streams
without writing or persisting any files to local storage or disk.
"""
import io
import logging
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base exception for image processing and storage errors."""


class InvalidImageURLError(StorageError):
    """Raised when downloading or validating an image from an external URL fails."""


def _detect_image_content_type(data: bytes, header_content_type: str | None = None) -> str:
    """
    Detect the MIME type using header content-type or binary magic bytes.
    """
    if header_content_type:
        clean_type = header_content_type.split(";")[0].strip().lower()
        if clean_type in ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"):
            return "image/jpeg" if clean_type == "image/jpg" else clean_type

    # Inspect magic bytes
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"

    return header_content_type.split(";")[0].strip().lower() if header_content_type else "image/jpeg"


def download_image_to_memory(url: str) -> tuple[bytes, str]:
    """
    Download an image from a public HTTP/HTTPS or S3 pre-signed URL directly into memory,
    validate content type and size limits, and return (image_bytes, content_type).
    Zero disk storage used.
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
    timeout = float(settings.gemini_timeout_seconds)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise InvalidImageURLError(
                    f"Failed to fetch image from URL. Server responded with HTTP status {response.status}."
                )

            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                if int(content_length) > settings.max_upload_size_bytes:
                    raise InvalidImageURLError(
                        f"Image from URL is too large ({int(content_length) / 1024 / 1024:.1f} MB). "
                        f"Maximum allowed: {settings.max_upload_size_mb} MB."
                    )

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
            raw_content_type = (
                response.headers.get_content_type()
                if hasattr(response.headers, "get_content_type")
                else response.headers.get("Content-Type")
            )

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

    return image_bytes, content_type


def save_image(
    image_bytes: bytes,
    content_type: str,
) -> tuple[str, str]:
    """
    Zero-disk stub: returns an in-memory reference identifier without writing to local disk.
    """
    return "", ""


def download_and_save_image(
    url: str,
) -> tuple[str, str]:
    """
    Zero-disk validation: validates the remote S3/HTTP URL and returns (url, "")
    without saving any file to local disk.
    """
    # Validate in-memory to ensure URL is reachable and points to a valid image
    download_image_to_memory(url)
    return url, ""

