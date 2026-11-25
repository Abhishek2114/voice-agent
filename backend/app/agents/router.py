"""AI agents: builder CRUD, versions, duplication, activation, public links and a text test console."""

from __future__ import annotations

from typing import Any, Literal

from engine.conversation.session import ConversationSession
from engine.conversation.tools import KnowledgeSnippet, ToolContext, default_registry
from engine.providers.base import ProviderError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime import agent_tools, session_config, snapshot
from app.ai.hub import TTS_PROVIDERS, get_hub
from app.auth.deps import WorkspaceContext, ws_editor, ws_viewer
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, conflict, not_found
from app.core.security import new_token, token_hash
from app.database.base import utcnow
from app.database.models import Agent, AgentTool, AgentVersion, Call, Campaign
from app.database.session import get_db
from app.knowledge.service import search

router = APIRouter(prefix="/api/workspaces/{workspace_id}/agents", tags=["agents"])
_registry = default_registry()


class LLMCfg(BaseModel):
    provider: Literal["ollama"] = "ollama"
    model: str | None = Field(default=None, max_length=120)
    temperature: float = Field(default=0.6, ge=0, le=1.5)
    max_tokens: int = Field(default=200, ge=16, le=1024)


class STTCfg(BaseModel):
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    end_silence_ms: int | None = Field(default=None, ge=300, le=3000)


class TTSCfg(BaseModel):
    provider: str = "piper"
    voice: str | None = Field(default=None, max_length=120)
    speaker: str | None = Field(default=None, max_length=60)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @field_validator("provider")
    @classmethod
    def _p(cls, v: str) -> str:
        if v not in TTS_PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(TTS_PROVIDERS)}")
        return v


class EndRules(BaseModel):
    end_phrases: list[str] = Field(default_factory=lambda: ["goodbye", "bye", "hang up", "that's all"], max_length=30)
    max_turns: int = Field(default=40, ge=2, le=200)


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    personality: str = Field(default="Warm, concise and professional.", max_length=2000)
    system_instructions: str = Field(default="", max_length=20_000)
    greeting: str = Field(default="", max_length=1000)
    objectives: list[str] = Field(default_factory=list, max_length=20)
    business_knowledge: str = Field(default="", max_length=20_000)
    language: str = Field(default="en", pattern=r"^([a-z]{2}(-[A-Z]{2})?|auto)$")
    llm: LLMCfg = Field(default_factory=LLMCfg)
    stt: STTCfg = Field(default_factory=STTCfg)
    tts: TTSCfg = Field(default_factory=TTSCfg)
    max_duration_seconds: int = Field(default=600, ge=30, le=3600)
    silence_timeout_seconds: int = Field(default=20, ge=5, le=120)
    end_rules: EndRules = Field(default_factory=EndRules)
    tools: list[str] = Field(default_factory=lambda: ["end_call", "request_callback", "record_opt_out",
                                                      "search_knowledge", "record_interest", "capture_lead"])  # fmt: skip
    fallback_responses: dict[str, str] = Field(default_factory=dict)
    use_knowledge_base: bool = True
    is_active: bool = True
    version_note: str = Field(default="", max_length=300)

    @field_validator("tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(_registry.names())
        if unknown:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(unknown))}")
        return list(dict.fromkeys(v))

    @field_validator("fallback_responses")
    @classmethod
    def _fb(cls, v: dict[str, str]) -> dict[str, str]:
        allowed = {"llm_error", "silence", "goodbye", "max_duration", "not_understood"}
        if set(v) - allowed:
            raise ValueError(f"fallback keys must be among {', '.join(sorted(allowed))}")
        if any(len(x) > 500 for x in v.values()):
            raise ValueError("fallback responses must be under 500 characters")
        return v

    @field_validator("objectives")
    @classmethod
    def _obj(cls, v: list[str]) -> list[str]:
        return [o.strip()[:300] for o in v if o.strip()]


class TestTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class TestIn(BaseModel):
    history: list[TestTurn] = Field(default_factory=list, max_length=40)
    message: str = Field(min_length=1, max_length=2000)


def agent_out(a: Agent, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": a.id, "name": a.name, "description": a.description, "personality": a.personality,
        "system_instructions": a.system_instructions, "greeting": a.greeting, "objectives": a.objectives or [],
        "business_knowledge": a.business_knowledge, "language": a.language, "llm": a.llm or {}, "stt": a.stt or {},
        "tts": a.tts or {}, "max_duration_seconds": a.max_duration_seconds,
        "silence_timeout_seconds": a.silence_timeout_seconds, "end_rules": a.end_rules or {},
        "fallback_responses": a.fallback_responses or {}, "use_knowledge_base": a.use_knowledge_base,
        "is_active": a.is_active, "public_link_enabled": a.public_link_enabled, "version": a.version,
        "tools": agent_tools(a), "created_at": a.created_at, "updated_at": a.updated_at, "stats": stats or {},
    }  # fmt: skip


