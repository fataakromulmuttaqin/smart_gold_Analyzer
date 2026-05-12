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

    print("\nALL OFFLINE VALIDATIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
