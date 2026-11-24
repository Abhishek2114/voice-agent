"""Conversation session, tools, text processing and outcome analysis."""

from __future__ import annotations

from typing import Any

import pytest

from engine.conversation.outcome import analyze_call, rule_outcome
from engine.conversation.prompt import AgentPersona, build_system_prompt
from engine.conversation.session import (
    ConversationSession,
    SessionConfig,
    TranscriptEntry,
    _now,
)
from engine.conversation.text import SentenceSplitter, speechify
from engine.conversation.tools import (
    DEFAULT_TOOLS,
    KnowledgeSnippet,
    ToolContext,
    ToolValidationError,
    default_registry,
    validate_args,
)
from engine.providers.llm.base import LLMError, LLMOptions, ToolCall
from engine.providers.llm.scripted import ScriptedLLM


class Host:
    def __init__(self) -> None:
        self.callbacks: list[tuple[str, str, str]] = []
        self.opt_outs: list[str] = []
        self.leads: list[dict[str, Any]] = []

    async def create_callback_request(self, preferred_time: str, notes: str, kind: str) -> str:
        self.callbacks.append((preferred_time, notes, kind))
        return "cb1"

    async def record_opt_out(self, reason: str) -> bool:
        self.opt_outs.append(reason)
        return True

    async def search_knowledge(self, query: str, limit: int = 3) -> list[KnowledgeSnippet]:
        return [KnowledgeSnippet("FAQ", "Support is available 24x7 by email.")] if "support" in query else []

    async def capture_lead(self, details: dict[str, Any]) -> dict[str, Any]:
        self.leads.append(details)
        return {"contact_id": "c1", "created": True, "phone_valid": True, "consent_recorded": True}


def make_session(llm, tools=None, knowledge=None, **cfg) -> tuple[ConversationSession, Host]:
    host = Host()
    persona = AgentPersona(name="Asha", business_name="Acme Hosting", objectives=["Help callers"])
    config = SessionConfig(persona=persona, llm_options=LLMOptions(model="scripted"), greeting="Hi **there**! 😀",
                           tools_allowed=tools if tools is not None else list(DEFAULT_TOOLS), **cfg)  # fmt: skip
    ctx = ToolContext(workspace_id="w1", conversation_id="c1", host=host)
    return ConversationSession(config, llm, default_registry(), ctx, knowledge), host


def test_speechify_strips_formatting():
    assert speechify("**Hello** see https://x.com 😀\n- item") == "Hello see our website item"
    assert speechify("<think>secret</think>Answer.") == "Answer."


def test_sentence_splitter_streams():
    sp = SentenceSplitter()
    out = []
    for piece in ["Hello there, how are", " you today? I am fine", ". Dr. Smith said hi"]:
        out += sp.push(piece)
    out += sp.flush()
    assert out == ["Hello there, how are you today?", "I am fine.", "Dr. Smith said hi"]


def test_system_prompt_contains_guardrails():
    prompt = build_system_prompt(AgentPersona(name="A", business_name="B", business_knowledge="Plan X costs 100"))
    assert "Never claim to have completed a booking" in prompt
    assert "Plan X costs 100" in prompt
    assert "AI assistant" in prompt


def test_validate_args_rejects_bad_input():
    schema = default_registry()._tools["request_callback"].parameters
    with pytest.raises(ToolValidationError):
        validate_args(schema, {"preferred_time": "x", "evil": "rm -rf /"})
    with pytest.raises(ToolValidationError):
        validate_args(schema, {"notes": "no time"})
    with pytest.raises(ToolValidationError):
        validate_args(schema, {"preferred_time": "x" * 500})
    assert validate_args(schema, {"preferred_time": " tomorrow "}) == {"preferred_time": "tomorrow"}


async def test_session_text_turn_and_greeting():
    session, _ = make_session(ScriptedLLM(lambda m: "We offer web hosting. Anything else?"))
    assert session.greeting_text() == "Hi there!"
    text, metrics = await session.respond_text("What do you do?")
    assert text == "We offer web hosting. Anything else?"
    assert [e.role for e in session.transcript] == ["user", "assistant"]
    assert "llm_total_ms" in metrics


