"""Claude (Anthropic API) adapter. Opt-in: conversation text leaves this machine.

Only used for models named ``claude-*`` and only when remote models are allowed
(``VA_ALLOW_REMOTE_LLM_MODELS``) and ``ANTHROPIC_API_KEY`` is set.

Thinking blocks: Claude's response for a tool-calling turn is replayed unchanged within
that turn (required); earlier, finished turns are sent as plain text, which only trims
thinking from the front of the history (allowed).
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from engine.providers.base import ProviderHealth
from engine.providers.llm.base import (
    ChatMessage,
    LLMChunk,
    LLMError,
    LLMOptions,
    ModelInfo,
    ToolCall,
)

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    name = "claude"

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(timeout=60.0, max_retries=2)
        self._raw: OrderedDict[str, list[Any]] = OrderedDict()  # tool_use id -> Claude's full content

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.name, "ok", "Anthropic API key configured", is_local=False)

    async def list_models(self, force: bool = False) -> list[ModelInfo]:
        return [ModelInfo(name=m, family="claude", is_local=False, remote_host="api.anthropic.com",
                          capabilities=["tools"]) for m in MODELS]  # fmt: skip

    def _request(self, messages: list[ChatMessage], options: LLMOptions) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
        return {
            "model": options.model,
            # thinking shares this cap with the reply; the spoken length is set by the prompt
            "max_tokens": max(options.max_tokens, 4096),
            "system": system,
            "messages": self._convert([m for m in messages if m.role != "system"]),
            "output_config": {"effort": "low"},  # voice: lowest latency
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",  # a declined request is re-run on a fallback model
        }

    def _convert(self, history: list[ChatMessage]) -> list[dict[str, Any]]:
        last_user = max((i for i, m in enumerate(history) if m.role == "user"), default=-1)
        out: list[dict[str, Any]] = []
        pending: list[ToolCall] = []
        for i, m in enumerate(history):
            if m.role == "user":
                out.append({"role": "user", "content": m.content or "..."})
            elif m.role == "assistant":
                ids = [c.id or f"toolu_va{i}_{n}" for n, c in enumerate(m.tool_calls)]
                pending = [ToolCall(c.name, c.arguments, ids[n]) for n, c in enumerate(m.tool_calls)]
                raw = self._raw.get(ids[0]) if ids and i > last_user else None
                if raw is not None:
                    out.append({"role": "assistant", "content": raw})
                    continue
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content.strip() else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in pending]
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif m.role == "tool":
                call = next((c for c in pending if c.name == m.tool_name), pending[0] if pending else None)
                if call is None:
                    continue
                pending.remove(call)
                result = {"type": "tool_result", "tool_use_id": call.id, "content": m.content}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(result)  # all results of one turn go in one message
                else:
                    out.append({"role": "user", "content": [result]})
        if not out or out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": "(The call has connected.)"})
        if out[-1]["role"] == "assistant":  # assistant prefill is rejected
            out.append({"role": "user", "content": "(The caller is silent.)"})
        return out

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: LLMOptions,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        req = self._request(messages, options)
        if tools:
            req["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                             "input_schema": t["function"]["parameters"]} for t in tools]  # fmt: skip
        t0 = time.perf_counter()
        first = True
        try:
            async with self.client.beta.messages.stream(**req) as stream:
                async for event in stream:
                    if event.type == "text" and event.text:
                        chunk = LLMChunk(text=event.text)
                        if first:
                            chunk.metrics["ttft_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                            first = False
                        yield chunk
                final = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key (ANTHROPIC_API_KEY).", code="llm_error") from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise LLMError(f"Claude unavailable: {exc}", code="llm_unavailable", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}", code="llm_error") from exc
        if final.stop_reason == "refusal":
            raise LLMError("Claude declined to answer this turn.", code="llm_refused")
        calls = [ToolCall(b.name, b.input if isinstance(b.input, dict) else {}, b.id)
                 for b in final.content if b.type == "tool_use"]  # fmt: skip
        if calls and final.stop_reason != "max_tokens":
            self._raw[calls[0].id or ""] = list(final.content)
            while len(self._raw) > 256:
                self._raw.popitem(last=False)
        else:
            calls = []  # a truncated tool input is not safe to run
        yield LLMChunk(tool_calls=calls, done=True, metrics={
            **({"ttft_ms": round((time.perf_counter() - t0) * 1000, 1)} if first else {}),
            "prompt_eval_count": final.usage.input_tokens, "eval_count": final.usage.output_tokens,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        })  # fmt: skip

    async def chat_json(
        self, messages: list[ChatMessage], options: LLMOptions, schema: dict[str, Any]
    ) -> dict[str, Any]:
        req = {**self._request(messages, options), "max_tokens": 16000}
        req["output_config"] = {**req["output_config"], "format": {"type": "json_schema", "schema": schema}}
        try:
            final = await self.client.beta.messages.create(**req)
        except anthropic.APIError as exc:
            raise LLMError(f"Claude request failed: {exc}", code="llm_error", retryable=True) from exc
        text = next((b.text for b in final.content if b.type == "text"), "")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("Claude returned invalid JSON", code="llm_bad_response") from exc
        if not isinstance(parsed, dict):
            raise LLMError("Claude returned non-object JSON", code="llm_bad_response")
        return parsed
