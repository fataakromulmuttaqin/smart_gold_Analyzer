# 🔍 Analisis & Perbaikan: SmartGold Analyzer Pro

**Repo:** https://github.com/fataakromulmuttaqin/smart_gold_Analyzer  
**Tanggal Analisis:** 14 Mei 2026  
**Harga Gold Saat Ini:** ~$4,688/oz (XAU/USD)

---

## ❗ ROOT CAUSE: Masalah Harga $2000-an di Dashboard

### Diagnosisnya

Website menampilkan ~$2000 padahal harga real gold ~$4,688. Ini **bukan bug UI** — ini bug di **cara fetching data harga gold**. Ada 3 kemungkinan penyebab:

### Penyebab #1 — Ticker yfinance salah (PALING MUNGKIN)
Di backtesting, repo ini menggunakan `GC=F` (Gold Futures). Harga futures gold **berbeda signifikan** dari spot price XAU/USD. Lebih parahnya, `yfinance` kadang mengembalikan data **kontrak lama** yang sudah expired, sehingga harga yang muncul adalah harga kontrak bulan lalu (atau bahkan tahun lalu = ~$2000-$2100 range di 2023).

**Fix:** Ganti ticker ke `GC=F` dengan fallback ke `XAUUSD=X` untuk spot price.

### Penyebab #2 — yfinance ter-block di VPS
README sendiri menyebut: *"Multi-provider fallback for VPS-blocked Yahoo"*. Jika yfinance ter-block, sistem bisa jatuh ke **hardcoded fallback value** atau mengambil data dari cache lama (2023 = ~$2000).

**Fix:** Perbaiki chain fallback provider dengan menggunakan TwelveData/AlphaVantage sebagai primary.

### Penyebab #3 — Cache SQLite tidak di-invalidate
Dashboard mengambil harga dari audit log SQLite terakhir. Jika webhook signal belum masuk (no active trading signal), harga yang tampil adalah harga dari signal terakhir yang tersimpan — yang bisa berbulan-bulan lalu.

**Fix:** Tambahkan endpoint khusus untuk fetch harga live, terpisah dari signal log.

---

## 🔧 FILE YANG PERLU DIPERBAIKI

### 1. `ai_bridge/app/macro.py` — FIX UTAMA

**Ganti fungsi fetch gold price:**

