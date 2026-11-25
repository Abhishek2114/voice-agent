"""Calls, simulation, callbacks, leads, analytics, system endpoints and backup/restore."""

from __future__ import annotations

import asyncio
import io
import tarfile

import pytest
from conftest import make_agent, make_contact
from engine.conversation.tools import ToolContext
from engine.providers.llm.base import ToolCall
from engine.providers.llm.scripted import ScriptedLLM

from app.calls.service import DbToolHost, create_call
from app.core.config import get_settings
from app.database.models import Agent, Contact
from app.database.session import sessionmaker
from app.system import backup as backups


async def _wait_call(c, ws, call_id, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        d = (await c.get(f"/api/workspaces/{ws}/calls/{call_id}")).json()
        if d["status"] in ("completed", "failed", "no_answer", "busy"):
            return d
        await asyncio.sleep(0.05)
    raise AssertionError("call did not finish")


async def test_simulated_call_produces_transcript_summary_outcome(owner):
    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)
    contact = await make_contact(c, ws)
    r = await c.post(f"/api/workspaces/{ws}/calls/simulate", json={"agent_id": agent["id"], "contact_id": contact["id"],
                                                                    "persona": "interested"})  # fmt: skip
    assert r.status_code == 202 and r.json()["mode"] == "SIMULATION"
    d = await _wait_call(c, ws, r.json()["call_id"])
    assert d["status"] == "completed" and d["is_simulated"] and d["outcome"] == "interested"
    assert d["summary"] and d["duration_seconds"] > 0
    roles = [m["role"] for m in d["messages"]]
    assert roles[0] == "assistant" and roles.count("user") >= 3
    assert d["messages"][0]["content"].startswith("Hello, this is an AI assistant calling on behalf of")
    # simulated outcomes never change the real contact's CRM status
    assert (await c.get(f"/api/workspaces/{ws}/contacts/{contact['id']}")).json()["status"] == "new"
    listing = (await c.get(f"/api/workspaces/{ws}/calls", params={"mode": "SIMULATION", "q": "hosting"})).json()
    assert listing["total"] >= 1
    export = await c.get(f"/api/workspaces/{ws}/calls/export", params={"include_transcripts": True})
    assert export.status_code == 200 and "SIMULATION" in export.text


async def test_simulated_inbound_call(owner):
    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)
    r = await c.post(f"/api/workspaces/{ws}/telephony/simulate-inbound", json={"agent_id": agent["id"], "persona": "questions"})
    assert r.status_code == 202
    d = await _wait_call(c, ws, r.json()["call_id"])
    assert d["direction"] == "inbound" and d["mode"] == "SIMULATION" and d["status"] == "completed"
    status = (await c.get(f"/api/workspaces/{ws}/telephony/status")).json()
    assert status["modes"]["PSTN"] == "disabled" and status["real_telephony_enabled"] is False


async def test_lead_capture_from_real_conversation(owner):
    """A real (WEBRTC) conversation creates a lead, links the call and records verbal consent."""
    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    async with sessionmaker()() as db:
        agent = await db.get(Agent, a["id"])
        call = await create_call(db, ws_id=ws, agent=agent, mode="WEBRTC", direction="inbound", channel="public_link")
        await db.commit()
        call_id = call.id
    host = DbToolHost(ws, call_id, "WEBRTC", None, "IN", "public_link")
    res = await host.capture_lead({"name": "Meera Iyer", "phone": "98123 45690", "email": "meera@example.com",
                                   "requirement": "Managed VPS for an online store", "consent_to_contact": True})  # fmt: skip
    assert res["created"] and res["consent_recorded"]
    again = await host.capture_lead({"name": "Meera", "phone": "+919812345690"})
    assert again["contact_id"] == res["contact_id"] and not again["created"]  # deduplicated by phone
    contact = (await c.get(f"/api/workspaces/{ws}/contacts/{res['contact_id']}")).json()
    assert contact["status"] == "lead" and contact["source"] == "public_link" and "ai-lead" in contact["tags"]
    assert "Managed VPS" in contact["notes"]
    consents = (await c.get(f"/api/workspaces/{ws}/compliance/consents", params={"contact_id": contact["id"]})).json()
    assert consents["items"][0]["purpose"] == "inquiry_followup" and consents["items"][0]["call_id"] == call_id
    assert (await c.get(f"/api/workspaces/{ws}/calls/{call_id}")).json()["contact_id"] == contact["id"]


