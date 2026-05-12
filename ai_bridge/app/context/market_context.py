"""Macro context provider.

Pulls **DXY** (USD index) and **US 10Y yield** via ``yfinance`` and gold
news headlines via NewsAPI.org. All fetches are best-effort — any failure
degrades gracefully and sets ``partial=True`` on the returned
:class:`MacroContext`.

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
# yfinance helpers (sync lib; run in thread pool to avoid blocking loop)
# ────────────────────────────────────────────────────────────────────────
def _fetch_yf_ticker_blocking(symbol: str) -> dict[str, Any] | None:
    """Return last 2 daily closes for a yfinance symbol. None on any error."""
    try:
        import yfinance as yf  # imported lazily so module works without it
    except ImportError:
        logger.warning("yfinance not installed — macro DXY/yield fetch disabled")
        return None

    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 2:
            return None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        return {"last": last, "prev": prev}
    except Exception as exc:  # noqa: BLE001 — yfinance raises many different errors
        logger.warning("yfinance fetch failed for {}: {}", symbol, exc)
        return None


async def _fetch_yf_ticker(symbol: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_fetch_yf_ticker_blocking, symbol)


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

    # Symbols: ^DX-Y.NYB = DXY futures, ^TNX = 10Y Treasury yield x10
    dxy_task = _fetch_yf_ticker("DX-Y.NYB")
    tnx_task = _fetch_yf_ticker("^TNX")
    news_task = _fetch_gold_news(s.newsapi_key) if s.newsapi_is_configured else _noop_news()

    dxy, tnx, headlines = await asyncio.gather(dxy_task, tnx_task, news_task)

    if dxy is not None:
        ctx.dxy_price = round(dxy["last"], 3)
        ctx.dxy_change_pct = round(
            (dxy["last"] - dxy["prev"]) / dxy["prev"] * 100.0, 3
        )
    else:
        ctx.partial = True
        ctx.notes.append("DXY unavailable")

    if tnx is not None:
        # ^TNX is yield × 10 → divide by 10 to get percentage
        ctx.us10y_yield = round(tnx["last"] / 10.0, 3)
        ctx.us10y_change_bp = round((tnx["last"] - tnx["prev"]) * 10.0, 2)
    else:
        ctx.partial = True
        ctx.notes.append("US10Y yield unavailable")

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
