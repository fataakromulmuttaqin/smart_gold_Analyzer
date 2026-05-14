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

    # ── Fallback data providers (when Yahoo Finance is blocked) ───────────
    # Twelve Data: free tier = 800 API calls/day. Sign up: https://twelvedata.com
    twelvedata_api_key: str = Field(default="", alias="TWELVEDATA_API_KEY")
    # Alpha Vantage: free tier = 25 API calls/day. Sign up: https://www.alphavantage.co
    alphavantage_api_key: str = Field(default="", alias="ALPHAVANTAGE_API_KEY")
    # Metals-API: free tier available. Sign up: https://metals-api.com
    metals_api_key: str = Field(default="", alias="METALS_API_KEY")

    # ── Gold price validation bounds ────────────────────────────────────
    # Reject fetched prices outside this range (prevents stale/expired data)
    # Per 2026: gold is ~$4,500-$5,000. Set wide margin for safety.
    gold_price_min: float = Field(default=3000.0, alias="GOLD_PRICE_MIN")
    gold_price_max: float = Field(default=7000.0, alias="GOLD_PRICE_MAX")

    # ── Telegram ─────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # ── Storage ──────────────────────────────────────────────────────────
    sqlite_path: str = Field(default="./data/signals.db", alias="SQLITE_PATH")

    # ── Risk policy ──────────────────────────────────────────────────────
    min_confidence: float = Field(default=0.60, alias="MIN_CONFIDENCE")
    signal_cooldown_seconds: int = Field(default=60, alias="SIGNAL_COOLDOWN_SECONDS")

    # ── Monitor ─────────────────────────────────────────────────────────
    monitor_silence_hours: float = Field(
        default=6.0, alias="MONITOR_SILENCE_HOURS",
    )
    monitor_notify_skip: bool = Field(
        default=True, alias="MONITOR_NOTIFY_SKIP",
    )
    monitor_notify_execute: bool = Field(
        default=True, alias="MONITOR_NOTIFY_EXECUTE",
    )

    # ── Safety guards ────────────────────────────────────────────────────
    guard_max_daily_trades: int = Field(default=5, alias="GUARD_MAX_DAILY_TRADES")
    guard_max_daily_drawdown_r: float = Field(
        default=-3.0, alias="GUARD_MAX_DAILY_DRAWDOWN_R"
    )
    guard_max_spread_points: float = Field(
        default=150.0, alias="GUARD_MAX_SPREAD_POINTS"
    )
    guard_news_blackout: bool = Field(default=True, alias="GUARD_NEWS_BLACKOUT")
    guard_max_atr: float = Field(default=50.0, alias="GUARD_MAX_ATR")

    # ── Stop Loss policy ────────────────────────────────────────────────
    # Policy options:
    #   "hybrid" (default, RECOMMENDED) — PSAR with ATR bounds
    #   "psar"   — pure PSAR distance (no ATR floor/cap)
    #   "atr"    — classic fixed ATR multiple
    sl_policy: str = Field(default="hybrid", alias="SL_POLICY")
    sl_atr_mult: float = Field(default=1.5, alias="SL_ATR_MULT")
    sl_min_atr_mult: float = Field(default=0.8, alias="SL_MIN_ATR_MULT")
    sl_max_atr_mult: float = Field(default=2.5, alias="SL_MAX_ATR_MULT")

    # ── Breakeven stop shift ────────────────────────────────────────────
    sl_breakeven_enabled: bool = Field(default=True, alias="SL_BREAKEVEN_ENABLED")
    sl_breakeven_trigger_r: float = Field(
        default=1.0, alias="SL_BREAKEVEN_TRIGGER_R"
    )
    sl_breakeven_buffer_atr_mult: float = Field(
        default=0.1, alias="SL_BREAKEVEN_BUFFER_ATR_MULT"
    )

    # ── Trade plan defaults ─────────────────────────────────────────────
    # RR used when the LLM decision does not provide a suggested_rr.
    sl_default_rr: float = Field(default=2.0, alias="SL_DEFAULT_RR")
    # Hypothetical equity used to produce a sizing *estimate* in the plan.
    # This is display-only when MT5 is off (signal-only mode). When MT5 is
    # live the actual equity from account_info() overrides this.
    plan_equity_hint: float = Field(default=10000.0, alias="PLAN_EQUITY_HINT")

    # ── Risk sizing ─────────────────────────────────────────────────────
    risk_per_trade_pct: float = Field(default=1.0, alias="RISK_PER_TRADE_PCT")
    risk_per_trade_pct_reduce: float = Field(
        default=0.5, alias="RISK_PER_TRADE_PCT_REDUCE"
    )

    # ── cTrader MCP execution (RECOMMENDED for Linux) ──────────────────
    # Pure HTTP POST to mcp.ctrader.com. No WebSocket, no Wine, no GUI.
    # Just paste the bearer token from cTrader platform settings.
    # Token is a base64 JSON: {"plant":"ctrader","environment":"demo","token":"..."}
    ctrader_enabled: bool = Field(default=False, alias="CTRADER_ENABLED")
    ctrader_token: str = Field(default="", alias="CTRADER_TOKEN")
    ctrader_symbol: str = Field(default="XAUUSD", alias="CTRADER_SYMBOL")
    ctrader_fixed_lot: float = Field(default=0.0, alias="CTRADER_FIXED_LOT")
    ctrader_label: str = Field(default="SmartGold", alias="CTRADER_LABEL")

    # ── MT5 broker execution (legacy, Windows only) ──────────────────────
    # Opt-in: when MT5_ENABLED=false (default) the bridge uses NoopExecutor.
    # MT5 only works on Windows / Wine; on Linux the import will fail and
    # we fall back to NoopExecutor automatically.
    mt5_enabled: bool = Field(default=False, alias="MT5_ENABLED")
    mt5_login: str = Field(default="", alias="MT5_LOGIN")
    mt5_password: str = Field(default="", alias="MT5_PASSWORD")
    mt5_server: str = Field(default="", alias="MT5_SERVER")
    mt5_symbol: str = Field(default="", alias="MT5_SYMBOL")
    mt5_risk_pct: float = Field(default=1.0, alias="MT5_RISK_PCT")
    mt5_fixed_lot: float = Field(default=0.0, alias="MT5_FIXED_LOT")
    mt5_deviation: int = Field(default=20, alias="MT5_DEVIATION")
    mt5_magic: int = Field(default=260512, alias="MT5_MAGIC")
    mt5_fallback_stop_points: int = Field(
        default=2000, alias="MT5_FALLBACK_STOP_POINTS"
    )

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

    @property
    def ctrader_is_configured(self) -> bool:
        """True if cTrader is enabled and the MCP token is present."""
        return self.ctrader_enabled and bool(self.ctrader_token)

    @property
    def mt5_is_configured(self) -> bool:
        """True if MT5 is enabled and the essential credentials are present.

        Note: even when this returns True, the MetaTrader5 package may fail
        to import on Linux — the factory handles that gracefully.
        """
        return (
            self.mt5_enabled
            and bool(self.mt5_login)
            and bool(self.mt5_password)
            and bool(self.mt5_server)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Use dependency-injection style in tests: ``get_settings.cache_clear()``
    between tests that mutate env.
    """
    return Settings()
