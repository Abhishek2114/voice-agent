"""Profile, password and (instance-admin) user management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_session, current_user, instance_admin
from app.auth.router import user_out
from app.core.audit import audit
from app.core.errors import APIError, bad_request, conflict, not_found
from app.core.security import hash_password, password_problems, verify_password
from app.database.base import utcnow
from app.database.models import User, UserSession
from app.database.session import get_db

router = APIRouter(prefix="/api/users", tags=["users"])


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class NewUserIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)
    is_instance_admin: bool = False


class UserPatch(BaseModel):
    is_active: bool | None = None
    is_instance_admin: bool | None = None


@router.patch("/me")
async def update_profile(body: ProfileIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    u = await db.get(User, user.id)
    assert u is not None
    u.name = body.name.strip()
    await db.commit()
    return user_out(u)


@router.post("/me/password")
async def change_password(body: PasswordIn, request: Request, sess: UserSession = Depends(current_session),
                          db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    user = await db.get(User, sess.user_id)
    assert user is not None
    if not verify_password(user.password_hash, body.current_password):
        raise APIError(400, "invalid_password", "Current password is incorrect")
    problems = password_problems(body.new_password, user.email)
    if problems:
        raise bad_request(" ".join(problems), "weak_password")
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    # revoke every other session for this user
    await db.execute(update(UserSession).where(UserSession.user_id == user.id, UserSession.id != sess.id,
                                               UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))  # fmt: skip
    audit(db, "user.password_changed", user_id=user.id, request=request)
    await db.commit()
    return {"ok": True}


@router.get("/me/sessions")
async def my_sessions(sess: UserSession = Depends(current_session), db: AsyncSession = Depends(get_db)) -> list[dict[str, object]]:
    rows = (await db.execute(select(UserSession).where(UserSession.user_id == sess.user_id,
            UserSession.revoked_at.is_(None), UserSession.expires_at > utcnow())
            .order_by(UserSession.last_seen_at.desc()))).scalars()  # fmt: skip
    return [{"current": s.id == sess.id, "created_at": s.created_at, "last_seen_at": s.last_seen_at, "ip": s.ip,
             "user_agent": s.user_agent} for s in rows]  # fmt: skip


@router.get("")
async def list_users(_: User = Depends(instance_admin), db: AsyncSession = Depends(get_db)) -> list[dict[str, object]]:
    rows = (await db.execute(select(User).order_by(User.created_at))).scalars()
    return [{**user_out(u), "is_active": u.is_active, "last_login_at": u.last_login_at} for u in rows]


@router.post("", status_code=201)
async def create_user(body: NewUserIn, request: Request, admin: User = Depends(instance_admin),
                      db: AsyncSession = Depends(get_db)) -> dict[str, object]:  # fmt: skip
    email = body.email.lower()
    if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
        raise conflict("A user with that email already exists")
    problems = password_problems(body.password, email)
    if problems:
        raise bad_request(" ".join(problems), "weak_password")
    user = User(email=email, name=body.name, password_hash=hash_password(body.password),
                is_instance_admin=body.is_instance_admin, password_changed_at=utcnow())  # fmt: skip
    db.add(user)
    await db.flush()
    audit(db, "user.created", user_id=admin.id, entity_type="user", entity_id=user.id, request=request)
    await db.commit()
    return user_out(user)


@router.patch("/{user_id}")
async def patch_user(user_id: str, body: UserPatch, request: Request, admin: User = Depends(instance_admin),
                     db: AsyncSession = Depends(get_db)) -> dict[str, object]:  # fmt: skip
    user = await db.get(User, user_id)
    if user is None:
        raise not_found("User")
    if user.id == admin.id and (body.is_active is False or body.is_instance_admin is False):
        raise bad_request("You cannot deactivate or demote yourself")
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            await db.execute(update(UserSession).where(UserSession.user_id == user.id).values(revoked_at=utcnow()))
    if body.is_instance_admin is not None:
        user.is_instance_admin = body.is_instance_admin
    audit(db, "user.updated", user_id=admin.id, entity_type="user", entity_id=user.id,
          details=body.model_dump(exclude_none=True), request=request)  # fmt: skip
    await db.commit()
    return {**user_out(user), "is_active": user.is_active}
