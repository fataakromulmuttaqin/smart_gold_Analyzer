"""Macro context provider.

Pulls **DXY** (USD index) and **US 10Y yield** using a multi-provider
fallback strategy:
  1. yfinance (primary — but often blocked on VPS / servers)
  2. Twelve Data API (free tier, 800 req/day)
  3. Alpha Vantage API (free tier, 25 req/day)

Gold news headlines via NewsAPI.org.

All fetches are best-effort — any failure degrades gracefully and sets
``partial=True`` on the returned :class:`MacroContext`.

Gold correlation reminder:
  * DXY up → gold typically down (-0.7..-0.9 historical correlation)
  * Real yields up → gold down
  * Risk-off headlines → gold up
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config.settings import Settings, get_settings
from app.models.schemas import MacroContext
from app.utils.logging import logger


# ────────────────────────────────────────────────────────────────────────
# Provider 1: yfinance (sync lib; run in thread pool)
# ────────────────────────────────────────────────────────────────────────
def _fetch_yf_ticker_blocking(symbol: str) -> dict[str, Any] | None:
    """Return last 2 daily closes for a yfinance symbol. None on any error."""
    try:
        import yfinance as yf  # imported lazily so module works without it
    except ImportError:
        logger.debug("yfinance not installed — skipping yfinance provider")
        return None

    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 2:
            return None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        return {"last": last, "prev": prev, "source": "yfinance"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance fetch failed for {}: {}", symbol, exc)
        return None


async def _fetch_yf_ticker(symbol: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_fetch_yf_ticker_blocking, symbol)


# ────────────────────────────────────────────────────────────────────────
# Provider 2: Twelve Data (free tier — 800 calls/day, no VPS block)
# https://twelvedata.com/docs
# ────────────────────────────────────────────────────────────────────────
async def _fetch_twelvedata(
    symbol: str, api_key: str, timeout: float = 10.0
) -> dict[str, Any] | None:
    """Fetch last 2 closes from Twelve Data time_series endpoint."""
    if not api_key:
        return None

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": 2,
        "apikey": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning(
                "TwelveData HTTP {} for {} — skipping", resp.status_code, symbol
            )
            return None
        data = resp.json()
        if "values" not in data or len(data["values"]) < 2:
            logger.warning("TwelveData returned insufficient data for {}", symbol)
            return None
        # values[0] = most recent, values[1] = previous
        last = float(data["values"][0]["close"])
        prev = float(data["values"][1]["close"])
        return {"last": last, "prev": prev, "source": "twelvedata"}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("TwelveData fetch failed for {}: {}", symbol, exc)
        return None


# ────────────────────────────────────────────────────────────────────────
# Provider 3: Alpha Vantage (free tier — 25 calls/day)
# https://www.alphavantage.co/documentation/
# ────────────────────────────────────────────────────────────────────────
async def _fetch_alphavantage(
    symbol: str, api_key: str, timeout: float = 15.0
) -> dict[str, Any] | None:
    """Fetch last 2 closes from Alpha Vantage GLOBAL_QUOTE + previous close."""
    if not api_key:
        return None

    # For forex/index symbols, use FX_DAILY or a direct function
    # Alpha Vantage uses different symbols: DXY = "DX-Y.NYB" not available directly,
    # so we use the "REAL_GDP" function for forex. For simplicity, use TIME_SERIES_DAILY.
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "compact",
        "apikey": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning(
                "AlphaVantage HTTP {} for {} — skipping", resp.status_code, symbol
            )
            return None
        data = resp.json()
        ts_key = "Time Series (Daily)"
        if ts_key not in data:
            logger.warning("AlphaVantage no time series data for {}", symbol)
            return None
        dates = sorted(data[ts_key].keys(), reverse=True)
        if len(dates) < 2:
            return None
        last = float(data[ts_key][dates[0]]["4. close"])
        prev = float(data[ts_key][dates[1]]["4. close"])
        return {"last": last, "prev": prev, "source": "alphavantage"}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("AlphaVantage fetch failed for {}: {}", symbol, exc)
        return None


# ────────────────────────────────────────────────────────────────────────
# Multi-provider orchestrator — tries providers in order until one works
# ────────────────────────────────────────────────────────────────────────

# Symbol mappings per provider
_SYMBOL_MAP = {
    "yfinance": {"dxy": "DX-Y.NYB", "us10y": "^TNX"},
    "twelvedata": {"dxy": "DXY", "us10y": "US10Y"},
    "alphavantage": {"dxy": "UUP", "us10y": "TLT"},  # ETF proxies as fallback
}


async def _fetch_with_fallback(
    instrument: str,
    settings: Settings,
) -> dict[str, Any] | None:
    """Try multiple providers for an instrument (dxy or us10y).

    Returns the first successful result, or None if all fail.
    """
    # Provider 1: yfinance
    yf_symbol = _SYMBOL_MAP["yfinance"][instrument]
    result = await _fetch_yf_ticker(yf_symbol)
    if result is not None:
        logger.debug("{} fetched via yfinance", instrument.upper())
        return result

    logger.info(
        "yfinance failed for {} — trying fallback providers...",
        instrument.upper(),
    )

    # Provider 2: Twelve Data
    if settings.twelvedata_api_key:
        td_symbol = _SYMBOL_MAP["twelvedata"][instrument]
        result = await _fetch_twelvedata(td_symbol, settings.twelvedata_api_key)
        if result is not None:
            logger.info("{} fetched via TwelveData ✓", instrument.upper())
            return result

    # Provider 3: Alpha Vantage
    if settings.alphavantage_api_key:
        av_symbol = _SYMBOL_MAP["alphavantage"][instrument]
        result = await _fetch_alphavantage(av_symbol, settings.alphavantage_api_key)
        if result is not None:
            logger.info("{} fetched via AlphaVantage ✓", instrument.upper())
            return result

    logger.warning("All providers failed for {} — data unavailable", instrument.upper())
    return None


# ────────────────────────────────────────────────────────────────────────
# NewsAPI helper (async via httpx)
# ────────────────────────────────────────────────────────────────────────
async def _fetch_gold_news(
    api_key: str, limit: int = 5, timeout: float = 10.0
) -> list[str]:
    """Return up to ``limit`` recent gold-related headlines. [] on failure."""
    if not api_key:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": '("gold price" OR XAU OR "Federal Reserve" OR "US dollar") AND '
             "(inflation OR rate OR FOMC OR CPI OR NFP)",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": limit,
        "apiKey": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning(
                "NewsAPI returned HTTP {} — skipping news context",
                resp.status_code,
            )
            return []
        data = resp.json()
        return [
            f"[{a.get('source', {}).get('name', '?')}] {a.get('title', '')}"
            for a in data.get("articles", [])[:limit]
            if a.get("title")
        ]
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("NewsAPI fetch failed: {}", exc)
        return []


# ────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────
async def fetch_macro_context(settings: Settings | None = None) -> MacroContext:
    """Fetch DXY, US10Y and headlines concurrently. Never raises."""
    s = settings or get_settings()
    ctx = MacroContext()

    if not s.enable_macro_context:
        ctx.partial = True
        ctx.notes.append("macro disabled via ENABLE_MACRO_CONTEXT=false")
        return ctx

    # Fetch DXY + US10Y with multi-provider fallback, and news concurrently
    dxy_task = _fetch_with_fallback("dxy", s)
    tnx_task = _fetch_with_fallback("us10y", s)
    news_task = (
        _fetch_gold_news(s.newsapi_key) if s.newsapi_is_configured else _noop_news()
    )

    dxy, tnx, headlines = await asyncio.gather(dxy_task, tnx_task, news_task)

    if dxy is not None:
        ctx.dxy_price = round(dxy["last"], 3)
        ctx.dxy_change_pct = round(
            (dxy["last"] - dxy["prev"]) / dxy["prev"] * 100.0, 3
        )
        if dxy.get("source") != "yfinance":
            ctx.notes.append(f"DXY via {dxy['source']}")
    else:
        ctx.partial = True
        ctx.notes.append("DXY unavailable (all providers failed)")

    if tnx is not None:
        # ^TNX from yfinance is yield × 10; other providers return raw yield %
        if tnx.get("source") == "yfinance":
            ctx.us10y_yield = round(tnx["last"] / 10.0, 3)
            ctx.us10y_change_bp = round((tnx["last"] - tnx["prev"]) * 10.0, 2)
        else:
            # TwelveData / AlphaVantage return the yield directly as percentage
            ctx.us10y_yield = round(tnx["last"], 3)
            ctx.us10y_change_bp = round(
                (tnx["last"] - tnx["prev"]) * 100.0, 2
            )
            ctx.notes.append(f"US10Y via {tnx['source']}")
    else:
        ctx.partial = True
        ctx.notes.append("US10Y yield unavailable (all providers failed)")

    if headlines:
        ctx.news_headlines = headlines
    elif s.newsapi_is_configured:
        ctx.partial = True
        ctx.notes.append("NewsAPI returned no headlines")
    else:
        ctx.notes.append("NewsAPI not configured")

    logger.info(
        "Macro context: DXY={} ({}%) US10Y={}% headlines={} partial={}",
        ctx.dxy_price,
        ctx.dxy_change_pct,
        ctx.us10y_yield,
        len(ctx.news_headlines),
        ctx.partial,
    )
    return ctx


async def _noop_news() -> list[str]:
    return []


__all__ = ["fetch_macro_context"]
