"""
guards_patch.py — CATATAN PERBAIKAN GUARDS
Fix: ATR guard dan parameter lain yang masih dikalibrasi untuk harga gold $2000

Semua nilai ini harus di-update di .env atau langsung di file guard masing-masing.
"""

# ─────────────────────────────────────────────────────────────────────────────
# MASALAH: MaxATRGuard
# ─────────────────────────────────────────────────────────────────────────────
# README asli: "MaxATR (>12 = extreme vol)"
# Masalah: ATR H1 normal untuk gold di harga $4700 adalah $15-35.
# Nilai 12 akan MEMBLOCK hampir semua signal valid di kondisi pasar sekarang!
#
# ATR sebagai % dari harga:
# - Di $2000: ATR normal $8-15 → threshold $12 = ~0.6% harga ✅ masuk akal
# - Di $4700: ATR normal $15-35 → threshold $12 = $12/$4700 = 0.26% ❌ terlalu ketat!
#
# FIX di .env:
#   MAX_ATR_BLOCK=50
#
# Atau untuk auto-scaling (lebih baik), tambahkan di guard:

import os

class MaxATRGuard:
    """
    Versi yang sudah diperbaiki dengan auto-scaling berdasarkan % harga.
    Block signal jika ATR > X% dari harga entry (default 0.8%).
    """
    
    def __init__(self):
        # Legacy: nilai absolut (backward compatible)
        self.max_atr_abs = float(os.getenv("MAX_ATR_BLOCK", "50"))  # ← Naik dari 12 ke 50
        
        # Baru: percentage-based (lebih robust saat harga berubah drastis)
        self.max_atr_pct = float(os.getenv("MAX_ATR_PCT_BLOCK", "0.8"))  # 0.8% dari harga
        self.use_pct_mode = os.getenv("MAX_ATR_USE_PCT", "true").lower() == "true"
    
    def check(self, signal: dict) -> dict:
        atr = signal.get("atr", 0)
        price = signal.get("price", 4700)  # fallback ke harga perkiraan
        
        if self.use_pct_mode:
            # Auto-scaling: 0.8% dari $4700 = $37.6
            threshold = price * (self.max_atr_pct / 100)
        else:
            threshold = self.max_atr_abs
        
        if atr > threshold:
            return {
                "action": "block",
                "reason": (
                    f"ATR terlalu tinggi: ${atr:.2f} > threshold ${threshold:.2f} "
                    f"({'%.1f%%' % (atr/price*100)} dari harga). "
                    f"Extreme volatility — skip signal."
                )
            }
        return {"action": "pass"}


# ─────────────────────────────────────────────────────────────────────────────
# MASALAH: SpreadGuard
# ─────────────────────────────────────────────────────────────────────────────
# README asli: "Spread (>50 pts = block) — Skip when broker spread abnormal"
# 
# Di harga $2000: spread 50 pts = $0.50 = 0.025% harga → wajar
# Di harga $4700: spread 50 pts = $0.50 = 0.011% harga → TERLALU PERMISIF
# 
# Di harga $4700, spread normal XAU/USD adalah 30-60 pts.
# Spread abnormal biasanya > 150-200 pts.
#
# FIX di .env:
#   SPREAD_MAX_POINTS=150


# ─────────────────────────────────────────────────────────────────────────────  
# MASALAH: LLM Prompt Context
# ─────────────────────────────────────────────────────────────────────────────
# System prompt di engine/prompts.py kemungkinan masih menyebut harga reference
# gold di range $1800-$2500. Ini membuat LLM memberikan keputusan yang tidak
# akurat karena context macro yang salah.
#
# FIX: Update system prompt di engine/prompts.py
#
# Cari baris seperti:
#   "Gold is currently trading around $2000-$2500..."
#   "typical XAU/USD price range of $1800-$2500..."
#
# Ganti dengan:
#   "Gold is currently trading at approximately $4500-$5000 per troy ounce (2026)..."
#   "The ATR for H1 gold at current prices is typically $15-35..."
#
# Contoh system prompt yang diupdate:

SYSTEM_PROMPT_GOLD_CONTEXT = """
You are a gold (XAU/USD) trading assistant for the SmartGold Analyzer system.

CURRENT MARKET CONTEXT (2026):
- Gold (XAU/USD) is currently trading at approximately $4,500–$5,000 per troy ounce
- This represents a significant increase from the ~$2,000 range in 2023-2024
- Normal H1 ATR at current prices: $15–35 (was $8–15 at $2000 price level)
- Normal spread: 30–80 points
- 1 standard lot = $100 per $1 move in gold (unchanged)

When evaluating signals, consider:
- Position sizing is risk-based, not lot-based — unchanged
- ATR values of $20-30 are NORMAL at $4700 gold (not extreme volatility)
- Stop distances of $30-60 (hybrid ATR policy) are normal and appropriate
- Macro factors: DXY correlation, US10Y yields, Iran conflict ongoing (2026)
"""


# ─────────────────────────────────────────────────────────────────────────────
# REKOMENDASI .env LENGKAP (tambahkan/update ini):
# ─────────────────────────────────────────────────────────────────────────────

RECOMMENDED_ENV_UPDATES = """
# === GOLD PRICE PROVIDERS (tambahkan untuk reliability) ===
TWELVEDATA_API_KEY=your_key_here      # Free: https://twelvedata.com
ALPHAVANTAGE_API_KEY=your_key_here    # Free: https://www.alphavantage.co
METALS_API_KEY=your_key_here          # Free: https://metals-api.com

# === PRICE VALIDATION BOUNDS ===
GOLD_PRICE_MIN=3000    # Harga gold tidak mungkin < $3000 di 2026
GOLD_PRICE_MAX=7000    # Harga gold tidak mungkin > $7000 di 2026

# === GUARDS — RESCALED UNTUK HARGA $4700 ===
MAX_ATR_BLOCK=50              # Naik dari 12 → ATR normal di $4700 adalah $15-35
MAX_ATR_USE_PCT=true          # Gunakan % mode (auto-scaling)  
MAX_ATR_PCT_BLOCK=0.8         # Block jika ATR > 0.8% dari harga entry
SPREAD_MAX_POINTS=150         # Naik dari 50 → spread normal $4700 gold = 30-80 pts
"""
