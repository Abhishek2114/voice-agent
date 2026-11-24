"""Workspace export/import (portable JSON). Imports always create a *new* workspace
owned by the importing user; imported campaigns are drafts in SIMULATION mode."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from engine.conversation.tools import default_registry
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime import snapshot
from app.compliance.policy import CompliancePolicy, policy_for
from app.contacts.csv_safety import neutralize
from app.contacts.phone import normalize_phone
from app.core.errors import bad_request
from app.database.base import utcnow
from app.database.models import (
    Agent,
    AgentTool,
    AgentVersion,
    Campaign,
    CampaignContact,
    ConsentRecord,
    Contact,
    KnowledgeDocument,
    SuppressionEntry,
    User,
    Workspace,
)
from app.knowledge.service import KINDS, faq_to_text, index_document
from app.workspaces.service import create_workspace

FORMAT = "voice-agent.workspace"
TOOLS = set(default_registry().names())


class _Profile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    brand_color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=1000)
    timezone: str = "Asia/Kolkata"
    default_country: str = Field(default="IN", pattern=r"^[A-Z]{2}$")


class _Knowledge(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: str = "document"
    content: str = Field(default="", max_length=500_000)
    faq: list[dict[str, str]] | None = None


class _Contact(BaseModel):
    ref: str | None = None
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    company: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=5000)
    status: str = "new"
    dnd_status: str = "unknown"
    is_fictional: bool = False
    consents: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class _Campaign(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    agent: str
    description: str = ""
    objective: str = ""
    purpose: str = "promotional"
    window_start: str = "10:00"
    window_end: str = "19:00"
    calling_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_delay_minutes: int = Field(default=60, ge=1, le=10_080)
    max_concurrent_calls: int = Field(default=1, ge=1, le=20)
    simulation: dict[str, Any] = Field(default_factory=dict)
    contact_tag: str | None = None


class _Export(BaseModel):
    format: str
    version: int = 1
    workspace: _Profile
    compliance: dict[str, Any] = Field(default_factory=dict)
    agents: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    knowledge: list[_Knowledge] = Field(default_factory=list, max_length=2000)
    contacts: list[_Contact] = Field(default_factory=list, max_length=50_000)
    suppression: list[dict[str, str]] = Field(default_factory=list, max_length=50_000)
    campaigns: list[_Campaign] = Field(default_factory=list, max_length=200)


async def export_workspace(db: AsyncSession, ws_id: str, *, include_contacts: bool = True) -> dict[str, Any]:
    ws = await db.get(Workspace, ws_id)
    assert ws is not None
    agents = (await db.execute(select(Agent).where(Agent.workspace_id == ws_id, Agent.deleted_at.is_(None)))).scalars().all()
    docs = (await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.workspace_id == ws_id))).scalars().all()
    out: dict[str, Any] = {
        "format": FORMAT, "version": 1, "exported_at": utcnow().isoformat(),
        "workspace": {k: getattr(ws, k) for k in _Profile.model_fields},
        "compliance": policy_for(ws.compliance).model_dump(),
        "agents": [snapshot(a) for a in agents],
        "knowledge": [{"title": d.title, "kind": d.kind, "content": d.content} for d in docs],
        "campaigns": [], "contacts": [], "suppression": [],
    }  # fmt: skip
    names = {a.id: a.name for a in agents}
    for c in (await db.execute(select(Campaign).where(Campaign.workspace_id == ws_id))).scalars():
        out["campaigns"].append({"name": c.name, "agent": names.get(c.agent_id, ""), "description": c.description,
                                 "objective": c.objective, "purpose": c.purpose, "window_start": c.window_start,
                                 "window_end": c.window_end, "calling_days": c.calling_days,
                                 "max_attempts": c.max_attempts, "retry_delay_minutes": c.retry_delay_minutes,
                                 "max_concurrent_calls": c.max_concurrent_calls, "simulation": c.simulation})  # fmt: skip
    if include_contacts:
        contacts = (await db.execute(select(Contact).where(Contact.workspace_id == ws_id, Contact.deleted_at.is_(None)))).scalars().all()
        consents = (await db.execute(select(ConsentRecord).where(ConsentRecord.workspace_id == ws_id,
                    ConsentRecord.revoked_at.is_(None)))).scalars().all()  # fmt: skip
        by_contact: dict[str, list[dict[str, Any]]] = {}
        for r in consents:
            if r.contact_id:
                by_contact.setdefault(r.contact_id, []).append({"purpose": r.purpose, "source": r.source,
                    "evidence": r.evidence, "obtained_at": r.obtained_at.isoformat(),
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None})  # fmt: skip
        out["contacts"] = [{"ref": c.id, "first_name": c.first_name, "last_name": c.last_name, "phone": c.phone_e164,
                            "email": c.email, "company": c.company, "tags": c.tags, "notes": c.notes,
                            "status": c.status, "dnd_status": c.dnd_status, "is_fictional": c.is_fictional,
                            "consents": by_contact.get(c.id, [])} for c in contacts]  # fmt: skip
        out["suppression"] = [{"phone": s.phone_e164, "reason": s.reason} for s in (await db.execute(
            select(SuppressionEntry).where(SuppressionEntry.workspace_id == ws_id))).scalars()]  # fmt: skip
    return out


async def import_workspace(db: AsyncSession, user: User, raw: Any) -> tuple[Workspace, dict[str, int]]:
    try:
        data = _Export.model_validate(raw)
    except ValidationError as exc:
        raise bad_request(f"Invalid workspace file: {exc.errors()[0]['msg']}", "invalid_file") from exc
    if data.format != FORMAT:
        raise bad_request("Not a voice-agent workspace export", "invalid_file")
    try:
        policy = CompliancePolicy.model_validate(data.compliance) if data.compliance else CompliancePolicy()
    except ValidationError:
        policy = CompliancePolicy()
    # attestations never travel between installations
    policy.pstn_authorized, policy.a2p_declared_to_provider = False, False
    ws = await create_workspace(db, user, **data.workspace.model_dump())
    ws.compliance = policy.model_dump()
    report = {"agents": 0, "knowledge": 0, "contacts": 0, "consents": 0, "suppression": 0, "campaigns": 0, "skipped": 0}
    agents_by_name: dict[str, Agent] = {}
    for a in data.agents:
        tools = [t for t in a.get("tools", []) if t in TOOLS]
        fields = {k: a[k] for k in ("name", "description", "personality", "system_instructions", "greeting",
                                    "objectives", "business_knowledge", "language", "llm", "stt", "tts",
                                    "max_duration_seconds", "silence_timeout_seconds", "end_rules",
                                    "fallback_responses", "use_knowledge_base") if k in a}  # fmt: skip
        if not str(fields.get("name", "")).strip():
            report["skipped"] += 1
            continue
        fields["name"] = str(fields["name"])[:120]
        agent = Agent(workspace_id=ws.id, created_by=user.id, version=1, tools=[], is_active=True, **fields)
        for t in TOOLS:
            agent.tools.append(AgentTool(workspace_id=ws.id, tool_name=t, enabled=t in tools))
        db.add(agent)
        await db.flush()
        db.add(AgentVersion(agent_id=agent.id, workspace_id=ws.id, version=1, snapshot=snapshot(agent),
                            note="Imported", created_by=user.id))  # fmt: skip
        agents_by_name[agent.name] = agent
        report["agents"] += 1
    for k in data.knowledge:
        content = faq_to_text(k.faq) if k.faq else k.content
        if not content.strip():
            report["skipped"] += 1
            continue
        doc = KnowledgeDocument(workspace_id=ws.id, title=k.title, kind=k.kind if k.kind in KINDS else "document",
                                content=content, size_bytes=len(content.encode()), mime_type="text/plain")  # fmt: skip
        db.add(doc)
        await db.flush()
        await index_document(db, doc)
        report["knowledge"] += 1
    seen: set[str] = set()
    for c in data.contacts:
        phone = normalize_phone(c.phone, ws.default_country) if c.phone else None
        e164 = phone.e164 if phone and phone.valid else None
        if e164 and e164 in seen:
            report["skipped"] += 1
            continue
        if e164:
            seen.add(e164)
        contact = Contact(workspace_id=ws.id, first_name=neutralize(c.first_name), last_name=neutralize(c.last_name),
                          phone_raw=c.phone, phone_e164=e164, email=(c.email or "").lower() or None,
                          company=neutralize(c.company) if c.company else None, tags=[t.lower()[:40] for t in c.tags],
                          notes=c.notes, status=c.status if c.status in ("new", "lead", "contacted", "interested",
                          "not_interested", "callback", "converted", "do_not_call", "wrong_number") else "new",
                          dnd_status=c.dnd_status if c.dnd_status in ("unknown", "registered", "not_registered") else "unknown",
                          dnd_checked_at=utcnow() if c.dnd_status in ("registered", "not_registered") else None,
                          source="workspace_import", is_fictional=c.is_fictional, custom_fields=[])  # fmt: skip
        db.add(contact)
        await db.flush()
        report["contacts"] += 1
        for cons in c.consents:
            try:
                obtained = datetime.fromisoformat(str(cons["obtained_at"]))
                expires = datetime.fromisoformat(str(cons["expires_at"])) if cons.get("expires_at") else None
                db.add(ConsentRecord(workspace_id=ws.id, contact_id=contact.id, phone_e164=e164,
                                     purpose=str(cons["purpose"]), source=str(cons["source"]),
                                     evidence=f"[imported] {str(cons.get('evidence', ''))[:1900]}",
                                     obtained_at=obtained, expires_at=expires, recorded_by=user.id))  # fmt: skip
                report["consents"] += 1
            except (KeyError, ValueError):
                report["skipped"] += 1
    for s in data.suppression:
        phone = normalize_phone(s.get("phone"), ws.default_country)
        if phone.e164:
            db.add(SuppressionEntry(workspace_id=ws.id, phone_e164=phone.e164, reason=s.get("reason", "manual")[:24],
                                    source="workspace_import", created_by=user.id))  # fmt: skip
            report["suppression"] += 1
    await db.flush()
    for camp in data.campaigns:
        camp_agent = agents_by_name.get(camp.agent)
        if camp_agent is None:
            report["skipped"] += 1
            continue
        new_camp = Campaign(workspace_id=ws.id, agent_id=camp_agent.id, name=camp.name, description=camp.description,
                     objective=camp.objective, purpose=camp.purpose if camp.purpose in ("promotional", "service",
                     "transactional", "inquiry_followup") else "promotional", mode="SIMULATION", status="draft",
                     window_start=camp.window_start, window_end=camp.window_end, calling_days=camp.calling_days,
                     max_attempts=camp.max_attempts, retry_delay_minutes=camp.retry_delay_minutes,
                     max_concurrent_calls=camp.max_concurrent_calls, simulation=camp.simulation,
                     created_by=user.id)  # fmt: skip
        db.add(new_camp)
        await db.flush()
        if camp.contact_tag:
            ids = (await db.execute(select(Contact.id).where(Contact.workspace_id == ws.id,
                   cast(Contact.tags, String).like(f'%"{camp.contact_tag.lower()}"%')))).scalars().all()  # fmt: skip
            for cid in ids:
                db.add(CampaignContact(campaign_id=new_camp.id, contact_id=cid, workspace_id=ws.id))
        report["campaigns"] += 1
    await db.flush()
    return ws, report
