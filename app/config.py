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
    # Best cost-performance balance: google/gemini-2.5-flash (~60% cheaper than 3.7 Flash with top accuracy)
    openrouter_model: str = "google/gemini-2.5-flash"
    openrouter_timeout_seconds: int = 45
    # OpenRouter reasoning control ("none", "low", "medium", "high"):
    # Setting to "none" prevents reasoning-enabled models from wasting 1,000+ reasoning tokens per image (~4s latency savings)
    openrouter_reasoning_effort: str = "none"
    openrouter_reasoning_max_tokens: int = 0
    openrouter_temperature: float = 0.1
    openrouter_frequency_penalty: float = 0.2
    openrouter_enable_cache_header: bool = True

    # ── Google Gemini Direct ──────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: int = 30
    gemini_thinking_budget: int = 0  # 0 disables reasoning latency for pure shelf extraction
    gemini_thinking_level: str = "low"  # low, medium, or high
    gemini_max_output_tokens: int = 1000
    gemini_temperature: float = 0.0
    # Media resolution for Gemini SDK (low: ~280 tokens, medium: ~560 tokens, high: ~1120 tokens)
    gemini_media_resolution: str = "medium"

    # ── Vision Token & Image Optimization ─────────────────────────────────────
    # Tile-aligned: 1024px preserves small label/spice text readability while staying within 2 tiles (~774 tokens)
    max_image_dimension: int = 1024
    image_quality: int = 82  # Optimal balance between visual clarity and byte transfer
    enable_image_cache: bool = True  # Exact SHA-256 duplicate cache (0 tokens on duplicate)
    image_cache_ttl_hours: int = 72  # Cache validity window

    # ── Token Limits ──────────────────────────────────────────────────────────
    max_input_tokens: int = 1_048_576
    max_output_tokens: int = 65_536

    # ── Token Pricing (USD per 1M tokens) ─────────────────────────────────────
    # Rates for Gemini 2.5 Flash: ~$0.30/1M input, ~$2.50/1M output
    token_cost_input_per_million: float = 0.30
    token_cost_output_per_million: float = 2.50

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