async def test_opt_out_during_real_call_suppresses_number(owner):
    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    contact = await make_contact(c, ws, "+919812345691")
    async with sessionmaker()() as db:
        agent = await db.get(Agent, a["id"])
        ct = await db.get(Contact, contact["id"])
        call = await create_call(db, ws_id=ws, agent=agent, mode="PSTN", direction="outbound", channel="campaign", contact=ct)
        await db.commit()
    host = DbToolHost(ws, call.id, "PSTN", contact["id"], "IN", "campaign")
    assert await host.record_opt_out("asked not to be called") is True
    sup = (await c.get(f"/api/workspaces/{ws}/compliance/suppression")).json()["items"]
    assert sup[0]["phone_e164"] == "+919812345691" and sup[0]["reason"] == "opt_out"
    assert (await c.get(f"/api/workspaces/{ws}/contacts/{contact['id']}")).json()["status"] == "do_not_call"


async def test_callback_requests_flow(owner, hub):
    c, ws = owner["client"], owner["ws"]
    calls = iter([("", [ToolCall("request_callback", {"preferred_time": "tomorrow 6 pm"})]), "Sure, the team will call you."])
    hub.overrides["llm"] = ScriptedLLM(lambda m: next(calls, "Okay, goodbye."), json_result={"outcome": "callback_requested",
                                       "summary": "Callback requested.", "sentiment": "neutral"})  # fmt: skip
    a = await make_agent(c, ws)
    r = await c.post(f"/api/workspaces/{ws}/calls/simulate", json={"agent_id": a["id"], "persona": "callback"})
    d = await _wait_call(c, ws, r.json()["call_id"])
    assert d["outcome"] == "callback_requested" and d["callbacks"][0]["preferred_time"] == "tomorrow 6 pm"
    assert (await c.get(f"/api/workspaces/{ws}/callbacks")).json()["total"] == 0  # simulated hidden by default
    cbs = (await c.get(f"/api/workspaces/{ws}/callbacks", params={"include_simulated": True})).json()
    assert cbs["items"][0]["is_simulated"] is True
    r = await c.patch(f"/api/workspaces/{ws}/callbacks/{cbs['items'][0]['id']}", json={"status": "done"})
    assert r.json()["status"] == "done"


async def test_analytics_reflect_database(owner):
    c, ws = owner["client"], owner["ws"]
    empty = (await c.get(f"/api/workspaces/{ws}/analytics/overview")).json()
    assert empty["total_calls"] == 0 and empty["recent_calls"] == []
    a = await make_agent(c, ws)
    for persona in ("interested", "questions"):
        r = await c.post(f"/api/workspaces/{ws}/calls/simulate", json={"agent_id": a["id"], "persona": persona})
        await _wait_call(c, ws, r.json()["call_id"])
    ov = (await c.get(f"/api/workspaces/{ws}/analytics/overview")).json()
    assert ov["total_calls"] == 2 and ov["successful_calls"] == 2 and ov["calls_by_mode"] == {"SIMULATION": 2}
    rep = (await c.get(f"/api/workspaces/{ws}/analytics/report", params={"days": 7})).json()
    assert rep["totals"]["calls"] == 2 and sum(o["count"] for o in rep["outcomes"]) == 2
    assert rep["agents"][0]["calls"] == 2 and sum(b["count"] for b in rep["duration_distribution"]) == 2
    only_pstn = (await c.get(f"/api/workspaces/{ws}/analytics/report", params={"mode": "PSTN"})).json()
    assert only_pstn["totals"]["calls"] == 0


async def test_system_health_and_providers(owner):
    c = owner["client"]
    h = (await c.get("/api/system/health")).json()
    assert h["database"]["status"] == "ok" and h["llm"]["status"] == "ok"
    assert h["scheduler"]["status"] == "unavailable"  # not running in tests
    models = (await c.get("/api/system/models")).json()
    assert models["available"] and models["models"][0]["is_local"]
    # SSRF: provider URLs must be local
    for url in ("http://169.254.169.254/latest/meta-data", "http://8.8.8.8:11434", "file:///etc/passwd",
                "http://user:pw@127.0.0.1:11434", "http://127.0.0.1:11434/?x=1"):  # fmt: skip
        r = await c.put("/api/system/providers/llm", json={"provider": "ollama", "config": {"base_url": url}})
        assert r.status_code == 400, url
    ok = await c.put("/api/system/providers/llm", json={"provider": "ollama", "config": {"base_url": "http://127.0.0.1:11434"}})
    assert ok.status_code == 200
    r = await c.put("/api/system/providers/telephony", json={"provider": "asterisk_ari", "enabled": True,
                    "config": {"base_url": "http://127.0.0.1:8088"}, "secrets": {"password": "x"}})  # fmt: skip
    assert r.status_code == 400 and r.json()["error"]["code"] == "pstn_disabled"
    r = await c.put("/api/system/providers/telephony", json={"provider": "asterisk_ari", "enabled": False,
                    "config": {"base_url": "http://127.0.0.1:8088", "username": "va"}, "secrets": {"password": "s3cret"}})  # fmt: skip
    assert r.status_code == 200
    prov = (await c.get("/api/system/providers")).json()
    assert prov["telephony"]["has_secrets"] is True and "password" not in str(prov["telephony"]["config"])


