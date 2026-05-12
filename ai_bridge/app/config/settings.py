"""Centralized application settings loaded from environment / .env.

All values are optional at import time so the app can boot even with a
partially-filled .env (e.g. no Telegram / NewsAPI keys). Runtime code must
check ``is_configured`` properties before calling external services.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed configuration.

    Reads from ``ai_bridge/.env`` (falls back to process env). Prefix-less:
    variable names match .env.example verbatim.
    """

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────
    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8080, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")

    # ── MiniMax LLM ──────────────────────────────────────────────────────
    minimax_api_key: str = Field(default="", alias="MINIMAX_API_KEY")
    minimax_group_id: str = Field(default="", alias="MINIMAX_GROUP_ID")
    minimax_model: str = Field(default="MiniMax-M2", alias="MINIMAX_MODEL")
    minimax_base_url: str = Field(
        default="https://api.minimax.io/v1", alias="MINIMAX_BASE_URL"
    )
    minimax_timeout: float = Field(default=30.0, alias="MINIMAX_TIMEOUT")
    minimax_max_tokens: int = Field(default=1024, alias="MINIMAX_MAX_TOKENS")
    minimax_temperature: float = Field(default=0.2, alias="MINIMAX_TEMPERATURE")
    llm_mock_mode: bool = Field(default=False, alias="LLM_MOCK_MODE")

    # ── Macro context ────────────────────────────────────────────────────
    newsapi_key: str = Field(default="", alias="NEWSAPI_KEY")
    enable_macro_context: bool = Field(default=True, alias="ENABLE_MACRO_CONTEXT")

    # ── Telegram ─────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # ── Storage ──────────────────────────────────────────────────────────
    sqlite_path: str = Field(default="./data/signals.db", alias="SQLITE_PATH")

    # ── Risk policy ──────────────────────────────────────────────────────
    min_confidence: float = Field(default=0.60, alias="MIN_CONFIDENCE")
    signal_cooldown_seconds: int = Field(default=60, alias="SIGNAL_COOLDOWN_SECONDS")

    # ── Validators ───────────────────────────────────────────────────────
    @field_validator("minimax_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("min_confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("MIN_CONFIDENCE must be between 0.0 and 1.0")
        return v

    # ── Convenience flags ────────────────────────────────────────────────
    @property
    def llm_is_configured(self) -> bool:
        """True if we have a real MiniMax API key (mock mode also counts)."""
        return self.llm_mock_mode or bool(self.minimax_api_key)

    @property
    def telegram_is_configured(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id)

    @property
    def newsapi_is_configured(self) -> bool:
        return bool(self.newsapi_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Use dependency-injection style in tests: ``get_settings.cache_clear()``
    between tests that mutate env.
    """
    return Settings()