```python
# ❌ SEBELUM (bermasalah):
import yfinance as yf

def get_gold_price() -> float:
    ticker = yf.Ticker("GC=F")
    hist = ticker.history(period="1d")
    return float(hist["Close"].iloc[-1])  # ← Bisa return data lama/expired!

# ✅ SESUDAH (fix dengan multi-provider + validasi harga):
import httpx
import yfinance as yf
import logging

logger = logging.getLogger(__name__)

GOLD_PRICE_MIN = 3000.0   # Validasi: harga gold tidak mungkin < $3000 di 2026
GOLD_PRICE_MAX = 7000.0   # Validasi: harga gold tidak mungkin > $7000

def _validate_price(price: float, source: str) -> float | None:
    """Validasi harga gold masuk akal untuk kondisi pasar saat ini."""
    if GOLD_PRICE_MIN <= price <= GOLD_PRICE_MAX:
        return price
    logger.warning(f"[macro] Harga dari {source} tidak valid: ${price:.2f} "
                   f"(expected ${GOLD_PRICE_MIN}-${GOLD_PRICE_MAX})")
    return None

def _fetch_gold_yfinance() -> float | None:
    """Fetch dari Yahoo Finance dengan validasi ketat."""
    try:
        import yfinance as yf
        # Coba spot price dulu, lalu futures
        for ticker_sym in ["XAUUSD=X", "GC=F"]:
            ticker = yf.Ticker(ticker_sym)
            # Gunakan period="5d" untuk lebih reliable vs "1d"
            hist = ticker.history(period="5d", interval="1h")
            if hist.empty:
                continue
            # Ambil data TERBARU (bukan iloc[-1] yang bisa stale)
            latest = hist["Close"].dropna().iloc[-1]
            price = float(latest)
            validated = _validate_price(price, f"yfinance/{ticker_sym}")
            if validated:
                logger.info(f"[macro] Gold price from yfinance/{ticker_sym}: ${validated:.2f}")
                return validated
    except Exception as e:
        logger.warning(f"[macro] yfinance failed: {e}")
    return None

def _fetch_gold_twelvedata() -> float | None:
    """Fetch dari TwelveData API."""
    api_key = os.getenv("TWELVEDATA_API_KEY", "")
    if not api_key:
        return None
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={api_key}"
        resp = httpx.get(url, timeout=5.0)
        data = resp.json()
        price = float(data.get("price", 0))
        return _validate_price(price, "TwelveData")
    except Exception as e:
        logger.warning(f"[macro] TwelveData failed: {e}")
    return None

def _fetch_gold_alphavantage() -> float | None:
    """Fetch dari AlphaVantage API."""
    api_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
    if not api_key:
        return None
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=CURRENCY_EXCHANGE_RATE"
            f"&from_currency=XAU&to_currency=USD&apikey={api_key}"
        )
        resp = httpx.get(url, timeout=5.0)
        data = resp.json()
        rate_data = data.get("Realtime Currency Exchange Rate", {})
        price = float(rate_data.get("5. Exchange Rate", 0))
        return _validate_price(price, "AlphaVantage")
    except Exception as e:
        logger.warning(f"[macro] AlphaVantage failed: {e}")
    return None

def _fetch_gold_metals_api() -> float | None:
    """
    Fetch dari metals-api.com (free tier tersedia).
    Tambahkan METALS_API_KEY ke .env untuk menggunakan ini.
    """
    api_key = os.getenv("METALS_API_KEY", "")
    if not api_key:
        return None
    try:
        url = f"https://metals-api.com/api/latest?access_key={api_key}&base=USD&symbols=XAU"
        resp = httpx.get(url, timeout=5.0)
        data = resp.json()
        # metals-api returns XAU per 1 USD, so invert
        xau_per_usd = float(data["rates"]["XAU"])
        price = 1.0 / xau_per_usd  # USD per 1 troy oz
        return _validate_price(price, "metals-api")
    except Exception as e:
        logger.warning(f"[macro] metals-api failed: {e}")
    return None

def get_gold_price() -> float:
    """
    Fetch harga gold spot (XAU/USD) dengan multi-provider fallback.
    Chain: yfinance → TwelveData → AlphaVantage → metals-api
    Setiap provider divalidasi: harga harus antara $3000-$7000.
    """
    providers = [
        _fetch_gold_yfinance,
        _fetch_gold_twelvedata,
        _fetch_gold_alphavantage,
        _fetch_gold_metals_api,
    ]
    
    for provider in providers:
        price = provider()
        if price is not None:
            return price
    
    # Jika semua gagal, raise error (jangan return hardcoded/stale value!)
    raise RuntimeError(
        "Semua provider harga gold gagal. "
        "Periksa API keys dan koneksi VPS."
    )
```

---

### 2. `ai_bridge/app/routes.py` — Tambah Endpoint Live Price

**Tambahkan route baru untuk dashboard:**

```python
# Tambahkan endpoint ini agar dashboard bisa fetch harga live secara independen
@router.get("/api/gold-price")
async def get_live_gold_price():
    """
    Endpoint untuk dashboard: ambil harga gold live.
    Tidak bergantung pada signal log — selalu fetch fresh.
    """
    try:
        from .macro import get_gold_price, get_macro_context
        price = get_gold_price()
        return {
            "price": price,
            "symbol": "XAU/USD",
            "unit": "USD/troy oz",
            "timestamp": datetime.utcnow().isoformat(),
            "status": "live"
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
```

---

### 3. Dashboard UI (`ai_bridge/app/templates/` atau static JS)

**Fix fetch harga di frontend:**

```javascript
// ❌ SEBELUM (salah — mengambil dari signal log terakhir):
async function fetchGoldPrice() {
    const res = await fetch('/api/signals?limit=1');
    const data = await res.json();
    const price = data[0]?.price;  // ← Harga dari signal lama!
    document.getElementById('gold-price').textContent = `$${price}`;
}

// ✅ SESUDAH (benar — endpoint khusus live price):
async function fetchGoldPrice() {
    try {
        const res = await fetch('/api/gold-price');
        const data = await res.json();
        const price = parseFloat(data.price).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
        document.getElementById('gold-price').textContent = `$${price}`;
        document.getElementById('price-status').textContent = data.status;
    } catch (err) {
        document.getElementById('gold-price').textContent = 'N/A';
        console.error('Failed to fetch gold price:', err);
    }
}

// Refresh setiap 60 detik
fetchGoldPrice();
setInterval(fetchGoldPrice, 60_000);
```

---

### 4. `.env.example` — Tambah API Keys Baru

