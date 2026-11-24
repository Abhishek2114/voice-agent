"""Ollama adapter (https://github.com/ollama/ollama/blob/main/docs/api.md).

Policy enforced here, not in the UI:
* Models that Ollama proxies to a remote host (``remote_host`` set, or ``:cloud`` tags)
  are reported as **not local** and refused unless ``allow_remote_models`` is
  explicitly enabled. There is no silent cloud fallback.
* Never pulls/downloads models.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from engine.providers.base import HealthStatus, ProviderHealth
from engine.providers.llm.base import (
    ChatMessage,
    LLMChunk,
    LLMError,
    LLMOptions,
    ModelInfo,
    ToolCall,
)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        allow_remote_models: bool = False,
        connect_timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.allow_remote_models = allow_remote_models
        self._timeout = httpx.Timeout(connect=connect_timeout_s, read=None, write=30.0, pool=10.0)
        self._transport = transport
        self._models: dict[str, ModelInfo] = {}
        self._models_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout, transport=self._transport)

    # ------------------------------------------------------------------ discovery
    async def health(self) -> ProviderHealth:
        t0 = time.perf_counter()
        try:
            async with self._client() as c:
                r = await c.get("/api/version", timeout=3.0)
                r.raise_for_status()
                version = r.json().get("version")
                ps = await c.get("/api/ps", timeout=3.0)
                loaded = [m.get("name") for m in ps.json().get("models", [])] if ps.status_code == 200 else []
            models = await self.list_models(force=True)
        except (httpx.HTTPError, ValueError) as exc:
            return ProviderHealth(
                self.name,
                "unavailable",
                f"Ollama is not reachable at {self.base_url}: {exc.__class__.__name__}. "
                "Start it with `ollama serve` (bound to 127.0.0.1).",
            )
        local = [m.name for m in models if m.is_local]
        status: HealthStatus = "ok" if local else "degraded"
        detail = f"Ollama {version}; {len(local)} local model(s)" if local else "Ollama running but no local models"
        return ProviderHealth(
            self.name,
            status,
            detail,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            extra={
                "version": version,
                "local_models": local,
                "remote_models": [m.name for m in models if not m.is_local],
                "loaded_models": loaded,
            },
        )

    async def list_models(self, force: bool = False) -> list[ModelInfo]:
        if not force and self._models and time.monotonic() - self._models_at < 30:
            return list(self._models.values())
        try:
            async with self._client() as c:
                r = await c.get("/api/tags", timeout=5.0)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama unavailable: {exc}", code="llm_unavailable", retryable=True) from exc
        models: dict[str, ModelInfo] = {}
        for m in data.get("models", []):
            details = m.get("details") or {}
            remote_host = m.get("remote_host")
            name = m.get("name") or m.get("model")
            is_local = not remote_host and not str(name).endswith(":cloud") and "-cloud" not in str(name)
            models[name] = ModelInfo(
                name=name,
                size_bytes=m.get("size"),
                family=details.get("family") or None,
                parameter_size=details.get("parameter_size") or None,
                quantization=details.get("quantization_level") or None,
                is_local=is_local,
                remote_host=remote_host,
                capabilities=list(m.get("capabilities") or []),
                context_length=details.get("context_length"),
            )
        self._models = models
        self._models_at = time.monotonic()
        return list(models.values())

    async def _resolve(self, model: str) -> ModelInfo:
        models = {m.name: m for m in await self.list_models()}
        info = models.get(model)
        if info is None:
            # refresh once in case a model was just added
            models = {m.name: m for m in await self.list_models(force=True)}
            info = models.get(model)
        if info is None:
            raise LLMError(
                f"Model '{model}' is not installed in Ollama. Installed: {', '.join(models) or 'none'}.",
                code="model_not_found",
            )
        if not info.is_local and not self.allow_remote_models:
            raise LLMError(
                f"Model '{model}' is served by a remote host ({info.remote_host or 'cloud'}) and is not local. "
                "Remote models are disabled; choose a local model.",
                code="remote_model_refused",
            )
        return info

    def _payload(
        self, info: ModelInfo, messages: list[ChatMessage], options: LLMOptions, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": info.name,
            "messages": [m.to_ollama() for m in messages],
            "stream": stream,
            "keep_alive": options.keep_alive,
            "options": {
                "temperature": options.temperature,
                "top_p": options.top_p,
                "num_predict": options.max_tokens,
                "num_ctx": options.num_ctx,
            },
        }
        if "thinking" in info.capabilities:
            payload["think"] = False  # voice needs low latency; reasoning traces are never spoken
        return payload

    # ------------------------------------------------------------------ inference
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: LLMOptions,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        info = await self._resolve(options.model)
        payload = self._payload(info, messages, options, stream=True)
        if tools:
            payload["tools"] = tools
        t0 = time.perf_counter()
        first = True
        try:
            async with self._client() as c, c.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")[:300]
                    raise _http_error(resp.status_code, body, options.model)
                lines = resp.aiter_lines()
                while True:
                    timeout = options.first_token_timeout_s if first else options.chunk_timeout_s
                    try:
                        line = await asyncio.wait_for(anext(lines), timeout=timeout)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as exc:
                        phase = "loading model / first token" if first else "streaming"
                        raise LLMError(
                            f"Ollama timed out while {phase} ({timeout:.0f}s).", code="llm_timeout", retryable=True
                        ) from exc
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise LLMError(f"Ollama error: {data['error']}", code="llm_error")
                    msg = data.get("message") or {}
                    calls = [
                        ToolCall(name=tc["function"]["name"], arguments=_args(tc["function"].get("arguments")))
                        for tc in msg.get("tool_calls") or []
                        if tc.get("function", {}).get("name")
                    ]
                    text = msg.get("content") or ""
                    chunk = LLMChunk(text=text, tool_calls=calls, done=bool(data.get("done")))
                    if first and (text or calls):
                        chunk.metrics["ttft_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                        first = False
                    if chunk.done:
                        chunk.metrics.update(
                            {
                                "eval_count": data.get("eval_count"),
                                "eval_duration_ms": (data.get("eval_duration") or 0) / 1e6,
                                "load_duration_ms": (data.get("load_duration") or 0) / 1e6,
                                "prompt_eval_count": data.get("prompt_eval_count"),
                                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
                            }
                        )
                    yield chunk
                    if chunk.done:
                        return
        except httpx.ConnectError as exc:
            raise LLMError(
                f"Cannot connect to Ollama at {self.base_url}. Is `ollama serve` running?",
                code="llm_unavailable",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}", code="llm_error", retryable=True) from exc

    async def chat_json(
        self, messages: list[ChatMessage], options: LLMOptions, schema: dict[str, Any]
    ) -> dict[str, Any]:
        info = await self._resolve(options.model)
        payload = self._payload(info, messages, options, stream=False)
        payload["format"] = schema
        try:
            async with self._client() as c:
                r = await asyncio.wait_for(
                    c.post("/api/chat", json=payload), timeout=options.first_token_timeout_s + 60
                )
        except TimeoutError as exc:
            raise LLMError("Ollama timed out producing structured output", code="llm_timeout") from exc
        except httpx.ConnectError as exc:
            raise LLMError("Cannot connect to Ollama", code="llm_unavailable", retryable=True) from exc
        if r.status_code != 200:
            raise _http_error(r.status_code, r.text[:300], options.model)
        content = (r.json().get("message") or {}).get("content", "")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError("Model returned invalid JSON", code="llm_bad_response") from exc
        if not isinstance(parsed, dict):
            raise LLMError("Model returned non-object JSON", code="llm_bad_response")
        return parsed

    async def warm_up(self, model: str, keep_alive: str = "15m") -> float:
        """Load a model into memory ahead of a call. Returns load time in ms."""
        await self._resolve(model)
        t0 = time.perf_counter()
        async with self._client() as c:
            r = await c.post(
                "/api/generate", json={"model": model, "prompt": "", "keep_alive": keep_alive}, timeout=180.0
            )
            if r.status_code != 200:
                raise _http_error(r.status_code, r.text[:300], model)
        return round((time.perf_counter() - t0) * 1000, 1)


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _http_error(status: int, body: str, model: str) -> LLMError:
    if status == 404:
        return LLMError(f"Model '{model}' not found in Ollama", code="model_not_found")
    if status in (500, 503):
        return LLMError(f"Ollama server error ({status}): {body}", code="llm_error", retryable=True)
    return LLMError(f"Ollama returned HTTP {status}: {body}", code="llm_error")
