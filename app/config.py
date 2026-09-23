"""
app/config.py — Application configuration loaded from .env via pydantic-settings.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./pran_rfl.db"

    # ── Gemini ────────────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_timeout_seconds: int = 30
    gemini_thinking_budget: int = 1024  # Capped reasoning budget for cost efficiency
    max_image_dimension: int = 1600  # Optimal resolution (Lanczos) for low vision input tokens

    # ── Token Pricing (USD per 1M tokens) ——————————————————————————
    # Default rates based on Gemini 3.6 Flash: $0.10/1M prompt, $0.40/1M completion
    token_cost_input_per_million: float = 0.10
    token_cost_output_per_million: float = 0.40

    # ── Local Storage ─────────────────────────────────────────────────────────
    # Base URL used to construct public image_url (e.g. http://localhost:8000)
    storage_base_url: str = ""
    # Directory where uploaded images are written (relative to cwd)
    storage_media_dir: str = "media/uploads"

    # ── Upload Constraints ────────────────────────────────────────────────────
    max_upload_size_mb: int = 10
    allowed_image_types: List[str] = ["image/jpeg", "image/png", "image/webp"]

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_allowed_origins: str = "http://localhost:3000,http://localhost:8000"

    @property
    def cors_origins_list(self) -> List[str]:
        """Split the comma-separated CORS string into a list."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
