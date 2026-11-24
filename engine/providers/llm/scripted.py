"""Deterministic scripted LLM used by tests and by CI (no model required).

It is a *test double*: it is never offered as a selectable provider in the UI and it
never pretends to be a real model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from engine.providers.base import ProviderHealth
from engine.providers.llm.base import (
    ChatMessage,
    LLMChunk,
    LLMError,
    LLMOptions,
    ModelInfo,
    ToolCall,
)

Responder = Callable[[list[ChatMessage]], "str | tuple[str, list[ToolCall]]"]


class ScriptedLLM:
    name = "scripted"

    def __init__(
        self,
        responder: Responder | None = None,
        *,
        delay_s: float = 0.0,
        fail_with: LLMError | None = None,
        json_result: dict[str, Any] | None = None,
    ) -> None:
        self.responder = responder or (lambda msgs: f"You said: {msgs[-1].content}")
        self.delay_s = delay_s
        self.fail_with = fail_with
        self.json_result = json_result
        self.calls: list[list[ChatMessage]] = []

    async def health(self) -> ProviderHealth:
        if self.fail_with:
            return ProviderHealth(self.name, "unavailable", str(self.fail_with))
        return ProviderHealth(self.name, "ok", "scripted test provider")

    async def list_models(self, force: bool = False) -> list[ModelInfo]:
        return [ModelInfo(name="scripted", is_local=True)]

    async def stream_chat(
        self, messages: list[ChatMessage], options: LLMOptions, tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        if self.fail_with:
            raise self.fail_with
        result = self.responder(messages)
        text, calls = (result, []) if isinstance(result, str) else result
        if calls:
            yield LLMChunk(tool_calls=calls, metrics={"ttft_ms": 1.0})
            yield LLMChunk(done=True)
            return
        words = text.split(" ")
        for i, w in enumerate(words):
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield LLMChunk(text=w + (" " if i < len(words) - 1 else ""), metrics={"ttft_ms": 1.0} if i == 0 else {})
        yield LLMChunk(done=True)

    async def chat_json(
        self, messages: list[ChatMessage], options: LLMOptions, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(list(messages))
        if self.fail_with:
            raise self.fail_with
        if self.json_result is None:
            raise LLMError("no scripted JSON result", code="llm_bad_response")
        return dict(self.json_result)
