"""
macro.py — PATCHED VERSION
Fix: Harga gold tidak valid ($2000-an) padahal harga real ~$4700
Root cause: yfinance bisa return expired futures contract + tidak ada validasi range harga
"""

import os
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Validasi harga (update jika harga gold naik/turun drastis) ──────────────
# Per Mei 2026: gold di sekitar $4500-$5000. Set margin lebar untuk safety.
GOLD_PRICE_MIN = float(os.getenv("GOLD_PRICE_MIN", "3000"))
GOLD_PRICE_MAX = float(os.getenv("GOLD_PRICE_MAX", "7000"))


def _validate_gold_price(price: float, source: str) -> Optional[float]:
    """
    Validasi harga gold masuk akal.
    Ini mencegah dashboard menampilkan harga stale/expired ($2000-an).
    """
    if GOLD_PRICE_MIN <= price <= GOLD_PRICE_MAX:
        return price
    logger.warning(
        f"[macro] ⚠️  Harga dari '{source}' TIDAK VALID: ${price:.2f} "
        f"(expected range: ${GOLD_PRICE_MIN:.0f}–${GOLD_PRICE_MAX:.0f}). "
        f"Mungkin data stale/expired — skip provider ini."
    )
    return None


def _fetch_yfinance() -> Optional[float]:
    """
    Fetch dari Yahoo Finance.
    Gunakan XAUUSD=X (spot) sebagai prioritas, GC=F (futures) sebagai fallback.
    
    NOTE: GC=F adalah kontrak futures yang bisa expired dan return harga lama!
    XAUUSD=X lebih reliable untuk spot price real-time.
    """
    try:
        import yfinance as yf

        for ticker_sym in ["XAUUSD=X", "GC=F"]:
            try:
                ticker = yf.Ticker(ticker_sym)
                # period="5d" lebih reliable dari "1d" untuk menghindari data kosong
                hist = ticker.history(period="5d", interval="1h")
                if hist.empty:
                    logger.debug(f"[macro] yfinance/{ticker_sym}: history kosong")
                    continue
                
                price = float(hist["Close"].dropna().iloc[-1])
                validated = _validate_gold_price(price, f"yfinance/{ticker_sym}")
                if validated:
                    logger.info(f"[macro] ✅ Gold price dari yfinance/{ticker_sym}: ${validated:.2f}")
                    return validated
                    
            except Exception as e:
                logger.debug(f"[macro] yfinance/{ticker_sym} error: {e}")
                continue

    except ImportError:
        logger.warning("[macro] yfinance tidak terinstall")
    except Exception as e:
        logger.warning(f"[macro] yfinance gagal: {e}")

    return None


def _fetch_twelvedata() -> Optional[float]:
    """
    Fetch dari TwelveData API.
    Daftar gratis di: https://twelvedata.com (800 req/day free tier)
    Set TWELVEDATA_API_KEY di .env
    """
    api_key = os.getenv("TWELVEDATA_API_KEY", "")
    if not api_key:
        logger.debug("[macro] TWELVEDATA_API_KEY tidak di-set, skip")
        return None

    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={api_key}"
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()

        if "price" not in data:
            logger.warning(f"[macro] TwelveData response unexpected: {data}")
            return None

        price = float(data["price"])
        validated = _validate_gold_price(price, "TwelveData")
        if validated:
            logger.info(f"[macro] ✅ Gold price dari TwelveData: ${validated:.2f}")
        return validated

    except Exception as e:
        logger.warning(f"[macro] TwelveData gagal: {e}")
        return None


def _fetch_alphavantage() -> Optional[float]:
    """
    Fetch dari AlphaVantage.
    Daftar gratis di: https://www.alphavantage.co (25 req/day free tier)
    Set ALPHAVANTAGE_API_KEY di .env
    """
    api_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
    if not api_key:
        logger.debug("[macro] ALPHAVANTAGE_API_KEY tidak di-set, skip")
        return None

    try:
        url = (
            "https://www.alphavantage.co/query"
            "?function=CURRENCY_EXCHANGE_RATE"
            f"&from_currency=XAU&to_currency=USD&apikey={api_key}"
        )
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()

        rate_data = data.get("Realtime Currency Exchange Rate", {})
        if not rate_data:
            logger.warning(f"[macro] AlphaVantage response kosong: {data}")
            return None

        price = float(rate_data.get("5. Exchange Rate", 0))
        validated = _validate_gold_price(price, "AlphaVantage")
        if validated:
            logger.info(f"[macro] ✅ Gold price dari AlphaVantage: ${validated:.2f}")
        return validated

    except Exception as e:
        logger.warning(f"[macro] AlphaVantage gagal: {e}")
        return None


