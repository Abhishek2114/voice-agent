"""Telephony simulator: deterministic dial outcomes and simulated callees.

Nothing here touches a network or a phone. Every call it produces is labelled
SIMULATION by the backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import uuid
from dataclasses import dataclass

from engine.providers.base import ProviderHealth
from engine.providers.llm.base import ChatMessage, LLMOptions, LLMProvider
from engine.providers.telephony.base import (
    CallStatus,
    InboundHandler,
    TelephonyCall,
    TelephonyError,
)


@dataclass
class SimulationScenario:
    answer_rate: float = 0.75
    busy_rate: float = 0.05
    fail_rate: float = 0.05
    ring_seconds: float = 0.2

    def validate(self) -> list[str]:
        errs = []
        for name in ("answer_rate", "busy_rate", "fail_rate"):
            v = getattr(self, name)
            if not 0 <= v <= 1:
                errs.append(f"{name} must be between 0 and 1")
        if self.answer_rate + self.busy_rate + self.fail_rate > 1.0001:
            errs.append("answer_rate + busy_rate + fail_rate must not exceed 1")
        return errs


class SimulatedTelephonyProvider:
    name = "simulator"
    is_real = False

    def __init__(self, scenario: SimulationScenario | None = None) -> None:
        self.scenario = scenario or SimulationScenario()
        self._inbound: InboundHandler | None = None
        self.active: dict[str, TelephonyCall] = {}

    def validate_config(self) -> list[str]:
        return self.scenario.validate()

    async def health(self) -> ProviderHealth:
        errs = self.validate_config()
        return ProviderHealth(self.name, "degraded" if errs else "ok",
                              "; ".join(errs) or "simulator ready (no real calls are placed)")  # fmt: skip

    async def dial(self, to_number: str, from_number: str | None, *, ring_timeout_s: float = 30,
                   seed: str | None = None) -> TelephonyCall:  # fmt: skip
        errs = self.validate_config()
        if errs:
            raise TelephonyError("; ".join(errs), code="invalid_config")
        if not to_number:
            raise TelephonyError("missing destination number", code="invalid_number")
        call = TelephonyCall(self.name, f"sim-{uuid.uuid4().hex[:12]}", "outbound", to_number, from_number)
        call.transition(CallStatus.DIALING)
        call.transition(CallStatus.RINGING)
        await asyncio.sleep(min(self.scenario.ring_seconds, ring_timeout_s))
        roll = _roll(seed or call.provider_call_id)
        s = self.scenario
        if roll < s.answer_rate:
            call.transition(CallStatus.ANSWERED)
            self.active[call.provider_call_id] = call
        elif roll < s.answer_rate + s.busy_rate:
            call.transition(CallStatus.BUSY, "simulated busy")
        elif roll < s.answer_rate + s.busy_rate + s.fail_rate:
            call.transition(CallStatus.FAILED, "simulated network failure")
            call.hangup_cause = "simulated_failure"
        else:
            call.transition(CallStatus.NO_ANSWER, "simulated no answer")
        return call

    async def simulate_inbound(self, from_number: str, to_number: str = "simulated-line") -> TelephonyCall:
        call = TelephonyCall(self.name, f"sim-in-{uuid.uuid4().hex[:12]}", "inbound", to_number, from_number)
        call.transition(CallStatus.RINGING)
        call.transition(CallStatus.ANSWERED)
        self.active[call.provider_call_id] = call
        if self._inbound:
            await self._inbound(call)
        return call

    async def hangup(self, call: TelephonyCall, reason: str = "normal") -> None:
        self.active.pop(call.provider_call_id, None)
        if call.status == CallStatus.ANSWERED:
            call.transition(CallStatus.COMPLETED, reason)
        elif call.status not in (CallStatus.COMPLETED, CallStatus.NO_ANSWER, CallStatus.BUSY,
                                 CallStatus.FAILED, CallStatus.CANCELLED):  # fmt: skip
            call.transition(CallStatus.CANCELLED, reason)

    def on_inbound(self, handler: InboundHandler) -> None:
        self._inbound = handler


def _roll(seed: str) -> float:
    digest = hashlib.sha256(seed.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


# ---------------------------------------------------------------- simulated callees
PERSONA_SCRIPTS: dict[str, list[str]] = {
    "interested": [
        "Hello? Yes, this is {name}.",
        "Okay, sure, tell me a bit more.",
        "That sounds useful. What would it cost roughly?",
        "Alright. Yes, you can have someone contact me on this number about it.",
        "Great, thanks. Bye.",
    ],
    "not_interested": [
        "Hello, who is this?",
        "I'm not really interested, sorry.",
        "No thank you. Bye.",
    ],
    "callback": [
        "Hello, {name} speaking.",
        "I'm a bit busy right now. Can someone call me back tomorrow evening around six?",
        "Yes, tomorrow at six works. Thanks, bye.",
    ],
    "questions": [
        "Hi, yes?",
        "What exactly do you offer?",
        "Do you provide support if something goes wrong?",
        "Okay, thanks for the information. Bye.",
    ],
    "wrong_number": [
        "Hello?",
        "Sorry, there's nobody called {name} here. You have the wrong number.",
        "No problem. Bye.",
    ],
    "opt_out": [
        "Hello?",
        "Please don't call me again. Remove my number from your list.",
        "Yes. Goodbye.",
    ],
}


class ScriptedCallee:
    def __init__(self, persona: str, contact_name: str | None = None) -> None:
        if persona not in PERSONA_SCRIPTS:
            raise ValueError(f"unknown persona: {persona}")
        self.persona = persona
        self.lines = [ln.format(name=contact_name or "the person you asked for") for ln in PERSONA_SCRIPTS[persona]]
        self.turn = 0

    async def reply(self, agent_text: str) -> str | None:
        if self.turn >= len(self.lines):
            return None
        line = self.lines[self.turn]
        self.turn += 1
        return line


class LLMCallee:
    """A local LLM role-plays the person being called (simulation only)."""

    def __init__(self, llm: LLMProvider, options: LLMOptions, persona: str, contact_name: str | None,
                 max_turns: int = 6) -> None:  # fmt: skip
        self.llm = llm
        self.options = options
        self.max_turns = max_turns
        self.turn = 0
        self.history: list[ChatMessage] = [ChatMessage("system", (
            f"You are role-playing a person named {contact_name or 'Alex'} receiving a phone call. "
            f"Your attitude: {persona.replace('_', ' ')}. Reply with one short spoken sentence only. "
            "When you want to end the call, say goodbye."
        ))]  # fmt: skip

    async def reply(self, agent_text: str) -> str | None:
        if self.turn >= self.max_turns:
            return None
        self.turn += 1
        self.history.append(ChatMessage("user", agent_text))
        text = ""
        async for chunk in self.llm.stream_chat(self.history, self.options):
            text += chunk.text
        text = text.strip()
        self.history.append(ChatMessage("assistant", text))
        return text or None


def pick_persona(weights: dict[str, float], seed: str) -> str:
    items = [(k, v) for k, v in weights.items() if k in PERSONA_SCRIPTS and v > 0]
    if not items:
        return "questions"
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    return rng.choices([k for k, _ in items], weights=[v for _, v in items], k=1)[0]
