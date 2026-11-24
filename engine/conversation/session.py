"""Conversation session: isolated per-call memory, LLM turn handling and tool loop.

A session never shares history with another session, even when the same agent and
the same local model serve many conversations.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from engine.conversation.prompt import (
    AgentPersona,
    build_system_prompt,
    knowledge_context,
)
from engine.conversation.text import SentenceSplitter, speechify
from engine.conversation.tools import KnowledgeSnippet, ToolContext, ToolRegistry
from engine.providers.llm.base import ChatMessage, LLMOptions, LLMProvider, ToolCall

DEFAULT_FALLBACKS = {
    "llm_error": "Sorry, I'm having a technical problem right now. A member of our team will follow up with you.",
    "silence": "Are you still there?",
    "goodbye": "Thank you for your time. Goodbye!",
    "max_duration": "I'm sorry, we've reached the time limit for this call. Thank you, goodbye!",
    "not_understood": "Sorry, I didn't catch that. Could you say it again?",
}

DEFAULT_END_PHRASES = ["goodbye", "bye bye", "bye", "hang up", "end the call", "that's all"]


@dataclass
class SessionConfig:
    persona: AgentPersona
    llm_options: LLMOptions
    greeting: str
    tools_allowed: list[str] = field(default_factory=list)
    fallbacks: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FALLBACKS))
    end_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_END_PHRASES))
    max_turns: int = 40
    use_knowledge: bool = True
    history_limit: int = 24


@dataclass
class TranscriptEntry:
    role: Literal["assistant", "user", "tool", "system"]
    text: str
    at: datetime
    interrupted: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponseEvent:
    type: Literal["delta", "sentence", "tool", "done", "error"]
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ConversationSession:
    def __init__(
        self,
        config: SessionConfig,
        llm: LLMProvider,
        tools: ToolRegistry,
        tool_ctx: ToolContext | None = None,
        knowledge: Callable[[str], Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex
        self.config = config
        self.llm = llm
        self.tools = tools
        self.tool_ctx = tool_ctx
        self.knowledge = knowledge
        self.history: list[ChatMessage] = []
        self.transcript: list[TranscriptEntry] = []
        self.effects: list[str] = []
        self.tool_log: list[dict[str, Any]] = []
        self.end_requested = False
        self.end_reason: str | None = None
        self.user_turns = 0
        self.started_at = time.monotonic()
        self._system_prompt = build_system_prompt(config.persona)

    # ------------------------------------------------------------------ helpers
    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def greeting_text(self) -> str:
        return speechify(self.config.greeting)

    def fallback(self, key: str) -> str:
        return self.config.fallbacks.get(key) or DEFAULT_FALLBACKS.get(key, "")

    def add_assistant(self, text: str, *, interrupted: bool = False, metrics: dict[str, Any] | None = None) -> None:
        text = text.strip()
        if not text:
            return
        content = text + (" …" if interrupted else "")
        self.history.append(ChatMessage("assistant", content))
        self.transcript.append(TranscriptEntry("assistant", text, _now(), interrupted, metrics or {}))

    def add_user(self, text: str, metrics: dict[str, Any] | None = None) -> None:
        self.history.append(ChatMessage("user", text))
        self.transcript.append(TranscriptEntry("user", text, _now(), False, metrics or {}))
        self.user_turns += 1
        lowered = text.lower()
        if any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in self.config.end_phrases):
            self.request_end("user_goodbye")
        if self.user_turns >= self.config.max_turns:
            self.request_end("max_turns")

    def request_end(self, reason: str) -> None:
        if not self.end_requested:
            self.end_requested = True
            self.end_reason = reason

    def _messages(self, extra_system: str = "") -> list[ChatMessage]:
        msgs = [ChatMessage("system", self._system_prompt)]
        if extra_system:
            msgs.append(ChatMessage("system", extra_system))
        msgs.extend(self.history[-self.config.history_limit :])
        return msgs

    async def _retrieve(self, query: str) -> list[KnowledgeSnippet]:
        if not (self.config.use_knowledge and self.knowledge):
            return []
        result = self.knowledge(query)
        if hasattr(result, "__await__"):
            result = await result
        return list(result or [])

    # ------------------------------------------------------------------ turn
    async def respond(self, user_text: str | None = None) -> AsyncIterator[ResponseEvent]:
        """Generate the assistant's next turn. The caller must call :meth:`add_assistant`
        with what was actually delivered (full text, or the spoken part if interrupted)."""
        if user_text is not None:
            self.add_user(user_text)
        snippets = await self._retrieve(user_text) if user_text else []
        extra = knowledge_context(snippets)
        tools = self.tools.specs_for(self.config.tools_allowed) if self.tool_ctx else None
        full = ""
        metrics: dict[str, Any] = {"knowledge_hits": len(snippets)}
        t0 = time.perf_counter()
        for _round in range(3):  # at most 2 tool rounds, then a final spoken answer
            splitter = SentenceSplitter()
            calls: list[ToolCall] = []
            async for chunk in self.llm.stream_chat(self._messages(extra), self.config.llm_options, tools):
                if "ttft_ms" in chunk.metrics and "llm_ttft_ms" not in metrics:
                    metrics["llm_ttft_ms"] = chunk.metrics["ttft_ms"]
                if chunk.done:
                    metrics.update({k: v for k, v in chunk.metrics.items() if k != "ttft_ms"})
                calls.extend(chunk.tool_calls)
                if chunk.text:
                    full += chunk.text
                    yield ResponseEvent("delta", chunk.text)
                    for sentence in splitter.push(chunk.text):
                        yield ResponseEvent("sentence", sentence)
            for sentence in splitter.flush():
                yield ResponseEvent("sentence", sentence)
            if not calls or self.tool_ctx is None:
                break
            self.history.append(ChatMessage("assistant", full, tool_calls=calls))
            for call in calls:
                result = await self.tools.execute(call.name, call.arguments, self.config.tools_allowed, self.tool_ctx)
                self.effects.extend(result.effects)
                if "end_call" in result.effects:
                    self.request_end("agent_ended")
                entry = {"tool": call.name, "arguments": call.arguments, "ok": result.ok, "message": result.message,
                         "effects": result.effects, "data": result.data}  # fmt: skip
                self.tool_log.append(entry)
                self.transcript.append(TranscriptEntry("tool", f"{call.name}: {result.message}", _now(), metrics=entry))
                self.history.append(ChatMessage("tool", result.for_model(), tool_name=call.name))
                yield ResponseEvent("tool", call.name, entry)
            full = ""
            tools = None if _round >= 1 else tools  # force a spoken answer on the last round
        metrics["llm_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        yield ResponseEvent("done", speechify(full), metrics)

    async def respond_text(self, user_text: str | None = None) -> tuple[str, dict[str, Any]]:
        """Non-streaming convenience used by text simulations and the agent test console."""
        sentences: list[str] = []
        metrics: dict[str, Any] = {}
        async for ev in self.respond(user_text):
            if ev.type == "sentence":
                sentences.append(ev.text)
            elif ev.type == "done":
                metrics = ev.data
        text = " ".join(sentences).strip()
        self.add_assistant(text, metrics=metrics)
        return text, metrics

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


def _now() -> datetime:
    return datetime.now(UTC)