async def get_agent(db: AsyncSession, ws_id: str, agent_id: str) -> Agent:
    a = await db.get(Agent, agent_id)
    if a is None or a.workspace_id != ws_id or a.deleted_at is not None:
        raise not_found("Agent")
    return a


def _apply(a: Agent, body: AgentIn, ws_id: str) -> None:
    data = body.model_dump(exclude={"tools", "version_note"})
    for k, v in data.items():
        setattr(a, k, v)
    if not a.llm.get("model"):
        a.llm = {**a.llm, "model": get_hub().default_model()}
    existing = {t.tool_name: t for t in a.tools}
    for name in _registry.names():
        enabled = name in body.tools
        if name in existing:
            existing[name].enabled = enabled
        else:
            a.tools.append(AgentTool(workspace_id=ws_id, tool_name=name, enabled=enabled))


async def _validate_model(model: str | None) -> None:
    if not model:
        return
    try:
        models = {m.name: m for m in await get_hub().llm().list_models()}
    except ProviderError:
        return  # Ollama offline: allow saving; the model is re-validated when a call starts
    m = models.get(model)
    if m is None:
        raise bad_request(f"Model '{model}' is not installed in Ollama", "model_not_found")
    if not m.is_local and not get_settings().allow_remote_llm_models:
        raise bad_request(f"Model '{model}' runs on a remote host and is not local", "remote_model_refused")


@router.get("")
async def list_agents(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Agent).where(Agent.workspace_id == ctx.id, Agent.deleted_at.is_(None))
                             .order_by(Agent.created_at.desc()))).scalars().all()  # fmt: skip
    stats = dict((await db.execute(select(Call.agent_id, func.count(Call.id)).where(Call.workspace_id == ctx.id)
                                   .group_by(Call.agent_id))).all())  # fmt: skip
    return [agent_out(a, {"calls": stats.get(a.id, 0)}) for a in rows]


@router.get("/tools")
async def tool_catalog(ctx: WorkspaceContext = Depends(ws_viewer)) -> list[dict[str, str]]:
    return _registry.catalog()


