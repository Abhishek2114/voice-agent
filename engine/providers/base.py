"""Types shared by every provider family."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

HealthStatus = Literal["ok", "degraded", "unavailable", "disabled"]


@dataclass
class ProviderHealth:
    provider: str
    status: HealthStatus
    detail: str = ""
    latency_ms: float | None = None
    is_local: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderError(Exception):
    """Base class for structured provider failures.

    ``code`` is a stable machine-readable identifier surfaced to the API/UI.
    ``retryable`` tells callers whether retrying the same request may succeed.
    """

    code: str = "provider_error"
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}
