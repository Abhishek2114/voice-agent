"""Authentication, CSRF and workspace authorization dependencies.

* Sessions: random 256-bit token in an HttpOnly, SameSite=Lax cookie; only its
  SHA-256 is stored. Absolute and idle expiry are enforced server-side.
* CSRF: every state-changing request from an authenticated session must carry
  ``X-CSRF-Token`` equal to the session's token (double-submit, token readable from
  the non-HttpOnly ``va_csrf`` cookie). Enforced here so no route can forget it.
* Workspace access: membership is resolved server-side from the session user; a
  workspace the user cannot access is reported as 404 (no existence oracle).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Path, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import APIError, forbidden
from app.core.security import constant_eq, token_hash
from app.database.base import utcnow
from app.database.models import User, UserSession, Workspace, WorkspaceMembership
from app.database.session import get_db

SESSION_COOKIE = "va_session"
CSRF_COOKIE = "va_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"viewer": 10, "editor": 20, "admin": 30, "owner": 40}


async def load_session(request: Request, db: AsyncSession) -> UserSession | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        return None
    sess = await db.get(UserSession, token_hash(token))
    if sess is None or sess.revoked_at is not None:
        return None
    now = utcnow()
    settings = get_settings()
    if sess.expires_at <= now or sess.last_seen_at + timedelta(hours=settings.session_idle_hours) <= now:
        return None
    if not sess.user.is_active:
        return None
    if (now - sess.last_seen_at).total_seconds() > 60:
        sess.last_seen_at = now
        await db.commit()
    return sess


async def current_session(request: Request, db: AsyncSession = Depends(get_db)) -> UserSession:
    sess = await load_session(request, db)
    if sess is None:
        raise APIError(401, "unauthenticated", "Please sign in")
    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not constant_eq(header, sess.csrf_token):
            raise APIError(403, "csrf_failed", "Missing or invalid CSRF token")
    request.state.user_id = sess.user_id
    return sess


async def current_user(sess: UserSession = Depends(current_session)) -> User:
    return sess.user


async def instance_admin(user: User = Depends(current_user)) -> User:
    if not user.is_instance_admin:
        raise forbidden("Instance administrator access required")
    return user


@dataclass
class WorkspaceContext:
    workspace: Workspace
    user: User
    role: str

    @property
    def id(self) -> str:
        return self.workspace.id

    def can(self, min_role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[min_role]


def workspace_access(min_role: str = "viewer"):
    async def dep(
        workspace_id: str = Path(..., min_length=1, max_length=40),
        user: User = Depends(current_user),
        db: AsyncSession = Depends(get_db),
    ) -> WorkspaceContext:
        row = (
            await db.execute(
                select(WorkspaceMembership)
                .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user.id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise APIError(404, "not_found", "Workspace not found")
        if ROLE_RANK.get(row.role, 0) < ROLE_RANK[min_role]:
            raise forbidden(f"This action requires the '{min_role}' role in this workspace")
        return WorkspaceContext(workspace=row.workspace, user=user, role=row.role)

    return dep


ws_viewer = workspace_access("viewer")
ws_editor = workspace_access("editor")
ws_admin = workspace_access("admin")
ws_owner = workspace_access("owner")
