"""Telephony provider interface.

Calling modes are never conflated:
* ``SIMULATION`` — no network call is made; outcomes come from a simulator.
* ``PSTN`` — a real call through a configured, authorised provider (disabled by default).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from engine.providers.base import ProviderError, ProviderHealth
from engine.streaming.sink import QueueAudioSink


class CallStatus(StrEnum):
    QUEUED = "queued"
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {CallStatus.COMPLETED, CallStatus.NO_ANSWER, CallStatus.BUSY, CallStatus.FAILED, CallStatus.CANCELLED}


class CallMedia(Protocol):
    """Audio path of an answered real call (mirrors the WebRTC transport surface)."""

    sink: QueueAudioSink

    def set_audio_handler(self, handler: Callable[[Any], None]) -> None: ...

    async def close(self, reason: str = "closed") -> None: ...


@dataclass
class TelephonyCall:
    provider: str
    provider_call_id: str
    direction: str  # inbound | outbound
    to_number: str
    from_number: str | None
    status: CallStatus = CallStatus.QUEUED
    events: list[dict[str, Any]] = field(default_factory=list)
    media: CallMedia | None = None
    hangup_cause: str | None = None

    def transition(self, status: CallStatus, detail: str = "") -> None:
        if self.status in TERMINAL:
            raise TelephonyError(f"call already ended ({self.status.value})", code="invalid_transition")
        self.status = status
        self.events.append({"status": status.value, "detail": detail, "at": datetime.now(UTC).isoformat()})


class TelephonyError(ProviderError):
    code = "telephony_error"


InboundHandler = Callable[[TelephonyCall], Awaitable[None]]


class TelephonyProvider(Protocol):
    name: str
    is_real: bool

    def validate_config(self) -> list[str]: ...

    async def health(self) -> ProviderHealth: ...

    async def dial(self, to_number: str, from_number: str | None, *, ring_timeout_s: float = 30,
                   seed: str | None = None) -> TelephonyCall: ...  # fmt: skip

    async def hangup(self, call: TelephonyCall, reason: str = "normal") -> None: ...

    def on_inbound(self, handler: InboundHandler) -> None: ...
