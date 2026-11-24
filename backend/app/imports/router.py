from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import WorkspaceContext, ws_editor, ws_viewer
from app.contacts.csv_safety import escape_cell
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, conflict, not_found
from app.core.files import safe_filename, sniff_document
from app.database.base import new_id
from app.database.models import ImportJob
from app.database.session import get_db
from app.imports.service import (
    TARGET_FIELDS,
    CSVError,
    commit_rows,
    guess_mapping,
    parse_csv,
    summarize,
    validate_rows,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}/imports", tags=["imports"])


class MappingIn(BaseModel):
    mapping: dict[str, str]
    custom_columns: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("mapping")
    @classmethod
    def _m(cls, v: dict[str, str]) -> dict[str, str]:
        clean = {k: t for k, t in v.items() if t and t != "ignore"}
        bad = {t for t in clean.values() if t not in TARGET_FIELDS}
        if bad:
            raise ValueError(f"unknown target field(s): {', '.join(sorted(bad))}")
        if len(set(clean.values())) != len(clean):
            raise ValueError("each target field can be mapped only once")
        if not ({"phone", "email"} & set(clean.values())):
            raise ValueError("map at least a phone or an email column")
        return clean


class CommitIn(MappingIn):
    update_existing: bool = False
    tags: list[str] = Field(default_factory=list, max_length=10)


def _path(ws_id: str, job: ImportJob) -> Any:
    return get_settings().uploads_dir / ws_id / "imports" / job.stored_name


async def _job(db: AsyncSession, ws_id: str, job_id: str) -> ImportJob:
    job = await db.get(ImportJob, job_id)
    if job is None or job.workspace_id != ws_id:
        raise not_found("Import")
    return job


def _load(ws_id: str, job: ImportJob) -> tuple[list[str], list[list[str]]]:
    p = _path(ws_id, job)
    if not p.is_file():
        raise APIError(410, "expired", "The uploaded file is no longer available; upload it again")
    return parse_csv(p.read_bytes())


def _job_out(j: ImportJob) -> dict[str, Any]:
    return {"id": j.id, "filename": j.filename, "status": j.status, "row_count": j.row_count, "headers": j.headers,
            "mapping": j.mapping, "report": j.report, "created_at": j.created_at}  # fmt: skip


@router.get("")
async def list_imports(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(ImportJob).where(ImportJob.workspace_id == ctx.id)
                             .order_by(ImportJob.created_at.desc()).limit(50))).scalars()  # fmt: skip
    return [_job_out(j) for j in rows]


@router.post("", status_code=201)
async def upload(request: Request, file: UploadFile = File(...), ctx: WorkspaceContext = Depends(ws_editor),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    settings = get_settings()
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise APIError(413, "too_large", f"CSV exceeds {settings.max_upload_mb} MB")
    name = safe_filename(file.filename or "contacts.csv")
    if not name.lower().endswith((".csv", ".txt", ".tsv")) or sniff_document(data, "x.csv") != "csv":
        raise bad_request("Upload a UTF-8 .csv file", "unsupported_file")
    try:
        headers, rows = parse_csv(data)
    except CSVError as exc:
        raise bad_request(str(exc), "invalid_csv") from exc
    job = ImportJob(workspace_id=ctx.id, filename=name, stored_name=f"{new_id()}.csv", row_count=len(rows),
                    headers=headers, mapping=guess_mapping(headers), created_by=ctx.user.id)  # fmt: skip
    path = _path(ctx.id, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    db.add(job)
    audit(db, "import.uploaded", user_id=ctx.user.id, workspace_id=ctx.id, details={"rows": len(rows)}, request=request)
    await db.commit()
    preview = [[escape_cell(c) for c in r[: len(headers)]] for r in rows[:10]]
    return {**_job_out(job), "preview": preview, "targets": list(TARGET_FIELDS)}


@router.post("/{job_id}/validate")
async def validate(job_id: str, body: MappingIn, ctx: WorkspaceContext = Depends(ws_editor),
                   db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    job = await _job(db, ctx.id, job_id)
    if job.status != "uploaded":
        raise conflict("This import has already been processed")
    headers, rows = _load(ctx.id, job)
    unknown = set(body.mapping) - set(headers)
    if unknown:
        raise bad_request(f"Unknown column(s): {', '.join(sorted(unknown))}")
    results = await validate_rows(db, ctx.id, headers, rows, body.mapping, ctx.workspace.default_country,
                                  body.custom_columns)  # fmt: skip
    job.mapping = body.mapping
    await db.commit()
    problems = [r for r in results if r.status != "valid" or r.warnings][:200]
    sample = [r for r in results if r.status == "valid"][:20]
    return {
        "summary": summarize(results),
        "rows": [{"row": r.row, "status": r.status, "errors": r.errors, "warnings": r.warnings,
                  "data": {k: v for k, v in r.data.items() if k in ("first_name", "last_name", "phone_e164", "email",
                                                                   "company", "tags", "consent", "dnd_status")}}
                 for r in sample + problems],
        "consent_notice": "Phone numbers in a CSV are not consent. Consent is only recorded for rows with a mapped "
                          "purpose, source, date and evidence.",
    }  # fmt: skip


@router.post("/{job_id}/commit")
async def commit(job_id: str, body: CommitIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    job = await _job(db, ctx.id, job_id)
    if job.status != "uploaded":
        raise conflict("This import has already been processed")
    headers, rows = _load(ctx.id, job)
    results = await validate_rows(db, ctx.id, headers, rows, body.mapping, ctx.workspace.default_country,
                                  body.custom_columns)  # fmt: skip
    tags = [t.strip().lower()[:40] for t in body.tags if t.strip()]
    outcome = await commit_rows(db, ctx.id, results, update_existing=body.update_existing,
                                extra_tags=[*tags, f"import-{job.created_at:%Y%m%d}"], user_id=ctx.user.id,
                                source="csv_import")  # fmt: skip
    summary = summarize(results)
    job.status, job.mapping = "completed", body.mapping
    job.report = {**summary, **outcome, "invalid_rows": [{"row": r.row, "errors": r.errors}
                                                          for r in results if r.status == "invalid"][:500]}  # fmt: skip
    audit(db, "import.completed", user_id=ctx.user.id, workspace_id=ctx.id, details={**summary, **outcome},
          request=request)  # fmt: skip
    await db.commit()
    _path(ctx.id, job).unlink(missing_ok=True)  # data minimisation: the raw upload is not kept
    return _job_out(job)


@router.delete("/{job_id}")
async def discard(job_id: str, ctx: WorkspaceContext = Depends(ws_editor), db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    job = await _job(db, ctx.id, job_id)
    _path(ctx.id, job).unlink(missing_ok=True)
    if job.status == "uploaded":
        job.status = "expired"
    await db.commit()
    return {"ok": True}
