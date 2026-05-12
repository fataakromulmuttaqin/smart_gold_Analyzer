"""Async MiniMax chat-completion client (OpenAI-compatible schema).

MiniMax exposes an OpenAI-compatible endpoint at
``{base_url}/chat/completions`` with ``Authorization: Bearer <api_key>``.
Response shape is identical to OpenAI's Chat Completions API:
``choices[0].message.content``.

When ``LLM_MOCK_MODE=true`` is set, the client bypasses the network and
returns a deterministic canned response so the full pipeline (webhook →
engine → notifier) can be smoke-tested without spending tokens.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config.settings import Settings, get_settings
from app.utils.logging import logger


class MiniMaxError(Exception):
    """Raised when MiniMax returns a non-2xx response or unparseable body."""


@dataclass(slots=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class ChatResponse:
    content: str
    model: str
    raw: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        """Parse ``content`` as JSON. Raises MiniMaxError on failure."""
        try:
            # Strip common markdown fences the model sometimes emits.
            text = self.content.strip()
            if text.startswith("```"):
                text = text.strip("`")
                # remove leading "json\n" if present
                if text.lower().startswith("json"):
                    text = text[4:].lstrip()
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MiniMaxError(f"LLM returned non-JSON content: {exc}") from exc


# ────────────────────────────────────────────────────────────────────────
# Mock canned response (used when LLM_MOCK_MODE=true)
# ────────────────────────────────────────────────────────────────────────
_MOCK_DECISION = {
    "action": "skip",
    "confidence": 0.45,
    "reasoning": (
        "MOCK MODE: real LLM not called. Returning a conservative 'skip' "
        "so downstream wiring can be verified end-to-end."
    ),
    "risk_notes": "Mock response — do not execute in production.",
}


class MiniMaxClient:
    """Thin async wrapper over httpx.AsyncClient for MiniMax chat completions."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> MiniMaxClient:
        self._client = httpx.AsyncClient(
            base_url=self.settings.minimax_base_url,
            timeout=self.settings.minimax_timeout,
            headers={
                "Authorization": f"Bearer {self.settings.minimax_api_key}",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResponse:
        """Send a chat completion request. Returns ChatResponse.

        Args:
            messages: conversation in OpenAI format.
            temperature: override default (settings.minimax_temperature).
            max_tokens: override default (settings.minimax_max_tokens).
            json_mode: request structured JSON output (adds response_format).
        """
        # Mock short-circuit
        if self.settings.llm_mock_mode:
            logger.warning("LLM_MOCK_MODE=true — returning canned response")
            return ChatResponse(
                content=json.dumps(_MOCK_DECISION),
                model=f"{self.settings.minimax_model} (mock)",
                raw={"mock": True},
            )

        if not self.settings.minimax_api_key:
            raise MiniMaxError(
                "MINIMAX_API_KEY is empty and LLM_MOCK_MODE is false — "
                "cannot call MiniMax API."
            )
        if self._client is None:
            raise RuntimeError(
                "MiniMaxClient must be used as async context manager "
                "(async with MiniMaxClient() as llm: ...)"
            )

        payload: dict[str, Any] = {
            "model": self.settings.minimax_model,
            "messages": [m.to_dict() for m in messages],
            "temperature": (
                self.settings.minimax_temperature if temperature is None else temperature
            ),
            "max_tokens": (
                self.settings.minimax_max_tokens if max_tokens is None else max_tokens
            ),
        }
        if json_mode:
            # OpenAI-compatible structured output flag — MiniMax supports it.
            payload["response_format"] = {"type": "json_object"}

        logger.debug(
            "MiniMax request: model={} messages={} json_mode={}",
            payload["model"],
            len(messages),
            json_mode,
        )

        try:
            resp = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise MiniMaxError(f"HTTP error calling MiniMax: {exc}") from exc

        if resp.status_code >= 400:
            raise MiniMaxError(
                f"MiniMax HTTP {resp.status_code}: {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise MiniMaxError(f"MiniMax returned non-JSON body: {exc}") from exc

        # OpenAI-compatible response shape
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MiniMaxError(
                f"Unexpected MiniMax response shape: {json.dumps(data)[:300]}"
            ) from exc

        return ChatResponse(
            content=content,
            model=data.get("model", self.settings.minimax_model),
            raw=data,
        )


__all__ = ["MiniMaxClient", "ChatMessage", "ChatResponse", "MiniMaxError"]
