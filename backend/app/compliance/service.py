"""Calling-eligibility engine. Fails closed: any check that cannot be completed blocks the call."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.policy import CompliancePolicy, policy_for
from app.core.audit import audit
from app.core.config import get_settings
from app.database.base import utcnow
from app.database.models import Call, Campaign, ConsentRecord, Contact, SuppressionEntry, Workspace


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Eligibility:
    allowed: bool
    checks: list[Check] = field(default_factory=list)
    retry_after: datetime | None = None  # set when only the time window blocks the call

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "checks": [asdict(c) for c in self.checks], "reasons": self.reasons,
                "retry_after": self.retry_after.isoformat() if self.retry_after else None}  # fmt: skip


def _hm(v: str) -> tuple[int, int]:
    h, m = v.split(":")
    return int(h), int(m)


def window_status(now: datetime, tz: str, start: str, end: str, days: list[int]) -> tuple[bool, datetime | None]:
    """Is ``now`` inside the window? If not, when does the next window open (UTC)?"""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    sh, sm = _hm(start)
    eh, em = _hm(end)
    in_day = local.weekday() in days
    start_t = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_t = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if in_day and start_t <= local < end_t:
        return True, None
    for offset in range(0, 8):
        day = (local + timedelta(days=offset)).replace(hour=sh, minute=sm, second=0, microsecond=0)
        if day.weekday() in days and day > local:
            return False, day.astimezone(ZoneInfo("UTC"))
    return False, None


def effective_window(policy: CompliancePolicy, campaign: Campaign | None) -> tuple[str, str, list[int]]:
    """Campaign windows can only narrow the workspace window, never widen it."""
    start, end, days = policy.calling_window_start, policy.calling_window_end, list(policy.calling_days)
    if campaign is not None:
        start = max(start, campaign.window_start)
        end = min(end, campaign.window_end)
        days = sorted(set(days) & set(campaign.calling_days or []))
    return start, end, days


async def active_consent(db: AsyncSession, ws_id: str, contact: Contact, purpose: str, policy: CompliancePolicy,
                         now: datetime) -> ConsentRecord | None:  # fmt: skip
    conds = [ConsentRecord.contact_id == contact.id]
    if contact.phone_e164:
        conds.append(ConsentRecord.phone_e164 == contact.phone_e164)
    stmt = select(ConsentRecord).where(
        ConsentRecord.workspace_id == ws_id, or_(*conds), ConsentRecord.purpose == purpose,
        ConsentRecord.status == "granted", ConsentRecord.revoked_at.is_(None),
        or_(ConsentRecord.expires_at.is_(None), ConsentRecord.expires_at > now),
    ).order_by(ConsentRecord.obtained_at.desc())  # fmt: skip
    if purpose == "inquiry_followup":
        stmt = stmt.where(ConsentRecord.obtained_at >= now - timedelta(days=policy.inquiry_followup_days))
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def check_eligibility(
    db: AsyncSession,
    ws: Workspace,
    contact: Contact,
    *,
    purpose: str,
    mode: str,
    campaign: Campaign | None = None,
    attempts: int = 0,
    now: datetime | None = None,
) -> Eligibility:
    now = now or utcnow()
    policy = policy_for(ws.compliance)
    checks: list[Check] = []
    retry_after: datetime | None = None

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name, ok, detail))

    try:
        # 1. reachable number
        add("phone", bool(contact.phone_e164), "Valid phone number" if contact.phone_e164 else "No valid phone number")
        add("contact_status", contact.deleted_at is None and contact.status not in ("do_not_call", "wrong_number"),
            f"Contact status is '{contact.status}'")  # fmt: skip

        # 2. suppression list
        if contact.phone_e164:
            sup = (await db.execute(select(SuppressionEntry).where(
                SuppressionEntry.workspace_id == ws.id, SuppressionEntry.phone_e164 == contact.phone_e164,
                or_(SuppressionEntry.expires_at.is_(None), SuppressionEntry.expires_at > now)))).scalar_one_or_none()  # fmt: skip
            add("suppression", sup is None, f"Number is on the suppression list ({sup.reason})" if sup else "Not suppressed")

        # 3. consent
        if purpose in policy.consent_required_for:
            consent = await active_consent(db, ws.id, contact, purpose, policy, now)
            add("consent", consent is not None,
                f"Recorded {purpose} consent ({consent.source}, {consent.obtained_at:%Y-%m-%d})" if consent
                else f"No active recorded consent for '{purpose}' calls")  # fmt: skip
        else:
            add("consent", True, f"Consent not required by policy for '{purpose}'")

        # 4. DND / NCPR preference (operator-supplied scrub results; no public API exists)
        if purpose in policy.dnd_check_required_for:
            fresh = contact.dnd_checked_at is not None and contact.dnd_checked_at >= now - timedelta(days=policy.dnd_max_age_days)
            if contact.dnd_status == "registered":
                dca = await active_consent(db, ws.id, contact, purpose, policy, now)
                ok = dca is not None and dca.source == "digital_consent"
                add("dnd", ok, "Registered on DND; explicit digital consent on record" if ok
                    else "Number is registered on DND/NCPR")  # fmt: skip
            elif contact.dnd_status == "not_registered" and fresh:
                add("dnd", True, f"DND scrub: not registered (checked {contact.dnd_checked_at:%Y-%m-%d})")
            else:
                add("dnd", False, "DND status unknown or older than "
                    f"{policy.dnd_max_age_days} days — import a fresh scrub result")  # fmt: skip
        else:
            add("dnd", True, f"DND scrub not required by policy for '{purpose}'")

        # 5. calling window
        start, end, days = effective_window(policy, campaign)
        ignore_window = bool(campaign and mode == "SIMULATION" and (campaign.simulation or {}).get("ignore_calling_window"))
        if ignore_window:
            add("calling_window", True, "Calling window ignored for this simulation (configured)")
        elif not days or start >= end:
            add("calling_window", False, "Campaign window does not overlap the workspace calling window")
        else:
            tz = campaign.timezone if campaign else ws.timezone
            inside, nxt = window_status(now, tz, start, end, days)
            add("calling_window", inside, f"Inside calling window {start}–{end} ({tz})" if inside
                else f"Outside calling window {start}–{end} ({tz})")  # fmt: skip
            if not inside:
                retry_after = nxt

        # 6. frequency limits (same calling mode, per number)
        if contact.phone_e164:
            async def count_since(delta: timedelta) -> int:
                return (await db.execute(select(func.count(Call.id)).where(
                    Call.workspace_id == ws.id, Call.mode == mode, Call.to_number == contact.phone_e164,
                    Call.started_at >= now - delta))).scalar_one()  # fmt: skip

            day = await count_since(timedelta(days=1))
            week = await count_since(timedelta(days=7))
            add("frequency_day", day < policy.max_calls_per_contact_per_day,
                f"{day} call(s) in last 24h (limit {policy.max_calls_per_contact_per_day})")  # fmt: skip
            add("frequency_week", week < policy.max_calls_per_contact_per_week,
                f"{week} call(s) in last 7 days (limit {policy.max_calls_per_contact_per_week})")  # fmt: skip

        # 7. attempts
        if campaign is not None:
            add("attempts", attempts < campaign.max_attempts, f"Attempt {attempts + 1} of {campaign.max_attempts}")

        # 8. real telephony prerequisites
        if mode == "PSTN":
            missing = policy.pstn_prerequisites_missing()
            if not get_settings().real_telephony_enabled:
                missing.insert(0, "Real telephony is disabled on this installation (VA_REAL_TELEPHONY_ENABLED)")
            add("pstn_prerequisites", not missing, "; ".join(missing) or "PSTN prerequisites recorded")
    except Exception as exc:  # fail closed
        add("internal", False, f"Eligibility check could not be completed: {exc.__class__.__name__}")

    allowed = all(c.passed for c in checks)
    only_window = not allowed and all(c.passed or c.name == "calling_window" for c in checks)
    return Eligibility(allowed, checks, retry_after if only_window else None)


async def record_opt_out(db: AsyncSession, ws_id: str, *, phone_e164: str | None, contact_id: str | None,
                         source: str, user_id: str | None = None, call_id: str | None = None,
                         reason: str = "opt_out") -> bool:  # fmt: skip
    """Suppress the number, mark the contact do-not-call and revoke marketing consent."""
    if not phone_e164 and contact_id:
        c = await db.get(Contact, contact_id)
        phone_e164 = c.phone_e164 if c and c.workspace_id == ws_id else None
    if contact_id:
        await db.execute(update(Contact).where(Contact.id == contact_id, Contact.workspace_id == ws_id)
                         .values(status="do_not_call" if reason == "opt_out" else "wrong_number"))  # fmt: skip
    if not phone_e164:
        return False
    exists = (await db.execute(select(SuppressionEntry).where(SuppressionEntry.workspace_id == ws_id,
              SuppressionEntry.phone_e164 == phone_e164))).scalar_one_or_none()  # fmt: skip
    if exists is None:
        db.add(SuppressionEntry(workspace_id=ws_id, phone_e164=phone_e164, reason=reason, source=source,
                                created_by=user_id, notes=f"call {call_id}" if call_id else ""))  # fmt: skip
    if reason == "opt_out":
        await db.execute(update(ConsentRecord).where(ConsentRecord.workspace_id == ws_id,
                         ConsentRecord.phone_e164 == phone_e164, ConsentRecord.revoked_at.is_(None))
                         .values(status="revoked", revoked_at=utcnow()))  # fmt: skip
    audit(db, f"compliance.{reason}", user_id=user_id, workspace_id=ws_id, entity_type="contact",
          entity_id=contact_id, details={"source": source, "call_id": call_id})  # fmt: skip
    return True