async def test_backup_create_verify_restore(owner, tmp_path):
    c = owner["client"]
    settings = get_settings()
    # point backups at the per-test database
    from app.database.session import get_engine

    db_file = str(get_engine().url).split("///", 1)[1]
    s = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{db_file}", "data_dir": tmp_path})
    (tmp_path / "recordings" / "w").mkdir(parents=True)
    (tmp_path / "recordings" / "w" / "r.wav").write_bytes(b"RIFF....fake")
    s.ensure_dirs()
    plain = backups.create_backup(s)
    enc = backups.create_backup(s, passphrase="a-long-passphrase")
    assert oct(plain.stat().st_mode & 0o777) == "0o600"
    info = backups.verify_backup(plain.read_bytes())
    assert info["files"] == 2 and info["schema_revision"] == "0001"
    with pytest.raises(backups.BackupError):
        backups.verify_backup(enc.read_bytes())
    with pytest.raises(backups.BackupError):
        backups.verify_backup(enc.read_bytes(), "wrong-passphrase!")
    assert backups.verify_backup(enc.read_bytes(), "a-long-passphrase")["encrypted"] is True
    tampered = bytearray(plain.read_bytes())
    tampered[len(tampered) // 2] ^= 0xFF
    with pytest.raises(backups.BackupError):
        backups.verify_backup(bytes(tampered))
    # path traversal in archive is refused
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        ti = tarfile.TarInfo("../../evil.sh")
        ti.size = 2
        tar.addfile(ti, io.BytesIO(b"hi"))
    with pytest.raises(backups.BackupError):
        backups.verify_backup(buf.getvalue())
    staged = backups.stage_restore(plain.read_bytes(), settings=s)
    assert staged["files"] == 2
    s2 = s.model_copy(update={"database_url": f"sqlite+aiosqlite:///{tmp_path / 'restored.db'}"})
    applied = backups.apply_pending_restore(s2)
    assert applied and (tmp_path / "restored.db").exists() and (tmp_path / "recordings" / "w" / "r.wav").exists()
    # API: listing and restore staging require instance admin
    r = await c.post("/api/system/backups/restore", files={"file": ("x.tar.gz", b"garbage", "application/gzip")})
    assert r.status_code == 400
    assert (await c.get("/api/system/backups/..%2F..%2Fetc%2Fpasswd")).status_code == 404


async def test_call_in_progress_cannot_be_deleted(owner):
    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    async with sessionmaker()() as db:
        call = await create_call(db, ws_id=ws, agent=await db.get(Agent, a["id"]), mode="SIMULATION",
                                 direction="outbound", channel="simulation", status="dialing")  # fmt: skip
        await db.commit()
    r = await c.delete(f"/api/workspaces/{ws}/calls/{call.id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "call_in_progress"
    async with sessionmaker()() as db:
        (await db.get(type(call), call.id)).status = "completed"
        await db.commit()
    assert (await c.delete(f"/api/workspaces/{ws}/calls/{call.id}")).status_code == 200


async def test_tool_host_simulation_never_writes_leads(owner):
    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    async with sessionmaker()() as db:
        agent = await db.get(Agent, a["id"])
        call = await create_call(db, ws_id=ws, agent=agent, mode="SIMULATION", direction="outbound", channel="simulation")
        await db.commit()
    host = DbToolHost(ws, call.id, "SIMULATION", None, "IN", "simulation")
    res = await host.capture_lead({"name": "Fake Person", "phone": "+919812345692"})
    assert res["simulated"] is True
    assert (await c.get(f"/api/workspaces/{ws}/contacts")).json()["total"] == 0
    assert isinstance(ToolContext(ws, call.id, host), ToolContext)


async def test_retention_clears_old_personal_data(owner):
    from datetime import timedelta

    from sqlalchemy import select

    from app.database.base import utcnow
    from app.database.models import Call, Conversation, Transcript
    from app.scheduler.worker import Scheduler

    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    r = await c.post(f"/api/workspaces/{ws}/calls/simulate", json={"agent_id": a["id"], "persona": "interested"})
    call_id = r.json()["call_id"]
    await _wait_call(c, ws, call_id)
    async with sessionmaker()() as db:
        call = await db.get(Call, call_id)
        old = utcnow() - timedelta(days=400)
        call.created_at = old
        conv = (await db.execute(select(Conversation).where(Conversation.call_id == call_id))).scalar_one()
        conv.created_at = old
        await db.commit()
    await Scheduler(worker_id="ret").apply_retention()
    async with sessionmaker()() as db:
        call = await db.get(Call, call_id)
        conv = (await db.execute(select(Conversation).where(Conversation.call_id == call_id))).scalar_one()
        assert call.summary is None and conv.tool_log == [] and conv.system_prompt == ""
        assert (await db.execute(select(Transcript).where(Transcript.call_id == call_id))).scalar_one_or_none() is None