def _fetch_metals_api() -> Optional[float]:
    """
    Fetch dari metals-api.com.
    Daftar gratis di: https://metals-api.com
    Set METALS_API_KEY di .env
    
    NOTE: metals-api return XAU per 1 USD, perlu di-invert.
    """
    api_key = os.getenv("METALS_API_KEY", "")
    if not api_key:
        logger.debug("[macro] METALS_API_KEY tidak di-set, skip")
        return None

    try:
        url = f"https://metals-api.com/api/latest?access_key={api_key}&base=USD&symbols=XAU"
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            logger.warning(f"[macro] metals-api error: {data.get('error')}")
            return None

        xau_per_usd = float(data["rates"]["XAU"])
        if xau_per_usd == 0:
            return None
            
        price = 1.0 / xau_per_usd  # Invert: USD per troy oz
        validated = _validate_gold_price(price, "metals-api")
        if validated:
            logger.info(f"[macro] ✅ Gold price dari metals-api: ${validated:.2f}")
        return validated

    except Exception as e:
        logger.warning(f"[macro] metals-api gagal: {e}")
        return None


def get_gold_price() -> float:
    """
    Fetch harga gold spot (XAU/USD) dengan multi-provider fallback chain.
    
    Provider chain (berurutan):
    1. yfinance/XAUUSD=X  → spot price Yahoo Finance
    2. yfinance/GC=F      → futures Yahoo Finance (fallback)
    3. TwelveData         → requires TWELVEDATA_API_KEY
    4. AlphaVantage       → requires ALPHAVANTAGE_API_KEY
    5. metals-api         → requires METALS_API_KEY
    
    Setiap provider divalidasi: harga harus antara $3000-$7000 (per 2026).
    Jika semua gagal, raise RuntimeError (jangan return nilai stale/default!).
    
    Returns:
        float: Harga XAU/USD dalam USD per troy ounce
    
    Raises:
        RuntimeError: Jika semua provider gagal
    """
    providers = [
        ("yfinance", _fetch_yfinance),
        ("TwelveData", _fetch_twelvedata),
        ("AlphaVantage", _fetch_alphavantage),
        ("metals-api", _fetch_metals_api),
    ]

    for name, provider in providers:
        try:
            price = provider()
            if price is not None:
                return price
        except Exception as e:
            logger.warning(f"[macro] Provider '{name}' exception: {e}")
            continue

    raise RuntimeError(
        "❌ Semua provider harga gold gagal! "
        "Periksa: (1) koneksi VPS ke Yahoo Finance, "
        "(2) TWELVEDATA_API_KEY / ALPHAVANTAGE_API_KEY di .env, "
        "(3) firewall rules di VPS."
    )


# ── Fungsi get_macro_context yang sudah ada (tidak diubah, hanya ditambah) ──

def get_macro_context() -> dict:
    """
    Fetch macro context: gold price, DXY, US10Y yield, gold news.
    Versi yang sudah di-patch untuk harga yang benar.
    """
    context = {}

    # Gold price — gunakan get_gold_price() yang sudah di-fix
    try:
        context["gold_price"] = get_gold_price()
    except RuntimeError as e:
        logger.error(f"[macro] Gagal fetch gold price: {e}")
        context["gold_price"] = None

    # DXY dan US10Y — bisa tetap pakai yfinance, karena tidak ada masalah
    # validasi range seperti gold
    try:
        import yfinance as yf
        dxy = yf.Ticker("DX-Y.NYB")
        dxy_hist = dxy.history(period="2d", interval="1h")
        if not dxy_hist.empty:
            context["dxy"] = float(dxy_hist["Close"].dropna().iloc[-1])
    except Exception as e:
        logger.warning(f"[macro] DXY fetch gagal: {e}")
        context["dxy"] = None

    try:
        import yfinance as yf
        tnx = yf.Ticker("^TNX")
        tnx_hist = tnx.history(period="2d", interval="1h")
        if not tnx_hist.empty:
            context["us10y"] = float(tnx_hist["Close"].dropna().iloc[-1])
    except Exception as e:
        logger.warning(f"[macro] US10Y fetch gagal: {e}")
        context["us10y"] = None

    return context