```env
# === Gold Price Providers (tambahkan minimal 2 untuk reliability) ===

# TwelveData (free: 800 req/day) — https://twelvedata.com
TWELVEDATA_API_KEY=your_key_here

# AlphaVantage (free: 25 req/day) — https://www.alphavantage.co
ALPHAVANTAGE_API_KEY=your_key_here

# Metals-API (free tier tersedia) — https://metals-api.com
METALS_API_KEY=your_key_here

# === Price Validation Bounds (sesuaikan jika harga gold berubah drastis) ===
GOLD_PRICE_MIN=3000
GOLD_PRICE_MAX=7000
```

---

## 📋 APA YANG PERLU DIPERBAIKI/DITAMBAH (Prioritas)

### 🔴 CRITICAL (Fix Sekarang)
| # | Masalah | File | Fix |
|---|---------|------|-----|
| 1 | Harga gold salah ($2000 vs $4688) | `macro.py` | Tambah validasi harga + multi-provider dengan range check |
| 2 | Dashboard ambil harga dari signal log lama | `routes.py` + UI JS | Buat endpoint `/api/gold-price` tersendiri |
| 3 | yfinance bisa return expired futures contract | `macro.py` | Gunakan `XAUUSD=X` sebagai primary ticker, validasi range |

### 🟡 IMPORTANT (Segera)
| # | Masalah | File | Fix |
|---|---------|------|-----|
| 4 | Tidak ada logging saat provider fallback terjadi | `macro.py` | Tambah logger.warning untuk setiap provider gagal |
| 5 | Backtesting menggunakan `GC=F` (futures) bukan spot | `backtest.py` | Ganti ke `XAUUSD=X` atau tambah flag `--use-spot` |
| 6 | LLM prompt hardcode harga range lama | `engine/prompts.py` | Update prompt agar aware harga gold sekarang ~$4500-$5000+ |
| 7 | Safety guard `MaxATR` → nilai 12 terlalu kecil untuk harga $4700 | `guards/` | Rescale ke ~$25-35 ATR (proporsional dengan harga baru) |

### 🟢 NICE TO HAVE (Improvement)
| # | Fitur | Deskripsi |
|---|-------|-----------|
| 8 | Dashboard: tambah price chart mini | Tampilkan chart harga gold 24h di UI |
| 9 | Dashboard: alert jika provider fallback terjadi | Badge merah jika harga dari secondary provider |
| 10 | Webhook payload: tambah validasi harga masuk | Tolak signal jika `price` di payload tidak masuk range valid |
| 11 | Tambah unit test untuk `get_gold_price()` | Mock semua provider, test fallback chain |
| 12 | Pine Script: update label harga di dashboard TradingView | Sudah otomatis dari TradingView, tapi pastikan pair yang dipilih benar |
| 13 | README: tambah troubleshooting section | Tambah FAQ "kenapa harga salah" |
| 14 | Docker health check: validasi harga gold | `docker-compose.yml` → healthcheck hit `/api/gold-price` |

---

## 🚦 Kenapa ATR Guard Perlu Di-rescale

Dari README, `MaxATRGuard` memblock signal jika `ATR > 12`. Ini ditulis saat harga gold ~$2000. Sekarang harga ~$4700, ATR H1 normal adalah **$15-35**. Nilai `12` akan **memblock hampir semua signal valid!**

```python
# ❌ LAMA (calibrated untuk harga $2000):
MAX_ATR = float(os.getenv("MAX_ATR_BLOCK", "12"))

# ✅ BARU (calibrated untuk harga $4500-5000):
# ATR normal H1 gold di harga $4700 = ~$15-35
# Block hanya jika extreme volatility (flash news/liquidity event)
MAX_ATR = float(os.getenv("MAX_ATR_BLOCK", "50"))
# Atau gunakan ATR_AS_PCT_OF_PRICE untuk auto-scaling:
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT_BLOCK", "0.8"))  # 0.8% dari harga
# Jika ATR > 0.8% × 4700 = $37.6 → block
```

---

## ✅ Quick Fix (Langkah Minimal)

Jika ingin fix cepat tanpa refactor besar:

1. **Edit `macro.py`** → tambahkan validasi range setelah fetch:
   ```python
   price = float(hist["Close"].iloc[-1])
   if not (3000 <= price <= 7000):
       raise ValueError(f"Harga gold tidak valid: ${price} — mungkin data stale/expired")
   ```

2. **Di `.env`** → set:
   ```
   MAX_ATR_BLOCK=50
   TWELVEDATA_API_KEY=<daftar gratis di twelvedata.com>
   ```

3. **Restart bridge** → `docker compose restart` atau `systemctl restart smart-gold`

---

*Analisis ini dibuat berdasarkan README, kode yang terlihat di repo, dan harga gold aktual hari ini ($4,688/oz per Investing.com, 14 Mei 2026).*