@router.post("", status_code=201)
async def create_agent(body: AgentIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    await _validate_model(body.llm.model)
    a = Agent(workspace_id=ctx.id, created_by=ctx.user.id, version=1, tools=[])
    _apply(a, body, ctx.id)
    db.add(a)
    await db.flush()
    db.add(AgentVersion(agent_id=a.id, workspace_id=ctx.id, version=1, snapshot=snapshot(a),
                        note=body.version_note or "Created", created_by=ctx.user.id))  # fmt: skip
    audit(db, "agent.created", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="agent", entity_id=a.id,
          request=request)  # fmt: skip
    await db.commit()
    return agent_out(a)


@router.get("/{agent_id}")
async def get_one(agent_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return agent_out(await get_agent(db, ctx.id, agent_id))


@router.put("/{agent_id}")
async def update_agent(agent_id: str, body: AgentIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    a = await get_agent(db, ctx.id, agent_id)
    if body.llm.model != (a.llm or {}).get("model"):
        await _validate_model(body.llm.model)
    before = snapshot(a)
    _apply(a, body, ctx.id)
    after = snapshot(a)
    if after != before:
        a.version += 1
        db.add(AgentVersion(agent_id=a.id, workspace_id=ctx.id, version=a.version, snapshot=after,
                            note=body.version_note or "Updated", created_by=ctx.user.id))  # fmt: skip
        audit(db, "agent.updated", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="agent", entity_id=a.id,
              details={"version": a.version, "fields": [k for k in after if after[k] != before.get(k)]},
              request=request)  # fmt: skip
    await db.commit()
    return agent_out(a)


@router.post("/{agent_id}/activate")
async def set_active(agent_id: str, active: bool, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    a = await get_agent(db, ctx.id, agent_id)
    a.is_active = active
    audit(db, "agent.activated" if active else "agent.deactivated", user_id=ctx.user.id, workspace_id=ctx.id,
          entity_type="agent", entity_id=a.id, request=request)  # fmt: skip
    await db.commit()
    return agent_out(a)


@router.post("/{agent_id}/duplicate", status_code=201)
async def duplicate(agent_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    src = await get_agent(db, ctx.id, agent_id)
    snap = snapshot(src)
    tools = snap.pop("tools")
    copy = Agent(workspace_id=ctx.id, created_by=ctx.user.id, version=1, tools=[], is_active=False,
                 **{**snap, "name": f"{src.name} (copy)"[:120]})  # fmt: skip
    for name in _registry.names():
        copy.tools.append(AgentTool(workspace_id=ctx.id, tool_name=name, enabled=name in tools))
    db.add(copy)
    await db.flush()
    db.add(AgentVersion(agent_id=copy.id, workspace_id=ctx.id, version=1, snapshot=snapshot(copy),
                        note=f"Duplicated from {src.name} v{src.version}", created_by=ctx.user.id))  # fmt: skip
    audit(db, "agent.duplicated", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="agent", entity_id=copy.id,
          details={"source": src.id}, request=request)  # fmt: skip
    await db.commit()
    return agent_out(copy)


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                       db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    a = await get_agent(db, ctx.id, agent_id)
    running = (await db.execute(select(func.count(Campaign.id)).where(
        Campaign.agent_id == a.id, Campaign.status.in_(("running", "scheduled"))))).scalar_one()  # fmt: skip
    if running:
        raise conflict("This agent is used by an active campaign; pause or cancel it first", "agent_in_use")
    a.deleted_at = utcnow()
    a.is_active = False
    a.public_link_enabled = False
    a.public_token_hash = None
    audit(db, "agent.deleted", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="agent", entity_id=a.id,
          request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}


@router.get("/{agent_id}/versions")
async def versions(agent_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    a = await get_agent(db, ctx.id, agent_id)
    rows = (await db.execute(select(AgentVersion).where(AgentVersion.agent_id == a.id, AgentVersion.workspace_id == ctx.id)
                             .order_by(AgentVersion.version.desc()))).scalars()  # fmt: skip
    return [{"version": v.version, "note": v.note, "created_at": v.created_at, "snapshot": v.snapshot} for v in rows]


@router.post("/{agent_id}/versions/{version}/restore")
async def restore(agent_id: str, version: int, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    a = await get_agent(db, ctx.id, agent_id)
    v = (await db.execute(select(AgentVersion).where(AgentVersion.agent_id == a.id, AgentVersion.version == version,
                                                     AgentVersion.workspace_id == ctx.id))).scalar_one_or_none()  # fmt: skip
    if v is None:
        raise not_found("Version")
    body = AgentIn.model_validate({**v.snapshot, "is_active": a.is_active, "version_note": f"Restored v{version}"})
    return await update_agent(agent_id, body, request, ctx, db)


@router.post("/{agent_id}/public-link")
async def public_link(agent_id: str, enabled: bool, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    """Enable/rotate or disable the customer-facing "talk to us" link for this agent."""
    a = await get_agent(db, ctx.id, agent_id)
    token = None
    if enabled:
        if not get_settings().public_calls_enabled:
            raise APIError(400, "public_calls_disabled",
                           "Public call links are disabled on this installation (set VA_PUBLIC_CALLS_ENABLED=true).")
        token = new_token(24)
        a.public_token_hash = token_hash(token)
        a.public_link_enabled = True
    else:
        a.public_link_enabled = False
        a.public_token_hash = None
    audit(db, "agent.public_link_enabled" if enabled else "agent.public_link_disabled", user_id=ctx.user.id,
          workspace_id=ctx.id, entity_type="agent", entity_id=a.id, request=request)  # fmt: skip
    await db.commit()
    return {"enabled": a.public_link_enabled, "token": token,
            "path": f"/talk/{token}" if token else None}  # fmt: skip


class _DryRunHost:
    """Tool host for the test console: nothing is written to the database."""

    def __init__(self, db: AsyncSession, ws_id: str) -> None:
        self.db, self.ws_id = db, ws_id

    async def create_callback_request(self, preferred_time: str, notes: str, kind: str) -> str:
        return "test-mode-not-saved"

    async def record_opt_out(self, reason: str) -> bool:
        return False

    async def search_knowledge(self, query: str, limit: int = 3) -> list[KnowledgeSnippet]:
        return await search(self.db, self.ws_id, query, limit)

    async def capture_lead(self, details: dict[str, Any]) -> dict[str, Any]:
        return {"contact_id": "test-mode-not-saved", "created": False, "phone_valid": True}


@router.post("/{agent_id}/test")
async def test_agent(agent_id: str, body: TestIn, ctx: WorkspaceContext = Depends(ws_editor),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    """Text test console. Uses the real local model; tool calls run in dry-run mode."""
    a = await get_agent(db, ctx.id, agent_id)
    hub = get_hub()
    cfg = session_config(a, ctx.workspace, hub, channel="voice")
    host = _DryRunHost(db, ctx.id)
    session = ConversationSession(cfg, hub.llm(), _registry, ToolContext(ctx.id, "test", host),
                                  knowledge=(lambda q: search(db, ctx.id, q, 3)) if a.use_knowledge_base else None)  # fmt: skip
    for turn in body.history:
        if turn.role == "user":
            session.add_user(turn.content)
        else:
            session.add_assistant(turn.content)
    try:
        async with hub.ai_slot:
            reply, metrics = await session.respond_text(body.message)
    except ProviderError as exc:
        raise APIError(503, exc.code, str(exc)) from exc
    return {"reply": reply, "metrics": metrics, "tools": session.tool_log, "greeting": session.greeting_text(),
            "end_requested": session.end_requested, "system_prompt": session.system_prompt}  # fmt: skip
