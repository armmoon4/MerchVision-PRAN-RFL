"""
app/services/storage_service.py — Local filesystem image storage.

Saves uploaded image bytes to  ./media/uploads/{shop_id}/{uuid}.{ext}
and returns the (image_url, image_key) tuple that the caller persists in the DB.

Swap this module for an S3 implementation later without touching any other code.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.config import get_settings


class StorageError(Exception):
    """Raised when saving the image to local storage fails."""


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
    image_url = f"{base}/media/{image_key}"

    return image_url, image_key
