"""Authentication endpoints: first-run setup, login, logout, session status."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CSRF_COOKIE, SESSION_COOKIE, current_session, load_session
from app.core.audit import audit, client_ip
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, conflict
from app.core.ratelimit import limiter
from app.core.security import (
    hash_password,
    needs_rehash,
    new_token,
    password_problems,
    token_hash,
    verify_password,
)
from app.database.base import utcnow
from app.database.models import User, UserSession, WorkspaceMembership
from app.database.session import get_db
from app.workspaces.service import create_workspace

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    workspace_name: str = Field(min_length=1, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


def user_out(user: User) -> dict[str, object]:
    return {"id": user.id, "email": user.email, "name": user.name, "is_instance_admin": user.is_instance_admin}


async def start_session(db: AsyncSession, user: User, request: Request, response: Response) -> UserSession:
    settings = get_settings()
    token = new_token()
    csrf = new_token(24)
    sess = UserSession(
        id=token_hash(token), user_id=user.id, csrf_token=csrf,
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300],
    )  # fmt: skip
    db.add(sess)
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")  # fmt: skip
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, samesite="lax",
                        secure=settings.cookie_secure, path="/")  # fmt: skip
    return sess


def clear_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/status")
async def status(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    users = (await db.execute(select(func.count(User.id)))).scalar_one()
    sess = await load_session(request, db)
    return {
        "setup_required": users == 0,
        "authenticated": sess is not None,
        "user": user_out(sess.user) if sess else None,
        "csrf_token": sess.csrf_token if sess else None,
    }


@router.post("/setup", status_code=201)
async def setup(body: SetupIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    ok, _ = limiter.hit(f"setup:{client_ip(request)}", 10, 300)
    if not ok:
        raise APIError(429, "rate_limited", "Too many attempts")
    if (await db.execute(select(func.count(User.id)))).scalar_one() > 0:
        raise conflict("Setup has already been completed", "already_setup")
    problems = password_problems(body.password, body.email)
    if problems:
        raise bad_request(" ".join(problems), "weak_password")
    user = User(email=body.email.lower(), name=body.name.strip(), password_hash=hash_password(body.password),
                is_instance_admin=True, password_changed_at=utcnow())  # fmt: skip
    db.add(user)
    await db.flush()
    ws = await create_workspace(db, user, name=body.workspace_name)
    sess = await start_session(db, user, request, response)
    audit(db, "auth.setup", user_id=user.id, workspace_id=ws.id, request=request)
    await db.commit()
    return {"user": user_out(user), "workspace_id": ws.id, "csrf_token": sess.csrf_token}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    settings = get_settings()
    ip = client_ip(request) or "unknown"
    window = settings.login_window_seconds
    for key, limit in ((f"login:ip:{ip}", settings.login_max_attempts * 4),
                       (f"login:acct:{body.email}", settings.login_max_attempts)):  # fmt: skip
        if limiter.count(key, window) >= limit:
            audit(db, "auth.login_rate_limited", details={"email_hash": token_hash(body.email)[:16]}, request=request)
            await db.commit()
            raise APIError(429, "rate_limited", "Too many sign-in attempts. Try again in a few minutes.")
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    valid = verify_password(user.password_hash if user else None, body.password)
    if not valid or user is None or not user.is_active:
        limiter.hit(f"login:ip:{ip}", 10_000, window)
        limiter.hit(f"login:acct:{body.email}", 10_000, window)
        audit(db, "auth.login_failed", user_id=user.id if user else None, request=request)
        await db.commit()
        raise APIError(401, "invalid_credentials", "Email or password is incorrect")
    limiter.reset(f"login:acct:{body.email}")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    user.last_login_at = utcnow()
    sess = await start_session(db, user, request, response)
    audit(db, "auth.login", user_id=user.id, request=request)
    await db.commit()
    return {"user": user_out(user), "csrf_token": sess.csrf_token}


@router.post("/logout")
async def logout(request: Request, response: Response, sess: UserSession = Depends(current_session),
                 db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    await db.execute(update(UserSession).where(UserSession.id == sess.id).values(revoked_at=utcnow()))
    audit(db, "auth.logout", user_id=sess.user_id, request=request)
    await db.commit()
    clear_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(request: Request, response: Response, sess: UserSession = Depends(current_session),
                     db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    await db.execute(update(UserSession).where(UserSession.user_id == sess.user_id, UserSession.revoked_at.is_(None))
                     .values(revoked_at=utcnow()))  # fmt: skip
    audit(db, "auth.logout_all", user_id=sess.user_id, request=request)
    await db.commit()
    clear_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(sess: UserSession = Depends(current_session), db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    rows = (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.user_id == sess.user_id))).scalars()
    return {
        "user": user_out(sess.user),
        "csrf_token": sess.csrf_token,
        "workspaces": [
            {"id": m.workspace.id, "name": m.workspace.name, "slug": m.workspace.slug, "role": m.role,
             "brand_color": m.workspace.brand_color}
            for m in rows
            if m.workspace.deleted_at is None
        ],
    }
