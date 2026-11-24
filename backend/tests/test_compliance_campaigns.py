"""Compliance eligibility engine, persistent scheduler and campaigns (simulation)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_agent, make_contact
from engine.providers.llm.base import LLMError
from engine.providers.llm.scripted import ScriptedLLM
from sqlalchemy import func, select

from app.compliance.policy import CompliancePolicy
from app.compliance.service import check_eligibility, window_status
from app.database.base import utcnow
from app.database.models import Call, Campaign, CampaignContact, Contact, ScheduledJob, SystemState, Workspace
from app.database.session import sessionmaker
from app.scheduler.worker import Scheduler, SchedulerLockedError

OPEN_ALL_DAY = {"calling_window_start": "00:00", "calling_window_end": "23:59", "calling_days": [0, 1, 2, 3, 4, 5, 6]}


async def _policy(c, ws, **over):
    p = (await c.get(f"/api/workspaces/{ws}/compliance")).json()["policy"]
    r = await c.put(f"/api/workspaces/{ws}/compliance", json={**p, **over})
    assert r.status_code == 200, r.text


async def _eligible_contact(c, ws, phone, **kw):
    contact = await make_contact(c, ws, phone, **kw)
    r = await c.post(f"/api/workspaces/{ws}/compliance/consents", json={"contact_id": contact["id"],
                     "purpose": "promotional", "source": "web_form", "evidence": "Signup form, double opt-in"})  # fmt: skip
    assert r.status_code == 201
    await c.post(f"/api/workspaces/{ws}/compliance/dnd-scrub", json={"entries": [{"phone": phone, "status": "not_registered"}]})
    return contact


def test_window_status_and_next_open():
    tz = "Asia/Kolkata"
    # 2026-11-23 is a Monday. 08:00 IST is before a 09:00 window.
    now = datetime(2026, 11, 23, 2, 30, tzinfo=UTC)
    inside, nxt = window_status(now, tz, "09:00", "21:00", [0, 1, 2, 3, 4, 5])
    assert not inside and nxt == datetime(2026, 11, 23, 3, 30, tzinfo=UTC)
    inside, _ = window_status(datetime(2026, 11, 23, 6, 0, tzinfo=UTC), tz, "09:00", "21:00", [0])
    assert inside
    # Sunday is excluded -> next open is Monday 09:00 IST
    inside, nxt = window_status(datetime(2026, 11, 22, 6, 0, tzinfo=UTC), tz, "09:00", "21:00", [0, 1, 2, 3, 4, 5])
    assert not inside and nxt.astimezone(UTC).day == 23


def test_policy_validation():
    with pytest.raises(ValueError):
        CompliancePolicy(calling_window_start="21:00", calling_window_end="09:00")
    with pytest.raises(ValueError):
        CompliancePolicy(max_calls_per_contact_per_day=3, max_calls_per_contact_per_week=1)
    assert len(CompliancePolicy().pstn_prerequisites_missing()) == 4


async def test_eligibility_fails_closed(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    contact = await make_contact(c, ws, "+919812345641")
    check = await c.post(f"/api/workspaces/{ws}/compliance/eligibility", json={"contact_id": contact["id"]})
    res = check.json()
    failed = {x["name"] for x in res["checks"] if not x["passed"]}
    assert res["allowed"] is False and failed == {"consent", "dnd"}
    # a CSV/phone number alone is never consent; adding consent + fresh DND scrub makes it eligible
    contact2 = await _eligible_contact(c, ws, "+919812345642")
    res2 = (await c.post(f"/api/workspaces/{ws}/compliance/eligibility", json={"contact_id": contact2["id"]})).json()
    assert res2["allowed"] is True, res2
    # opt-out wins over consent
    await c.post(f"/api/workspaces/{ws}/compliance/suppression", json={"phone": "+919812345642", "reason": "opt_out"})
    res3 = (await c.post(f"/api/workspaces/{ws}/compliance/eligibility", json={"contact_id": contact2["id"]})).json()
    assert res3["allowed"] is False
    assert {"suppression", "contact_status", "consent"} <= {x["name"] for x in res3["checks"] if not x["passed"]}
    # PSTN is always blocked without real telephony + attestations
    res4 = (await c.post(f"/api/workspaces/{ws}/compliance/eligibility",
                         json={"contact_id": contact["id"], "mode": "PSTN"})).json()  # fmt: skip
    assert any(x["name"] == "pstn_prerequisites" and not x["passed"] for x in res4["checks"])


async def test_dnd_registered_and_stale(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    contact = await _eligible_contact(c, ws, "+919812345643")
    await c.post(f"/api/workspaces/{ws}/compliance/dnd-scrub", json={"entries": [{"phone": "+919812345643", "status": "registered"}]})
    res = (await c.post(f"/api/workspaces/{ws}/compliance/eligibility", json={"contact_id": contact["id"]})).json()
    assert any(x["name"] == "dnd" and not x["passed"] for x in res["checks"])
    stale = (utcnow() - timedelta(days=90)).isoformat()
    await c.post(f"/api/workspaces/{ws}/compliance/dnd-scrub", json={"entries": [{"phone": "+919812345643", "status": "not_registered"}],
                                                                      "checked_at": stale})  # fmt: skip
    res = (await c.post(f"/api/workspaces/{ws}/compliance/eligibility", json={"contact_id": contact["id"]})).json()
    assert any(x["name"] == "dnd" and not x["passed"] and "older" in x["detail"] for x in res["checks"])


async def test_frequency_and_consent_expiry(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    contact = await _eligible_contact(c, ws, "+919812345644")
    async with sessionmaker()() as db:
        db.add(Call(workspace_id=ws, mode="SIMULATION", to_number="+919812345644", status="completed", started_at=utcnow()))
        await db.commit()
        workspace = await db.get(Workspace, ws)
        model = await db.get(Contact, contact["id"])
        res = await check_eligibility(db, workspace, model, purpose="promotional", mode="SIMULATION")
        assert any(ch.name == "frequency_day" and not ch.passed for ch in res.checks)
        # other modes are counted separately
        res = await check_eligibility(db, workspace, model, purpose="promotional", mode="PSTN")
        assert all(ch.passed for ch in res.checks if ch.name.startswith("frequency"))
    # expired consent is not consent
    c2 = await make_contact(c, ws, "+919812345645")
    await c.post(f"/api/workspaces/{ws}/compliance/consents", json={"contact_id": c2["id"], "purpose": "promotional",
                 "source": "written", "evidence": "paper form", "obtained_at": "2025-01-01T00:00:00Z",
                 "expires_at": "2025-06-01T00:00:00Z"})  # fmt: skip
    res = (await c.post(f"/api/workspaces/{ws}/compliance/eligibility", json={"contact_id": c2["id"]})).json()
    assert any(x["name"] == "consent" and not x["passed"] for x in res["checks"])


async def _campaign(c, ws, agent_id, contact_ids, **over):
    body = {"name": "Diwali offer", "agent_id": agent_id, "objective": "Offer the festive hosting discount",
            "window_start": "00:00", "window_end": "23:59", "calling_days": [0, 1, 2, 3, 4, 5, 6],
            "max_attempts": 2, "retry_delay_minutes": 1, "max_concurrent_calls": 2, "contact_ids": contact_ids,
            "simulation": {"answer_rate": 1.0, "busy_rate": 0, "fail_rate": 0,
                           "persona_weights": {"interested": 1, "callback": 1, "opt_out": 1}}, **over}  # fmt: skip
    r = await c.post(f"/api/workspaces/{ws}/campaigns", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _drain(sched: Scheduler, ticks: int = 40) -> None:
    import asyncio

    for _ in range(ticks):
        await sched.tick()
        if sched.running:
            await asyncio.gather(*list(sched.running.values()), return_exceptions=True)


async def test_campaign_runs_in_simulation(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    agent = await make_agent(c, ws)
    good = [await _eligible_contact(c, ws, f"+91981234565{i}", first_name=f"G{i}") for i in range(4)]
    blocked = await make_contact(c, ws, "+919812345659", first_name="NoConsent")
    camp = await _campaign(c, ws, agent["id"], [x["id"] for x in good] + [blocked["id"]])
    assert camp["mode"] == "SIMULATION" and camp["progress"]["total"] == 5
    preview = (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/preview")).json()
    assert preview["eligible_now"] == 4 and preview["blocked_by"]["consent"] == 1
    assert (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/start")).json()["status"] == "running"
    sched = Scheduler(worker_id="test-worker")
    await sched.acquire_lock()
    await _drain(sched)
    detail = (await c.get(f"/api/workspaces/{ws}/campaigns/{camp['id']}")).json()
    assert detail["status"] == "completed", detail
    assert detail["progress"].get("completed") == 4 and detail["progress"].get("skipped") == 1
    calls = (await c.get(f"/api/workspaces/{ws}/calls", params={"campaign_id": camp["id"]})).json()["items"]
    assert len(calls) == 4 and all(x["mode"] == "SIMULATION" and x["is_simulated"] for x in calls)
    assert all(x["outcome"] for x in calls)
    report = (await c.get(f"/api/workspaces/{ws}/campaigns/{camp['id']}/report")).json()
    assert report["calls"] == 4 and report["connected"] == 4
    # simulated opt-outs never suppress real numbers
    assert (await c.get(f"/api/workspaces/{ws}/compliance/suppression")).json()["total"] == 0
    # the blocked contact's reason is recorded
    skipped = (await c.get(f"/api/workspaces/{ws}/campaigns/{camp['id']}/contacts", params={"status": "skipped"})).json()
    assert "consent" in skipped["items"][0]["skip_reason"]


async def test_concurrency_limit_respected(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    agent = await make_agent(c, ws)
    contacts = [await _eligible_contact(c, ws, f"+91981234566{i}") for i in range(5)]
    camp = await _campaign(c, ws, agent["id"], [x["id"] for x in contacts], max_concurrent_calls=2)
    await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/start")
    sched = Scheduler(worker_id="t2")
    await sched.acquire_lock()
    async with sessionmaker()() as db:
        await sched._enqueue(db)
        await db.commit()
        n = (await db.execute(select(func.count(ScheduledJob.id)).where(ScheduledJob.campaign_id == camp["id"]))).scalar_one()
    assert n == 2
    await _drain(sched)
    async with sessionmaker()() as db:
        keys = (await db.execute(select(ScheduledJob.dedupe_key))).scalars().all()
    assert len(keys) == len(set(keys)) == 5


async def test_restart_recovery_without_duplicates(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    agent = await make_agent(c, ws)
    contacts = [await _eligible_contact(c, ws, f"+91981234567{i}") for i in range(2)]
    camp = await _campaign(c, ws, agent["id"], [x["id"] for x in contacts], max_concurrent_calls=1)
    await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/start")
    dead = Scheduler(worker_id="dead-worker")
    async with sessionmaker()() as db:
        await dead._enqueue(db)
        await db.commit()
    # simulate a crash: job claimed and a call started, then the process died
    async with sessionmaker()() as db:
        job = (await db.execute(select(ScheduledJob))).scalar_one()
        job.status, job.locked_by, job.locked_until = "running", "dead-worker", utcnow() + timedelta(minutes=10)
        db.add(Call(workspace_id=ws, mode="SIMULATION", status="in_progress", job_id=job.id, started_at=utcnow()))
        db.add(SystemState(key="scheduler", value={"worker_id": "dead-worker", "state": "running",
                                                   "heartbeat_at": (utcnow() - timedelta(minutes=5)).isoformat()}))  # fmt: skip
        await db.commit()
    fresh = Scheduler(worker_id="fresh-worker")
    await fresh.acquire_lock()  # stale heartbeat -> lock can be taken
    assert await fresh.recover() == 1
    await _drain(fresh)
    async with sessionmaker()() as db:
        jobs = (await db.execute(select(ScheduledJob))).scalars().all()
        interrupted = (await db.execute(select(Call).where(Call.end_reason == "interrupted_by_restart"))).scalars().all()
        camp_row = await db.get(Campaign, camp["id"])
    assert len(jobs) == 2 and len({j.dedupe_key for j in jobs}) == 2
    assert all(j.status == "done" for j in jobs)
    assert len(interrupted) == 1
    assert camp_row.status == "completed"


async def test_second_scheduler_refused_while_first_alive(db_url, hub):
    a = Scheduler(worker_id="a")
    await a.acquire_lock()
    b = Scheduler(worker_id="b")
    with pytest.raises(SchedulerLockedError):
        await b.acquire_lock()


async def test_outside_window_waits_and_pause_resume_cancel(owner):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, calling_window_start="09:00", calling_window_end="21:00", calling_days=[0, 1, 2, 3, 4, 5, 6])
    agent = await make_agent(c, ws)
    contact = await _eligible_contact(c, ws, "+919812345681")
    # a window that never overlaps the workspace window is refused at start
    bad = await _campaign(c, ws, agent["id"], [contact["id"]], window_start="00:00", window_end="00:01")
    r = await c.post(f"/api/workspaces/{ws}/campaigns/{bad['id']}/start")
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_calling_window"
    # valid window, but today is not a calling day -> contact waits (not skipped)
    from zoneinfo import ZoneInfo

    today = utcnow().astimezone(ZoneInfo("Asia/Kolkata")).weekday()
    camp = await _campaign(c, ws, agent["id"], [contact["id"]], window_start="09:00", window_end="21:00",
                           calling_days=[d for d in range(7) if d != today])  # fmt: skip
    assert (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/start")).status_code == 200
    sched = Scheduler(worker_id="t3")
    await sched.acquire_lock()
    await sched.tick()
    items = (await c.get(f"/api/workspaces/{ws}/campaigns/{camp['id']}/contacts")).json()["items"]
    assert items[0]["status"] == "pending" and items[0]["skip_reason"] == "Waiting for calling window"
    assert items[0]["next_attempt_at"] is not None
    assert (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/pause")).json()["status"] == "paused"
    assert (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/resume")).json()["status"] == "running"
    assert (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/cancel")).json()["status"] == "cancelled"
    assert (await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/start")).status_code == 409


async def test_pstn_campaign_refused_by_default(owner):
    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)
    r = await c.post(f"/api/workspaces/{ws}/campaigns", json={"name": "Real", "agent_id": agent["id"], "mode": "PSTN"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "pstn_disabled"


async def test_provider_outage_auto_pauses_without_burning_attempts(owner, hub):
    c, ws = owner["client"], owner["ws"]
    await _policy(c, ws, **OPEN_ALL_DAY)
    agent = await make_agent(c, ws)
    contacts = [await _eligible_contact(c, ws, f"+91981234569{i}") for i in range(3)]
    camp = await _campaign(c, ws, agent["id"], [x["id"] for x in contacts], max_concurrent_calls=3)
    await c.post(f"/api/workspaces/{ws}/campaigns/{camp['id']}/start")
    hub.overrides["llm"] = ScriptedLLM(fail_with=LLMError("Ollama is not running", code="llm_unavailable"))
    sched = Scheduler(worker_id="t4")
    await sched.acquire_lock()
    await _drain(sched, ticks=3)
    detail = (await c.get(f"/api/workspaces/{ws}/campaigns/{camp['id']}")).json()
    assert detail["status"] == "paused" and "provider" in detail["status_reason"]
    async with sessionmaker()() as db:
        attempts = (await db.execute(select(CampaignContact.attempts))).scalars().all()
    assert attempts == [0, 0, 0]
