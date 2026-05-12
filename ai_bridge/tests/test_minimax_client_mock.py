"""Smoke test: MiniMax client in mock mode returns the canned JSON decision.

Runs without any MiniMax credentials. Requires runtime deps installed
(httpx, pydantic, loguru). Skipped automatically if deps unavailable.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("httpx")
pytest.importorskip("pydantic_settings")
pytest.importorskip("loguru")


@pytest.mark.asyncio
async def test_mock_mode_returns_canned_decision(monkeypatch):
    monkeypatch.setenv("LLM_MOCK_MODE", "true")
    monkeypatch.setenv("MINIMAX_API_KEY", "")

    from app.config.settings import get_settings
    from app.llm.minimax_client import ChatMessage, MiniMaxClient

    get_settings.cache_clear()

    async with MiniMaxClient() as llm:
        resp = await llm.chat(
            [
                ChatMessage(role="system", content="You are a gold analyst."),
                ChatMessage(role="user", content="Should I long XAU/USD?"),
            ],
            json_mode=True,
        )

    data = resp.as_json()
    assert set(data.keys()) >= {"action", "confidence", "reasoning", "risk_notes"}
    assert data["action"] in {"execute", "skip", "reduce"}
    assert 0.0 <= data["confidence"] <= 1.0
    # Raw should mark mock
    assert resp.raw.get("mock") is True


def test_as_json_strips_markdown_fences():
    from app.llm.minimax_client import ChatResponse

    fenced = '```json\n{"action": "skip", "confidence": 0.1}\n```'
    cr = ChatResponse(content=fenced, model="test", raw={})
    assert cr.as_json() == {"action": "skip", "confidence": 0.1}

    plain = json.dumps({"action": "execute", "confidence": 0.9})
    cr2 = ChatResponse(content=plain, model="test", raw={})
    assert cr2.as_json()["action"] == "execute"
