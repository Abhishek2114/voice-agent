"""LLM provider interface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from engine.providers.base import ProviderError, ProviderHealth

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass
class ChatMessage:
    role: Role
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_name: str | None = None

    def to_ollama(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in self.tool_calls]
        if self.tool_name:
            msg["tool_name"] = self.tool_name
        return msg


@dataclass
class LLMOptions:
    model: str
    temperature: float = 0.6
    top_p: float = 0.9
    max_tokens: int = 220
    num_ctx: int = 4096
    keep_alive: str = "15m"
    # first-token timeout covers model load; inter-chunk timeout detects stalls.
    first_token_timeout_s: float = 120.0
    chunk_timeout_s: float = 30.0


@dataclass
class LLMChunk:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    done: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelInfo:
    name: str
    size_bytes: int | None = None
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    is_local: bool = True
    remote_host: str | None = None
    capabilities: list[str] = field(default_factory=list)
    context_length: int | None = None


class LLMError(ProviderError):
    code = "llm_error"


class LLMProvider(Protocol):
    name: str

    async def health(self) -> ProviderHealth: ...

    async def list_models(self, force: bool = False) -> list[ModelInfo]: ...

    def stream_chat(
        self,
        messages: list[ChatMessage],
        options: LLMOptions,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMChunk]: ...

    async def chat_json(
        self, messages: list[ChatMessage], options: LLMOptions, schema: dict[str, Any]
    ) -> dict[str, Any]: ...
