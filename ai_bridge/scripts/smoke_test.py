"""End-to-end smoke test for a running AI Bridge instance.

Posts a sample TradingView-shaped payload to /webhook/tradingview and
asserts that the response looks correct. Run this after ``docker compose
up`` to verify your deployment.

Usage:
    python scripts/smoke_test.py [--url URL] [--secret SECRET]

    # With env vars already set in .env:
    python scripts/smoke_test.py

    # Against a remote VPS:
    python scripts/smoke_test.py --url https://sga.example.com/webhook/tradingview \
                                 --secret $WEBHOOK_SECRET
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_dotenv() -> None:
    """Tiny .env loader — no dependency on python-dotenv.

    Looks for ai_bridge/.env relative to this script.
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_sample_payload(secret: str) -> dict:
    """Match the schema documented in pinescript/ALERT_PAYLOAD.md."""
    return {
        "secret": secret,
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "strong_long",
        "price": 2345.67,
        "time": "2026-05-12T18:00:00Z",
        "ms_state": "bullish",
        "rsi": 52.3,
        "atr": 3.21,
        "money_flow": 67.5,
        "ema_fast": 2340.5,
        "ema_slow": 2332.1,
        "ema_base": 2290.7,
    }


def post_json(url: str, payload: dict, timeout: int = 30) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "sga-smoke-test/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"detail": raw}
        return e.code, data


def get_json(url: str, timeout: int = 10) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"detail": e.read().decode("utf-8", errors="replace")}


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="AI Bridge smoke test")
    parser.add_argument(
        "--url",
        default=os.environ.get("SMOKE_URL", "http://127.0.0.1:8080"),
        help="Base URL (default: http://127.0.0.1:8080 or $SMOKE_URL)",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("WEBHOOK_SECRET", ""),
        help="Webhook secret (default: $WEBHOOK_SECRET from .env)",
    )
    parser.add_argument(
        "--signal",
        default="strong_long",
        help="Signal name to send",
    )
    args = parser.parse_args()

    if not args.secret:
        print("[ERR] WEBHOOK_SECRET not provided via --secret or .env", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    print(f"[smoke] target base URL: {base}")

    # ── 1. /health ─────────────────────────────────────────────────────
    status, body = get_json(f"{base}/health")
    print(f"[smoke] GET /health → {status}")
    if status != 200:
        print(f"[FAIL] /health returned {status}: {body}", file=sys.stderr)
        return 1
    print(f"[ok  ] health: mock={body.get('llm_mock_mode')} "
          f"model={body.get('model')} min_conf={body.get('min_confidence')}")

    # ── 2. POST webhook ────────────────────────────────────────────────
    payload = build_sample_payload(args.secret)
    payload["signal"] = args.signal
    t0 = time.time()
    status, body = post_json(f"{base}/webhook/tradingview", payload)
    dt = time.time() - t0
    print(f"[smoke] POST /webhook/tradingview → {status} in {dt:.2f}s")
    if status == 429:
        print("[warn] cooldown active — retry after SIGNAL_COOLDOWN_SECONDS")
        return 0
    if status != 200:
        print(f"[FAIL] webhook returned {status}: {body}", file=sys.stderr)
        return 1

    # ── 3. Validate response shape ─────────────────────────────────────
    required = {"accepted", "alert", "context", "decision"}
    missing = required - set(body)
    if missing:
        print(f"[FAIL] response missing keys: {missing}", file=sys.stderr)
        return 1
    d = body["decision"]
    print(f"[ok  ] decision: action={d.get('action')} "
          f"confidence={d.get('confidence'):.2f} "
          f"notifier_sent={body.get('notifier_sent')} "
          f"signal_id={body.get('signal_id')}")
    print(f"[ok  ] reasoning: {d.get('reasoning', '')[:120]}…")

    # ── 4. Bad secret → 401 ────────────────────────────────────────────
    payload["secret"] = "wrong"
    status, _ = post_json(f"{base}/webhook/tradingview", payload)
    if status not in (401, 429):  # 429 if same-key cooldown already fires
        print(f"[FAIL] bad-secret test: expected 401, got {status}", file=sys.stderr)
        return 1
    print(f"[ok  ] auth: bad secret rejected with {status}")

    print("\n[SMOKE TEST PASSED]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
