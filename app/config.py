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

    # ── AI Provider ───────────────────────────────────────────────────────────
    # Options: "openrouter" (default) or "gemini"
    ai_provider: str = "openrouter"

    # ── OpenRouter ────────────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "google/gemini-3.7-flash"
    openrouter_timeout_seconds: int = 45

    # ── Google Gemini Direct ──────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    gemini_timeout_seconds: int = 30
    gemini_thinking_budget: int = 1024  # Capped reasoning budget for cost efficiency
    gemini_thinking_level: str = "medium"  # low, medium, or high (minimal not supported in 3.7)
    max_image_dimension: int = 1600  # Optimal resolution (Lanczos) for low vision input tokens

    # ── Token Optimization Settings ───────────────────────────────────────────
    # Adaptive Thinking (Optimization #1): scale budget by image complexity
    adaptive_thinking: bool = True
    adaptive_thinking_low_threshold: float = 0.35   # complexity < this → budget=0
    adaptive_thinking_high_threshold: float = 0.65  # complexity >= this → full budget

    # Smart Image Format (Optimization #2): WebP saves 25-35% vs JPEG at same quality
    image_output_format: str = "webp"  # "webp" or "jpeg"
    jpeg_quality: int = 83             # 83 = sweet spot for text legibility vs file size

    # Two-Pass Strategy (Optimization #3): cheap small pass first
    enable_two_pass: bool = True
    two_pass_first_dimension: int = 512  # px for Pass 1 (cheap thumbnail pass)

    # ── Token Limits (Gemini 3.7 Flash) ───────────────────────────────────────
    max_input_tokens: int = 1_048_576
    max_output_tokens: int = 65_536

    # ── Token Pricing (USD per 1M tokens) ─────────────────────────────────────
    # OpenRouter listed rates for google/gemini-3.7-flash: $0.75/1M input, $3.75/1M output
    token_cost_input_per_million: float = 0.75
    token_cost_output_per_million: float = 3.75

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
