"""Offline validation harness for sandbox / CI without network.

Stubs httpx / loguru / pydantic-settings just enough to exercise:
  1. module imports
  2. mock-mode short-circuit of MiniMaxClient.chat
  3. ChatResponse.as_json() markdown-fence stripping

Not a production entrypoint. Real tests live in tests/ and require the
packages from requirements.txt to be installed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path


def _stub_httpx() -> None:
    mod = types.ModuleType("httpx")

    class AsyncClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def aclose(self) -> None:
            pass

        async def post(self, *a: object, **kw: object):  # pragma: no cover
            raise RuntimeError("network call attempted under mock mode")

    class HTTPError(Exception):
        pass

    mod.AsyncClient = AsyncClient  # type: ignore[attr-defined]
    mod.HTTPError = HTTPError  # type: ignore[attr-defined]
    sys.modules["httpx"] = mod


def _stub_loguru() -> None:
    mod = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, name: str):
            def _(*a: object, **kw: object) -> None:
                return None
            return _

    mod.logger = _Logger()  # type: ignore[attr-defined]
    sys.modules["loguru"] = mod


def _stub_pydantic() -> None:
    pyd = types.ModuleType("pydantic")

    def Field(default=None, alias=None, default_factory=None, ge=None, le=None, **kw):
        if default_factory is not None:
            return default_factory()
        return default

    def field_validator(*a, **kw):
        def deco(fn):
            return fn
        return deco

    def ConfigDict(**kw):
        return kw

    class BaseModel:
        def __init__(self, **data):
            for k, v in type(self).__dict__.items():
                if k.startswith("_") or callable(v) or isinstance(v, (classmethod, staticmethod, property)):
                    continue
                setattr(self, k, data.get(k, v))
            # also pick up annotations defaults supplied in subclass
            for k in getattr(type(self), "__annotations__", {}):
                if k in data:
                    setattr(self, k, data[k])
                elif not hasattr(self, k):
                    setattr(self, k, None)

        def model_dump(self):
            return {
                k: v
                for k, v in self.__dict__.items()
                if not k.startswith("_")
            }

    pyd.Field = Field  # type: ignore[attr-defined]
    pyd.field_validator = field_validator  # type: ignore[attr-defined]
    pyd.ConfigDict = ConfigDict  # type: ignore[attr-defined]
    pyd.BaseModel = BaseModel  # type: ignore[attr-defined]
    sys.modules["pydantic"] = pyd

    pys = types.ModuleType("pydantic_settings")

    class BaseSettings:
        def __init__(self, **_kw):
            # Walk MRO so subclass + base attrs are both covered; skip
            # descriptors (property, classmethod, staticmethod) and dunders.
            seen: set[str] = set()
            for klass in type(self).__mro__:
                for attr, val in klass.__dict__.items():
                    if attr in seen or attr.startswith("_"):
                        continue
                    if isinstance(val, (property, classmethod, staticmethod)):
                        continue
                    if callable(val):
                        continue
                    seen.add(attr)
                    env_val = os.environ.get(attr.upper())
                    if env_val is not None:
                        if isinstance(val, bool):
                            val = env_val.lower() in ("1", "true", "yes")
                        elif isinstance(val, int):
                            val = int(env_val)
                        elif isinstance(val, float):
                            val = float(env_val)
                        else:
                            val = env_val
                    setattr(self, attr, val)

    def SettingsConfigDict(**kw):
        return kw

    pys.BaseSettings = BaseSettings  # type: ignore[attr-defined]
    pys.SettingsConfigDict = SettingsConfigDict  # type: ignore[attr-defined]
    sys.modules["pydantic_settings"] = pys


def main() -> int:
    _stub_httpx()
    _stub_loguru()
    _stub_pydantic()

    sys.path.insert(
        0,
        str(Path(__file__).resolve().parents[1]),
    )

    os.environ["LLM_MOCK_MODE"] = "true"
    os.environ["MINIMAX_API_KEY"] = ""

    from app.config import settings as settings_mod  # noqa: E402
    from app.llm.minimax_client import (  # noqa: E402
        ChatMessage,
        ChatResponse,
        MiniMaxClient,
    )

    s = settings_mod.Settings()
    print(f"[config] llm_mock_mode   = {s.llm_mock_mode}")
    print(f"[config] minimax_base_url = {s.minimax_base_url}")
    print(f"[config] minimax_model    = {s.minimax_model}")
    assert s.llm_mock_mode is True, "mock mode env was not picked up"

    # Markdown fence stripping
    fenced = '```json\n{"action": "skip", "confidence": 0.1}\n```'
    cr = ChatResponse(content=fenced, model="test", raw={})
    assert cr.as_json() == {"action": "skip", "confidence": 0.1}
    print("[ok]   as_json strips ```json fences")

    # Mock mode end-to-end
    async def run() -> None:
        client = MiniMaxClient(settings=s)
        async with client as llm:
            resp = await llm.chat(
                [ChatMessage(role="user", content="hello")],
                json_mode=True,
            )
        data = resp.as_json()
        assert data["action"] in ("execute", "skip", "reduce"), data
        assert resp.raw.get("mock") is True
        print(f"[ok]   mock chat() returns canned decision: {data}")

    asyncio.run(run())

    # ── Macro context fallback (no yfinance, no NewsAPI key) ───────────
    # yfinance import should fail gracefully; NewsAPI key is empty → []
    os.environ["ENABLE_MACRO_CONTEXT"] = "true"
    os.environ["NEWSAPI_KEY"] = ""

    from app.context.market_context import fetch_macro_context  # noqa: E402

    async def run_ctx() -> None:
        # fresh settings to reflect env
        s2 = settings_mod.Settings()
        ctx = await fetch_macro_context(settings=s2)
        assert ctx.partial is True, "expected partial=True when DXY unavailable"
        assert ctx.dxy_price is None
        assert ctx.us10y_yield is None
        assert ctx.news_headlines == []
        print(f"[ok]   macro context gracefully degrades: partial={ctx.partial} notes={ctx.notes}")

    asyncio.run(run_ctx())

    # ── Decision engine end-to-end (mock LLM) ───────────────────────────
    from app.engine.decision_engine import evaluate_signal  # noqa: E402
    from app.models.schemas import MacroContext, TradingViewAlert  # noqa: E402

    async def run_engine() -> None:
        # Reuse mock LLM mode
        os.environ["LLM_MOCK_MODE"] = "true"
        os.environ["MIN_CONFIDENCE"] = "0.60"

        s3 = settings_mod.Settings()
        alert = TradingViewAlert(
            secret="x",
            symbol="OANDA:XAUUSD",
            timeframe="60",
            signal="strong_long",
            price=2345.67,
        )
        ctx = MacroContext(
            dxy_price=104.2,
            dxy_change_pct=-0.15,
            us10y_yield=4.12,
            news_headlines=["[Reuters] Gold holds steady as dollar slips"],
        )
        decision = await evaluate_signal(alert, ctx, settings=s3)
        # Mock returns action=skip confidence=0.45 → policy keeps as skip
        assert decision.action == "skip"
        assert 0.0 <= decision.confidence <= 1.0
        print(f"[ok]   engine returns validated decision: action={decision.action} conf={decision.confidence}")

        # Test policy: even if LLM said execute with low conf, policy → skip.
        # Patch the _parse_and_validate path via direct call:
        from app.engine.decision_engine import _apply_policy, _parse_and_validate
        raw = {"action": "execute", "confidence": 0.5, "reasoning": "r"}
        d = _parse_and_validate(raw)
        d2 = _apply_policy(d, s3)
        assert d2.action == "skip", f"policy should downgrade: {d2.action}"
        assert "Policy override" in d2.risk_notes
        print("[ok]   policy downgrades low-confidence execute → skip")

        # Test: invalid action coerced
        bad = _parse_and_validate({"action": "buy_now", "confidence": 0.9, "reasoning": ""})
        assert bad.action == "skip"
        print("[ok]   invalid action coerced to skip")

    asyncio.run(run_engine())

    # ── Telegram formatter (no network, just format) ────────────────────
    from app.models.schemas import LLMDecision as _LLMDecision  # noqa: E402
    from app.notifier.telegram import _format_message  # noqa: E402

    a = TradingViewAlert(
        secret="x", symbol="OANDA:XAUUSD", timeframe="60",
        signal="strong_long", price=2345.67,
    )
    c = MacroContext(
        dxy_price=104.2, dxy_change_pct=-0.15,
        us10y_yield=4.12, us10y_change_bp=-3.0,
        news_headlines=["[Reuters] Gold steady as dollar slips"],
    )
    d = _LLMDecision(
        action="execute", confidence=0.78,
        reasoning="Bullish structure with weakening DXY and softer yields.",
        risk_notes="Wait for retest of OB.",
        suggested_rr=2.0, suggested_stop_atr_mult=1.5,
    )
    msg = _format_message(a, c, d)
    assert "EXECUTE" in msg and "104.2" in msg and "0.78" in msg
    assert "1:2" in msg and "1.5×ATR" in msg
    print("[ok]   telegram message formatter contains all fields")

    # ── SQLite SignalLog round-trip ─────────────────────────────────────
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["SQLITE_PATH"] = tmp.name

    async def run_storage() -> None:
        s4 = settings_mod.Settings()
        from app.storage.signal_log import SignalLog
        sl = SignalLog(settings=s4)
        sid = await sl.record(a, c, d, notified=True)
        assert isinstance(sid, int) and sid >= 1
        # Verify row exists
        import sqlite3
        with sqlite3.connect(tmp.name) as conn:
            row = conn.execute(
                "SELECT symbol, signal, decision_action, notified FROM signals WHERE id=?",
                (sid,),
            ).fetchone()
        assert row == ("OANDA:XAUUSD", "strong_long", "execute", 1), row
        print(f"[ok]   SignalLog round-trip works: id={sid}")

    asyncio.run(run_storage())
    os.unlink(tmp.name)

    # ── Dashboard read-side (list / stats / get_by_id) ──────────────────
    tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp2.close()
    os.environ["SQLITE_PATH"] = tmp2.name

    async def run_dashboard() -> None:
        s5 = settings_mod.Settings()
        from app.storage.signal_log import SignalLog

        sl = SignalLog(settings=s5)
        # Insert 3 signals with varied actions so the stats aggregation has
        # something interesting to reduce over.
        variants = [
            ("execute", 0.82, True),
            ("skip",    0.42, False),
            ("reduce",  0.66, True),
        ]
        for action, conf, notified in variants:
            decision = _LLMDecision(
                action=action, confidence=conf,
                reasoning=f"{action} reasoning",
                risk_notes="n/a",
            )
            await sl.record(a, c, decision, notified=notified)

        # list_recent returns newest-first, clamped to 500
        recent = await sl.list_recent(limit=10)
        assert len(recent) == 3
        assert recent[0]["id"] > recent[-1]["id"]
        # Filter by action
        execs = await sl.list_recent(action="execute")
        assert len(execs) == 1 and execs[0]["decision_action"] == "execute"
        # Filter by symbol that doesn't exist
        none_rows = await sl.list_recent(symbol="EURUSD")
        assert none_rows == []
        print(f"[ok]   dashboard list/filter: {len(recent)} rows, exec filter={len(execs)}")

        # Stats
        stats = await sl.stats(since_hours=24)
        assert stats["total"] == 3
        assert stats["by_action"]["execute"] == 1
        assert stats["by_action"]["skip"] == 1
        assert stats["by_action"]["reduce"] == 1
        assert stats["notified"] == 2  # execute + reduce
        assert stats["avg_confidence"] is not None
        assert 0.4 < stats["avg_confidence"] < 0.8
        print(f"[ok]   dashboard stats aggregation: {stats}")

        # Detail
        detail = await sl.get_by_id(recent[0]["id"])
        assert detail is not None
        assert "alert" in detail and "context" in detail and "decision" in detail
        assert isinstance(detail["alert"], dict)
        print(f"[ok]   dashboard get_by_id expands JSON columns: keys={sorted(detail['alert'].keys())[:3]}…")

        # Not-found path
        missing = await sl.get_by_id(999999)
        assert missing is None
        print("[ok]   dashboard get_by_id returns None for missing id")

    asyncio.run(run_dashboard())
    os.unlink(tmp2.name)

    # ── Webhook payload round-trip (Pine Script JSON → TradingViewAlert) ─
    # This is the exact payload documented in pinescript/ALERT_PAYLOAD.md
    sample_payload = {
        "secret": "abc123xyz",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "strong_long",
        "price": 2345.67,
        "time": "2026-05-12T18:00:00Z",
        "ms_state": "bullish",
        "rsi": 52.34,
        "atr": 3.21,
        "money_flow": 67.5,
        "ema_fast": 2340.5,
        "ema_slow": 2332.1,
        "ema_base": 2290.7,
    }
    # With our pydantic stub, just construct & verify field access.
    parsed = TradingViewAlert(**sample_payload)
    assert parsed.secret == "abc123xyz"
    assert parsed.symbol == "XAUUSD"
    assert parsed.signal == "strong_long"
    assert parsed.price == 2345.67
    assert parsed.ms_state == "bullish"
    assert parsed.rsi == 52.34
    print("[ok]   Pine Script sample payload matches TradingViewAlert schema")

    # ── Executor (noop + factory fallback when MT5 absent) ──────────────
    from app.executor.base import ExecutionResult, NoopExecutor  # noqa: E402
    from app.executor.factory import build_executor  # noqa: E402

    async def run_executor() -> None:
        # Noop always returns placed=False
        alert_exec = TradingViewAlert(
            secret="x", symbol="XAUUSD", timeframe="60",
            signal="strong_long", price=2345.67,
        )
        d_exec = _LLMDecision(
            action="execute", confidence=0.82, reasoning="ok", risk_notes="",
        )
        noop = NoopExecutor()
        res = await noop.execute(alert_exec, d_exec)
        assert isinstance(res, ExecutionResult)
        assert res.placed is False and res.note.startswith("executor=noop")
        # to_dict has full shape
        d = res.to_dict()
        assert "placed" in d and "order_id" in d and "side" in d
        print(f"[ok]   NoopExecutor returns placed=False with note: {res.note[:60]}…")

        # Factory: with MT5_ENABLED=false → returns NoopExecutor
        os.environ["MT5_ENABLED"] = "false"
        s_ex = settings_mod.Settings()
        ex = build_executor(settings=s_ex)
        assert ex.name == "noop"
        print("[ok]   factory → noop when MT5_ENABLED=false")

        # Factory: with MT5_ENABLED=true but MetaTrader5 package absent on Linux,
        # factory catches ImportError and falls back to noop gracefully.
        os.environ["MT5_ENABLED"] = "true"
        os.environ["MT5_LOGIN"] = "12345"
        os.environ["MT5_PASSWORD"] = "x"
        os.environ["MT5_SERVER"] = "demo"
        s_ex2 = settings_mod.Settings()
        ex2 = build_executor(settings=s_ex2)
        # Either it falls back to noop (no MetaTrader5 package), OR it imports
        # but fails to init (also falls back to noop). Both are acceptable.
        assert ex2.name == "noop", f"expected noop fallback, got {ex2.name}"
        print("[ok]   factory → noop fallback when MT5 enabled but package absent")

        # Reset for later tests
        os.environ["MT5_ENABLED"] = "false"

    asyncio.run(run_executor())

    # ── Winrate aggregation ─────────────────────────────────────────────
    tmp3 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp3.close()
    os.environ["SQLITE_PATH"] = tmp3.name

    async def run_winrate() -> None:
        s_wr = settings_mod.Settings()
        from app.storage.signal_log import SignalLog

        sl = SignalLog(settings=s_wr)
        # Seed 5 signals: 3 strong_long (2w/1l), 2 strong_short (1w/1be)
        rows = [
            ("strong_long",  "win",       50.0),
            ("strong_long",  "win",       30.0),
            ("strong_long",  "loss",     -20.0),
            ("strong_short", "win",       40.0),
            ("strong_short", "breakeven", 0.0),
        ]
        for sig, outcome, pnl in rows:
            decision = _LLMDecision(
                action="execute", confidence=0.75,
                reasoning=f"{sig} r", risk_notes="",
            )
            alert_wr = TradingViewAlert(
                secret="x", symbol="XAUUSD", timeframe="60",
                signal=sig, price=2345.0,
            )
            sid = await sl.record(alert_wr, c, decision, notified=True)
            ok = await sl.update_outcome(sid, outcome, pnl)
            assert ok, f"update_outcome failed for {sid}"

        wr = await sl.winrate_by_signal(since_hours=24)
        assert wr["overall"]["closed"] == 5
        assert wr["overall"]["wins"] == 3
        assert wr["overall"]["losses"] == 1
        assert wr["overall"]["breakevens"] == 1
        assert abs(wr["overall"]["total_pnl"] - 100.0) < 1e-6
        # overall win_rate = wins / (wins+losses) = 3/4 = 0.75
        assert abs(wr["overall"]["win_rate"] - 0.75) < 1e-6

        by_sig = {r["signal"]: r for r in wr["by_signal"]}
        assert "strong_long" in by_sig and "strong_short" in by_sig
        sl_row = by_sig["strong_long"]
        assert sl_row["wins"] == 2 and sl_row["losses"] == 1
        # 2/(2+1) ≈ 0.6667
        assert abs(sl_row["win_rate"] - 2/3) < 1e-3
        ss_row = by_sig["strong_short"]
        # 1 win, 0 loss, 1 breakeven → win_rate = 1/(1+0) = 1.0 (BE excluded)
        assert ss_row["wins"] == 1 and ss_row["losses"] == 0
        assert ss_row["breakevens"] == 1
        assert ss_row["win_rate"] == 1.0
        print(
            f"[ok]   winrate aggregation: overall {wr['overall']['wins']}W "
            f"/ {wr['overall']['losses']}L (win_rate={wr['overall']['win_rate']}), "
            f"by_signal={len(wr['by_signal'])} rows"
        )

        # Invalid outcome should raise ValueError
        try:
            await sl.update_outcome(1, "foo")  # type: ignore[arg-type]
        except ValueError:
            print("[ok]   update_outcome rejects invalid outcome")
        else:
            raise AssertionError("expected ValueError for invalid outcome")

    asyncio.run(run_winrate())
    os.unlink(tmp3.name)

    print("\nALL OFFLINE VALIDATIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
