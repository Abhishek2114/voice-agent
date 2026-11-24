"""Explicitly registered, validated, permission-controlled agent tools.

The LLM can only *request* one of the tools below. The registry:
1. rejects tools the agent has not been granted,
2. validates arguments against a strict schema (unknown keys, types, lengths, enums),
3. executes a host-provided handler that records a *request* in the database.

No tool runs shell commands, fetches URLs, or completes a booking/payment/refund.
Tool results tell the model explicitly that requests are pending human follow-up.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass
class KnowledgeSnippet:
    title: str
    content: str
    score: float = 0.0
    document_id: str | None = None


class ToolHost(Protocol):
    """Implemented by the backend; scoped to one conversation/workspace."""

    async def create_callback_request(self, preferred_time: str, notes: str, kind: str) -> str: ...

    async def record_opt_out(self, reason: str) -> bool: ...

    async def search_knowledge(self, query: str, limit: int = 3) -> list[KnowledgeSnippet]: ...

    async def capture_lead(self, details: dict[str, Any]) -> dict[str, Any]:
        """Create/update a workspace contact from details the person gave. Returns
        {"contact_id", "created", "phone_valid", "consent_recorded"}."""
        ...


@dataclass
class ToolContext:
    workspace_id: str
    conversation_id: str
    host: ToolHost
    call_id: str | None = None
    contact_id: str | None = None


@dataclass
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    effects: list[str] = field(default_factory=list)

    def for_model(self) -> str:
        import json

        return json.dumps({"ok": self.ok, "message": self.message, **({"data": self.data} if self.data else {})})


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    label: str = ""

    def to_ollama(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


class ToolValidationError(ValueError):
    pass


def validate_args(schema: dict[str, Any], args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolValidationError("arguments must be an object")
    props: dict[str, Any] = schema.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise ToolValidationError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    for req in schema.get("required", []):
        if args.get(req) in (None, ""):
            raise ToolValidationError(f"missing required argument: {req}")
    clean: dict[str, Any] = {}
    for key, value in args.items():
        spec = props[key]
        t = spec.get("type")
        if t == "string":
            if not isinstance(value, str):
                value = str(value)
            value = value.strip()
            if len(value) > spec.get("maxLength", 500):
                raise ToolValidationError(f"{key} too long")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolValidationError(f"{key} must be one of {spec['enum']}")
        elif t == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolValidationError(f"{key} must be an integer")
        elif t == "boolean" and not isinstance(value, bool):
            raise ToolValidationError(f"{key} must be a boolean")
        clean[key] = value
    return clean


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def catalog(self) -> list[dict[str, str]]:
        return [{"name": t.name, "label": t.label or t.name, "description": t.description} for t in self._tools.values()]

    def specs_for(self, permitted: list[str] | set[str]) -> list[dict[str, Any]]:
        return [t.to_ollama() for name, t in self._tools.items() if name in permitted]

    async def execute(
        self, name: str, args: Any, permitted: list[str] | set[str], ctx: ToolContext
    ) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(False, f"Tool '{name}' does not exist.")
        if name not in permitted:
            log.warning("agent requested tool without permission: %s", name)
            return ToolResult(False, f"Tool '{name}' is not permitted for this agent.")
        try:
            clean = validate_args(spec.parameters, args)
        except ToolValidationError as exc:
            return ToolResult(False, f"Invalid arguments: {exc}")
        try:
            return await spec.handler(ctx, clean)
        except Exception:
            log.exception("tool %s failed", name)
            return ToolResult(False, "The request could not be recorded due to an internal error.")


# ---------------------------------------------------------------- built-in tools
async def _end_call(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    reason = args.get("reason", "completed")
    effects = ["end_call"]
    if reason == "wrong_number":
        effects.append("wrong_number")
    return ToolResult(True, "The call will end after your next sentence. Say a brief goodbye.", effects=effects)


async def _request_callback(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rid = await ctx.host.create_callback_request(args["preferred_time"], args.get("notes", ""), "callback")
    return ToolResult(
        True,
        "Callback REQUEST recorded for a human team member to review. It is not yet confirmed; "
        "tell the person the team will reach out, do not promise an exact time.",
        data={"request_id": rid},
        effects=["callback_requested"],
    )


async def _request_appointment(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    notes = f"Purpose: {args.get('purpose', '')}".strip()
    rid = await ctx.host.create_callback_request(args["preferred_time"], notes, "appointment")
    return ToolResult(
        True,
        "Appointment REQUEST recorded. It is NOT booked. Tell the person a team member will confirm.",
        data={"request_id": rid},
        effects=["appointment_requested"],
    )


async def _record_opt_out(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    recorded = await ctx.host.record_opt_out(args.get("reason", "requested during call"))
    msg = (
        "Opt-out recorded; this number will not be called again. Confirm politely and end the call."
        if recorded
        else "Opt-out noted for this conversation (no phone number is associated). Confirm politely."
    )
    return ToolResult(True, msg, effects=["opted_out", "end_call"])


async def _search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    hits = await ctx.host.search_knowledge(args["query"], 3)
    if not hits:
        return ToolResult(True, "No matching business information found. Do not guess; offer a follow-up.")
    return ToolResult(
        True,
        "Relevant business information (use only this; do not invent details).",
        data={"results": [{"title": h.title, "content": h.content[:700]} for h in hits]},
    )


async def _capture_lead(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if not (args.get("phone") or args.get("email")):
        return ToolResult(False, "Ask for a phone number or email before saving the lead.")
    res = await ctx.host.capture_lead(args)
    if not res.get("contact_id"):
        return ToolResult(False, res.get("message") or "The details could not be saved. Ask the person to repeat them.")
    msg = "Lead details saved for the team."
    if args.get("phone") and not res.get("phone_valid", True):
        msg += " The phone number looked invalid; read it back to the person and confirm it."
    if args.get("consent_to_contact") is not True:
        msg += " No follow-up consent was given, so the team will not contact them for promotions."
    return ToolResult(True, msg, data={"contact_id": res["contact_id"]}, effects=["lead_captured"])


async def _record_interest(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    level = args["level"]
    return ToolResult(True, "Noted.", effects=[f"interest:{level}"])


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        "end_call", "End the conversation after saying goodbye. Use when the conversation is finished, "
        "the person asks to stop, or it is a wrong number.",
        {"type": "object", "properties": {"reason": {"type": "string", "enum": [
            "completed", "not_interested", "wrong_number", "user_requested", "opted_out"]}},
         "required": ["reason"]},
        _end_call, "End call",
    ))  # fmt: skip
    reg.register(ToolSpec(
        "request_callback", "Record that the person wants a human to call them back.",
        {"type": "object", "properties": {
            "preferred_time": {"type": "string", "maxLength": 120, "description": "When they prefer, in their words"},
            "notes": {"type": "string", "maxLength": 500}}, "required": ["preferred_time"]},
        _request_callback, "Request callback",
    ))  # fmt: skip
    reg.register(ToolSpec(
        "request_appointment", "Record an appointment request for staff to confirm. Does NOT book anything.",
        {"type": "object", "properties": {
            "preferred_time": {"type": "string", "maxLength": 120},
            "purpose": {"type": "string", "maxLength": 300}}, "required": ["preferred_time"]},
        _request_appointment, "Request appointment",
    ))  # fmt: skip
    reg.register(ToolSpec(
        "record_opt_out", "The person asked not to be contacted again. Records an opt-out (do-not-call).",
        {"type": "object", "properties": {"reason": {"type": "string", "maxLength": 200}}},
        _record_opt_out, "Record opt-out",
    ))  # fmt: skip
    reg.register(ToolSpec(
        "search_knowledge", "Search this business's knowledge base (FAQs, products, policies, pricing).",
        {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200}}, "required": ["query"]},
        _search_knowledge, "Search knowledge base",
    ))  # fmt: skip
    reg.register(ToolSpec(
        "capture_lead", "Save the person as a lead once they have shared their details. Read the phone number "
        "back to confirm it first. Set consent_to_contact true ONLY if they explicitly agreed to be contacted.",
        {"type": "object", "properties": {
            "name": {"type": "string", "maxLength": 120},
            "phone": {"type": "string", "maxLength": 32},
            "email": {"type": "string", "maxLength": 200},
            "company": {"type": "string", "maxLength": 160},
            "requirement": {"type": "string", "maxLength": 600, "description": "What they need, in brief"},
            "consent_to_contact": {"type": "boolean",
                                   "description": "True only if they explicitly agreed to follow-up contact"}},
         "required": ["name"]},
        _capture_lead, "Capture lead",
    ))  # fmt: skip
    reg.register(ToolSpec(
        "record_interest", "Record the person's level of interest in the offer.",
        {"type": "object", "properties": {
            "level": {"type": "string", "enum": ["interested", "not_interested", "undecided"]},
            "notes": {"type": "string", "maxLength": 300}}, "required": ["level"]},
        _record_interest, "Record interest",
    ))  # fmt: skip
    return reg


DEFAULT_TOOLS = [
    "end_call", "request_callback", "record_opt_out", "search_knowledge", "record_interest", "capture_lead",
]
