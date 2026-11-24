from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.policy import CompliancePolicy
from app.database.models import User, Workspace, WorkspaceMembership


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return slug or "workspace"


async def unique_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug = base
    while (await db.execute(select(Workspace.id).where(Workspace.slug == slug))).first():
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


async def create_workspace(db: AsyncSession, owner: User, *, name: str, **fields: object) -> Workspace:
    ws = Workspace(name=name.strip(), slug=await unique_slug(db, name),
                   compliance=CompliancePolicy().model_dump(), settings={}, **fields)  # fmt: skip
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMembership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    await db.flush()
    return ws
