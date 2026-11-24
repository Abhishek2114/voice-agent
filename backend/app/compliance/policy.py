"""Per-workspace compliance policy (India-oriented defaults, all configurable).

These defaults are conservative engineering controls, not legal advice. See
docs/COMPLIANCE.md for the regulatory background and what remains the operator's
responsibility (TRAI/DLT registration, 140/160-series numbers, A2P declaration, etc.).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Purpose = Literal["promotional", "service", "transactional", "inquiry_followup"]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DEFAULT_CONSENT: tuple[Purpose, ...] = ("promotional", "service", "inquiry_followup")
_DEFAULT_DND: tuple[Purpose, ...] = ("promotional",)


class CompliancePolicy(BaseModel):
    # disclosure
    ai_disclosure_required: bool = True
    ai_disclosure_text: str = Field(
        default="Hello, this is an AI assistant calling on behalf of {business}.", max_length=400
    )
    recording_enabled: bool = False
    recording_disclosure_text: str = Field(default="This call may be recorded for quality purposes.", max_length=400)

    # calling time (workspace timezone); campaigns may only narrow this window
    calling_window_start: str = "09:00"
    calling_window_end: str = "21:00"
    calling_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])  # Mon..Sat

    # frequency
    max_calls_per_contact_per_day: int = Field(default=1, ge=1, le=10)
    max_calls_per_contact_per_week: int = Field(default=3, ge=1, le=30)

    # consent & preferences
    consent_required_for: list[Purpose] = Field(default_factory=lambda: list(_DEFAULT_CONSENT))
    inquiry_followup_days: int = Field(default=7, ge=1, le=30)
    dnd_check_required_for: list[Purpose] = Field(default_factory=lambda: list(_DEFAULT_DND))
    dnd_max_age_days: int = Field(default=30, ge=1, le=365)

    # retention
    retention_days_recordings: int = Field(default=90, ge=1, le=3650)
    retention_days_transcripts: int = Field(default=365, ge=1, le=3650)

    # real-telephony prerequisites (operator attestations; all must be true for PSTN)
    pstn_authorized: bool = False
    telemarketer_registration_id: str = Field(default="", max_length=120)
    registered_number_series: str = Field(default="", max_length=60)
    a2p_declared_to_provider: bool = False

    @field_validator("calling_window_start", "calling_window_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("time must be HH:MM (24h)")
        return v

    @field_validator("calling_days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("calling_days must contain weekday numbers 0 (Mon) – 6 (Sun)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _window(self) -> CompliancePolicy:
        if self.calling_window_start >= self.calling_window_end:
            raise ValueError("calling window start must be before end")
        if self.max_calls_per_contact_per_week < self.max_calls_per_contact_per_day:
            raise ValueError("weekly limit must be >= daily limit")
        return self

    def pstn_prerequisites_missing(self) -> list[str]:
        missing = []
        if not self.pstn_authorized:
            missing.append("Workspace has not been authorised for real (PSTN) calling")
        if not self.telemarketer_registration_id.strip():
            missing.append("Telemarketer / entity registration ID is not recorded")
        if not self.registered_number_series.strip():
            missing.append("Registered calling number series (e.g. 140xx/160xx) is not recorded")
        if not self.a2p_declared_to_provider:
            missing.append("Automated (A2P) calling has not been declared to the telecom provider")
        return missing


def policy_for(raw: dict[str, Any] | None) -> CompliancePolicy:
    try:
        return CompliancePolicy.model_validate(raw or {})
    except Exception:
        # a corrupted policy must not silently loosen controls
        return CompliancePolicy()
