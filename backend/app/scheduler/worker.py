"""Persistent campaign scheduler backed by SQLite.

Guarantees:
* **No duplicate jobs** — every job has a unique ``dedupe_key`` (campaign-contact + attempt
  number) inserted with ``ON CONFLICT DO NOTHING``.
* **Restart safety** — a single scheduler holds a heartbeat lock in ``system_state``; on
  start-up it re-queues jobs left ``running`` by a dead process and marks their
  half-finished calls as failed ("interrupted by restart").
* **Atomic claims** — jobs are claimed with ``UPDATE … WHERE status='pending'``.
* **Concurrency** — per-campaign ``max_concurrent_calls`` and the global telephony limit
  are both enforced; AI inference is separately limited by the provider hub semaphore.
* **Fail closed** — every call passes the compliance eligibility engine first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from engine.providers.base import ProviderError
from engine.providers.telephony.simulator import SimulationScenario, pick_persona
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.calls.service import run_simulated_call
from app.compliance.policy import policy_for
from app.compliance.service import check_eligibility
from app.core.audit import audit, system_event
from app.core.config import get_settings
from app.database.base import new_id, utcnow
from app.database.models import (
    Call,
    Campaign,
    CampaignContact,
    Conversation,
    Message,
    Recording,
    ScheduledJob,
    SystemState,
    Transcript,
    Workspace,
)
from app.database.session import sessionmaker

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = {"no_answer", "busy", "failed"}
LOCK_KEY = "scheduler"
HEARTBEAT_STALE_S = 20


class SchedulerLockedError(RuntimeError):
    pass


class Scheduler:
    def __init__(self, *, tick_s: float = 2.0, lease_s: int = 900, worker_id: str | None = None) -> None:
        self.tick_s = tick_s
        self.lease_s = lease_s
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.settings = get_settings()
        self.slots = asyncio.Semaphore(max(1, self.settings.telephony_concurrency))
        self.running: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self.ticks = 0

    # ------------------------------------------------------------ lifecycle
    async def acquire_lock(self) -> None:
        async with sessionmaker()() as db:
            row = await db.get(SystemState, LOCK_KEY)
            now = utcnow()
            if row is not None:
                v = row.value or {}
                hb = v.get("heartbeat_at")
                if (v.get("worker_id") != self.worker_id and hb and v.get("state") == "running"
                        and (now - _parse(hb)).total_seconds() < HEARTBEAT_STALE_S):  # fmt: skip
                    raise SchedulerLockedError(f"another scheduler is running ({v.get('worker_id')})")
            await self._write_state(db, "running")
            await db.commit()

    async def recover(self) -> int:
        """Re-queue work abandoned by a previous (crashed or stopped) scheduler process."""
        async with sessionmaker()() as db:
            stale = (await db.execute(select(ScheduledJob).where(ScheduledJob.status == "running",
                     ScheduledJob.locked_by != self.worker_id))).scalars().all()  # fmt: skip
            for job in stale:
                calls = (await db.execute(select(Call).where(Call.job_id == job.id,
                         Call.status.in_(("queued", "dialing", "connecting", "in_progress"))))).scalars().all()  # fmt: skip
                for c in calls:
                    c.status, c.ended_at, c.end_reason = "failed", utcnow(), "interrupted_by_restart"
                    c.error_code, c.error_detail = "interrupted", "Scheduler stopped while this call was in progress"
                    c.outcome = c.outcome or "failed"
                job.status, job.locked_by, job.locked_until = "pending", None, None
                job.error = "re-queued after scheduler restart"
                if job.campaign_contact_id:
                    await db.execute(update(CampaignContact).where(CampaignContact.id == job.campaign_contact_id)
                                     .values(status="queued"))  # fmt: skip
            if stale:
                system_event(db, "scheduler", f"Recovered {len(stale)} interrupted job(s) after restart", level="warning")
            await db.commit()
            return len(stale)

    async def run_forever(self) -> None:
        await self.acquire_lock()
        await self.recover()
        log.info("scheduler %s started", self.worker_id)
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.exception("scheduler tick failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.tick_s)
        finally:
            await self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        if self.running:
            log.info("waiting for %d running job(s)", len(self.running))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*self.running.values(), return_exceptions=True), timeout=60)
        async with sessionmaker()() as db:
            await self._write_state(db, "stopped")
            await db.commit()

    async def _write_state(self, db: AsyncSession, state: str) -> None:
        value = {"worker_id": self.worker_id, "pid": os.getpid(), "state": state,
                 "heartbeat_at": utcnow().isoformat(), "running_jobs": len(self.running), "ticks": self.ticks}  # fmt: skip
        stmt = sqlite_insert(SystemState).values(key=LOCK_KEY, value=value, updated_at=utcnow())
        await db.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": utcnow()}))

    # ------------------------------------------------------------ tick
    async def tick(self) -> None:
        self.ticks += 1
        async with sessionmaker()() as db:
            await self._write_state(db, "running")
            await self._activate_scheduled(db)
            await self._enqueue(db)
            await db.commit()
        await self._dispatch()
        async with sessionmaker()() as db:
            await self._complete_campaigns(db)
            await db.commit()
        if self.ticks % 900 == 1:  # roughly every 30 min at 2 s ticks
            await self.apply_retention()

    async def _activate_scheduled(self, db: AsyncSession) -> None:
        now = utcnow()
        await db.execute(update(Campaign).where(Campaign.status == "scheduled", Campaign.schedule_start_at <= now)
                         .values(status="running", started_at=func.coalesce(Campaign.started_at, now)))  # fmt: skip
        ended = (await db.execute(select(Campaign).where(Campaign.status.in_(("running", "scheduled")),
                 Campaign.schedule_end_at.is_not(None), Campaign.schedule_end_at <= now))).scalars().all()  # fmt: skip
        for c in ended:
            c.status, c.completed_at, c.status_reason = "completed", now, "Schedule end reached"
            await db.execute(update(CampaignContact).where(CampaignContact.campaign_id == c.id,
                             CampaignContact.status.in_(("pending", "queued"))).values(status="cancelled",
                             skip_reason="schedule ended"))  # fmt: skip
            await db.execute(update(ScheduledJob).where(ScheduledJob.campaign_id == c.id, ScheduledJob.status == "pending")
                             .values(status="cancelled", finished_at=now))  # fmt: skip

    async def _enqueue(self, db: AsyncSession) -> None:
        now = utcnow()
        campaigns = (await db.execute(select(Campaign).where(Campaign.status == "running"))).scalars().all()
        for c in campaigns:
            if c.mode == "PSTN" and not self.settings.real_telephony_enabled:
                c.status, c.status_reason = "paused", "Paused: real telephony is disabled on this installation"
                continue
            ws = await db.get(Workspace, c.workspace_id)
            if ws is None or ws.deleted_at is not None:
                continue
            active = (await db.execute(select(func.count(ScheduledJob.id)).where(
                ScheduledJob.campaign_id == c.id, ScheduledJob.status.in_(("pending", "running"))))).scalar_one()  # fmt: skip
            capacity = c.max_concurrent_calls - active
            if capacity <= 0:
                continue
            candidates = (await db.execute(select(CampaignContact).where(
                CampaignContact.campaign_id == c.id, CampaignContact.status == "pending",
                (CampaignContact.next_attempt_at.is_(None)) | (CampaignContact.next_attempt_at <= now))
                .order_by(CampaignContact.next_attempt_at.is_not(None), CampaignContact.created_at)
                .limit(capacity * 4))).scalars().all()  # fmt: skip
            for cc in candidates:
                if capacity <= 0:
                    break
                elig = await check_eligibility(db, ws, cc.contact, purpose=c.purpose, mode=c.mode, campaign=c,
                                               attempts=cc.attempts, now=now)  # fmt: skip
                if not elig.allowed:
                    failed = {ch.name for ch in elig.checks if not ch.passed}
                    if elig.retry_after:
                        cc.next_attempt_at, cc.skip_reason = elig.retry_after, "Waiting for calling window"
                    elif failed <= {"frequency_day", "frequency_week"}:
                        cc.next_attempt_at = now + timedelta(days=1)
                        cc.skip_reason = "Frequency limit reached; retrying later"
                    else:
                        cc.status = "exhausted" if failed == {"attempts"} else "skipped"
                        cc.skip_reason = "; ".join(elig.reasons)[:2000]
                        audit(db, "compliance.call_blocked", workspace_id=c.workspace_id, entity_type="contact",
                              entity_id=cc.contact_id, details={"campaign_id": c.id, "checks": sorted(failed)})  # fmt: skip
                    continue
                attempt = cc.attempts + 1
                res = await db.execute(sqlite_insert(ScheduledJob).values(
                    id=new_id(), workspace_id=c.workspace_id, kind="campaign_call", campaign_id=c.id,
                    campaign_contact_id=cc.id, dedupe_key=f"cc:{cc.id}:attempt:{attempt}", status="pending",
                    attempt=attempt, run_at=now, created_at=now, error="").on_conflict_do_nothing())  # fmt: skip
                if res.rowcount:  # type: ignore[attr-defined]
                    cc.status, cc.skip_reason = "queued", ""
                    capacity -= 1

    async def _dispatch(self) -> None:
        free = self.settings.telephony_concurrency - len(self.running)
        if free <= 0:
            return
        now = utcnow()
        async with sessionmaker()() as db:
            jobs = (await db.execute(select(ScheduledJob.id).where(ScheduledJob.status == "pending",
                    ScheduledJob.run_at <= now).order_by(ScheduledJob.run_at).limit(free))).scalars().all()  # fmt: skip
            claimed = []
            for jid in jobs:
                res = await db.execute(update(ScheduledJob).where(ScheduledJob.id == jid, ScheduledJob.status == "pending")
                                       .values(status="running", locked_by=self.worker_id,
                                               locked_until=now + timedelta(seconds=self.lease_s)))  # fmt: skip
                if res.rowcount == 1:  # type: ignore[attr-defined]
                    claimed.append(jid)
            await db.commit()
        for jid in claimed:
            task = asyncio.create_task(self._execute(jid), name=f"job-{jid}")
            self.running[jid] = task
            task.add_done_callback(self._forget(jid))

    def _forget(self, job_id: str) -> Callable[[asyncio.Task[None]], None]:
        def done(_t: asyncio.Task[None]) -> None:
            self.running.pop(job_id, None)

        return done

    async def _execute(self, job_id: str) -> None:
        async with self.slots:
            try:
                await self._execute_inner(job_id)
            except Exception:
                log.exception("job %s crashed", job_id)
                async with sessionmaker()() as db:
                    await db.execute(update(ScheduledJob).where(ScheduledJob.id == job_id)
                                     .values(status="failed", finished_at=utcnow(), error="internal error"))  # fmt: skip
                    await db.commit()

    async def _execute_inner(self, job_id: str) -> None:
        async with sessionmaker()() as db:
            job = await db.get(ScheduledJob, job_id)
            if job is None or job.status != "running" or job.locked_by != self.worker_id:
                return
            cc = await db.get(CampaignContact, job.campaign_contact_id) if job.campaign_contact_id else None
            campaign = await db.get(Campaign, job.campaign_id) if job.campaign_id else None
            if cc is None or campaign is None:
                job.status, job.finished_at, job.error = "failed", utcnow(), "missing campaign data"
                await db.commit()
                return
            if campaign.status != "running":
                job.status, job.finished_at, job.error = "cancelled", utcnow(), f"campaign {campaign.status}"
                if cc.status == "queued":
                    cc.status = "pending" if campaign.status == "paused" else "cancelled"
                await db.commit()
                return
            cc.status, cc.attempts, cc.last_attempt_at = "in_progress", job.attempt, utcnow()
            sim = dict(campaign.simulation or {})
            ws_id, agent_id, contact_id, campaign_id = campaign.workspace_id, campaign.agent_id, cc.contact_id, campaign.id
            mode, dedupe = campaign.mode, job.dedupe_key
            await db.commit()

        error: Exception | None = None
        call_id: str | None = None
        try:
            if mode == "SIMULATION":
                persona = pick_persona(sim.get("persona_weights") or {"questions": 1}, dedupe)
                scenario = SimulationScenario(answer_rate=float(sim.get("answer_rate", 0.75)),
                                              busy_rate=float(sim.get("busy_rate", 0.05)),
                                              fail_rate=float(sim.get("fail_rate", 0.05)), ring_seconds=0.2)  # fmt: skip
                call_id = await run_simulated_call(ws_id=ws_id, agent_id=agent_id, contact_id=contact_id,
                                                   campaign_id=campaign_id, job_id=job_id, persona=persona,
                                                   callee_mode=str(sim.get("callee_mode", "scripted")),
                                                   seed=dedupe, scenario=scenario)  # fmt: skip
            else:
                from app.calls.pstn import run_pstn_call

                call_id = await run_pstn_call(ws_id=ws_id, agent_id=agent_id, contact_id=contact_id,
                                              campaign_id=campaign_id, job_id=job_id)  # fmt: skip
        except Exception as exc:
            error = exc
        await self._finish(job_id, call_id, error)

    async def _finish(self, job_id: str, call_id: str | None, error: Exception | None) -> None:
        now = utcnow()
        async with sessionmaker()() as db:
            job = await db.get(ScheduledJob, job_id)
            assert job is not None
            cc = await db.get(CampaignContact, job.campaign_contact_id) if job.campaign_contact_id else None
            campaign = await db.get(Campaign, job.campaign_id) if job.campaign_id else None
            assert cc is not None and campaign is not None
            call = await db.get(Call, call_id) if call_id else None
            if call is None and call_id is None and error is not None:
                call = (await db.execute(select(Call).where(Call.job_id == job_id).order_by(Call.created_at.desc())
                                         .limit(1))).scalar_one_or_none()  # fmt: skip
            job.finished_at, job.locked_until = now, None
            if error is not None and isinstance(error, ProviderError):
                # system failure (local AI down): do not burn the contact's attempt
                job.status, job.error = "failed", f"{error.code}: {error}"[:2000]
                cc.status, cc.attempts = "pending", max(0, job.attempt - 1)
                cc.next_attempt_at = now + timedelta(minutes=2)
                cc.last_call_id = call.id if call else cc.last_call_id
                # atomic increment: concurrent job failures must not lose updates
                await db.execute(update(Campaign).where(Campaign.id == campaign.id)
                                 .values(consecutive_failures=Campaign.consecutive_failures + 1))  # fmt: skip
                await db.refresh(campaign, ["consecutive_failures", "status"])
                if campaign.consecutive_failures >= 3 and campaign.status == "running":
                    campaign.status = "paused"
                    campaign.status_reason = f"Paused automatically after repeated provider failures: {error}"[:2000]
                    system_event(db, "scheduler", campaign.status_reason, level="error",
                                 workspace_id=campaign.workspace_id, details={"campaign_id": campaign.id})  # fmt: skip
                await db.commit()
                return
            if error is not None:
                job.status, job.error = "failed", f"{error.__class__.__name__}: {error}"[:2000]
                cc.status, cc.skip_reason = "failed", f"Internal error: {error.__class__.__name__}"
                await db.commit()
                return
            job.status = "done"
            await db.execute(update(Campaign).where(Campaign.id == campaign.id).values(consecutive_failures=0))
            assert call is not None
            cc.last_call_id, cc.last_outcome = call.id, call.outcome
            retryable = call.status in RETRYABLE_STATUSES or call.outcome == "no_answer"
            if retryable and cc.attempts < campaign.max_attempts:
                cc.status = "pending"
                cc.next_attempt_at = now + timedelta(minutes=campaign.retry_delay_minutes)
            elif retryable:
                cc.status, cc.skip_reason = "exhausted", f"No answer after {cc.attempts} attempt(s)"
            else:
                cc.status = "completed"
            await db.commit()

    async def _complete_campaigns(self, db: AsyncSession) -> None:
        running = (await db.execute(select(Campaign).where(Campaign.status == "running"))).scalars().all()
        for c in running:
            open_items = (await db.execute(select(func.count(CampaignContact.id)).where(
                CampaignContact.campaign_id == c.id,
                CampaignContact.status.in_(("pending", "queued", "in_progress"))))).scalar_one()  # fmt: skip
            if open_items == 0:
                c.status, c.completed_at, c.status_reason = "completed", utcnow(), "All contacts processed"

    # ------------------------------------------------------------ retention
    async def apply_retention(self) -> dict[str, int]:
        removed = {"recordings": 0, "transcripts": 0}
        settings = get_settings()
        async with sessionmaker()() as db:
            for ws in (await db.execute(select(Workspace))).scalars().all():
                policy = policy_for(ws.compliance)
                rec_cut = utcnow() - timedelta(days=policy.retention_days_recordings)
                for rec in (await db.execute(select(Recording).where(Recording.workspace_id == ws.id,
                            Recording.deleted_at.is_(None), Recording.created_at < rec_cut))).scalars():  # fmt: skip
                    path = (settings.data_dir / rec.relative_path).resolve()
                    if path.is_relative_to(settings.recordings_dir.resolve()):
                        path.unlink(missing_ok=True)
                    rec.deleted_at = utcnow()
                    removed["recordings"] += 1
                tr_cut = utcnow() - timedelta(days=policy.retention_days_transcripts)
                old_calls = select(Call.id).where(Call.workspace_id == ws.id, Call.created_at < tr_cut)
                res = await db.execute(delete(Transcript).where(Transcript.workspace_id == ws.id,
                                                                Transcript.call_id.in_(old_calls)))  # fmt: skip
                removed["transcripts"] += res.rowcount or 0  # type: ignore[attr-defined]
                await db.execute(delete(Message).where(Message.workspace_id == ws.id, Message.created_at < tr_cut))
                # summaries, tool logs and prompts can contain personal data captured during the call
                await db.execute(update(Call).where(Call.workspace_id == ws.id, Call.created_at < tr_cut,
                                 Call.summary.is_not(None)).values(summary=None))  # fmt: skip
                await db.execute(update(Conversation).where(Conversation.workspace_id == ws.id,
                                 Conversation.created_at < tr_cut).values(tool_log=[], system_prompt=""))  # fmt: skip
            if any(removed.values()):
                system_event(db, "retention", f"Retention applied: {removed}")
            await db.commit()
        return removed


def _parse(iso: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(iso)
