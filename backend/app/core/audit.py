from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AuditLog, SystemEvent


def client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def audit(
    db: AsyncSession,
    action: str,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Stage an audit record in the caller's transaction (committed with the change)."""
    db.add(AuditLog(action=action, user_id=user_id, workspace_id=workspace_id, entity_type=entity_type,
                    entity_id=entity_id, details=details or {}, ip=client_ip(request)))  # fmt: skip


def system_event(db: AsyncSession, source: str, message: str, *, level: str = "info",
                 workspace_id: str | None = None, details: dict[str, Any] | None = None) -> None:  # fmt: skip
    db.add(SystemEvent(source=source, message=message[:2000], level=level, workspace_id=workspace_id,
                       details=details or {}))  # fmt: skip