async def test_tool_call_is_executed_and_recorded():
    calls = iter([("", [ToolCall("request_callback", {"preferred_time": "tomorrow 6pm"})]),
                  "Sure, our team will call you back."])  # fmt: skip
    session, host = make_session(ScriptedLLM(lambda m: next(calls)))
    text, _ = await session.respond_text("Call me tomorrow at 6")
    assert host.callbacks == [("tomorrow 6pm", "", "callback")]
    assert "callback_requested" in session.effects
    assert text == "Sure, our team will call you back."
    assert any(e.role == "tool" for e in session.transcript)


async def test_unpermitted_tool_is_refused():
    calls = iter([("", [ToolCall("record_opt_out", {})]), "Okay."])
    session, host = make_session(ScriptedLLM(lambda m: next(calls)), tools=["end_call"])
    await session.respond_text("stop calling")
    assert host.opt_outs == []
    assert session.tool_log[0]["ok"] is False


async def test_capture_lead_tool():
    args = {"name": "Ravi", "phone": "+919800000001", "requirement": "VPS", "consent_to_contact": True}
    calls = iter([("", [ToolCall("capture_lead", args)]), "Thanks Ravi, the team will reach out."])
    session, host = make_session(ScriptedLLM(lambda m: next(calls)))
    await session.respond_text("I'm Ravi, call me on 98000 00001 about a VPS")
    assert host.leads == [args]
    assert "lead_captured" in session.effects


async def test_opt_out_tool_requests_end():
    calls = iter([("", [ToolCall("record_opt_out", {"reason": "asked"})]), "Understood, goodbye."])
    session, host = make_session(ScriptedLLM(lambda m: next(calls)))
    await session.respond_text("don't call me again")
    assert host.opt_outs == ["asked"]
    assert session.end_requested


async def test_knowledge_is_injected_per_turn():
    llm = ScriptedLLM(lambda m: "ok.")
    session, _ = make_session(llm, knowledge=lambda q: [KnowledgeSnippet("Pricing", "Basic plan details")])
    await session.respond_text("price?")
    sent = llm.calls[-1]
    assert any("Basic plan details" in m.content for m in sent if m.role == "system")
    # retrieved knowledge is not persisted into the conversation history
    assert not any("Basic plan details" in m.content for m in session.history)


async def test_sessions_are_isolated():
    llm = ScriptedLLM(lambda m: "noted.")
    a, _ = make_session(llm)
    b, _ = make_session(llm)
    await a.respond_text("my secret is 1234")
    await b.respond_text("hello")
    assert not any("1234" in m.content for m in llm.calls[-1])


async def test_end_phrase_requests_end():
    session, _ = make_session(ScriptedLLM(lambda m: "Bye!"))
    await session.respond_text("ok thanks, bye")
    assert session.end_requested and session.end_reason == "user_goodbye"


async def test_llm_failure_propagates():
    session, _ = make_session(ScriptedLLM(fail_with=LLMError("down", code="llm_unavailable")))
    with pytest.raises(LLMError):
        await session.respond_text("hi")


def _entries(*pairs) -> list[TranscriptEntry]:
    return [TranscriptEntry(role, text, _now()) for role, text in pairs]


async def test_outcome_rules_take_precedence():
    entries = _entries(("assistant", "Hi"), ("user", "stop calling"))
    assert rule_outcome(entries, ["opted_out"]) == "opted_out"
    res = await analyze_call(ScriptedLLM(json_result={"outcome": "interested", "summary": "s", "sentiment": "neutral"}),
                             LLMOptions(model="x"), entries, ["opted_out"])  # fmt: skip
    assert res.outcome == "opted_out"


async def test_outcome_no_answer_without_user_speech():
    res = await analyze_call(None, None, _entries(("assistant", "Hello?")), [])
    assert res.outcome == "no_answer"


async def test_outcome_llm_classification():
    llm = ScriptedLLM(json_result={"outcome": "interested", "summary": "Caller wants VPS.", "sentiment": "positive"})
    res = await analyze_call(llm, LLMOptions(model="x"), _entries(("assistant", "Hi"), ("user", "tell me more")), [])
    assert (res.outcome, res.summary, res.source) == ("interested", "Caller wants VPS.", "llm")


async def test_outcome_reports_error_instead_of_guessing():
    llm = ScriptedLLM(fail_with=LLMError("down", code="llm_unavailable"))
    res = await analyze_call(llm, LLMOptions(model="x"), _entries(("assistant", "Hi"), ("user", "hmm")), [])
    assert res.outcome is None and res.summary is None and res.error
