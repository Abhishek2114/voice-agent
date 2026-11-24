from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import WorkspaceContext, ws_editor, ws_viewer
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, not_found
from app.core.files import safe_filename, sniff_document
from app.database.models import KnowledgeDocument
from app.database.session import get_db
from app.knowledge.service import KINDS, ExtractionError, extract_text, faq_to_text, index_document, search

router = APIRouter(prefix="/api/workspaces/{workspace_id}/knowledge", tags=["knowledge"])


class FAQItem(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)


class DocIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: str = "document"
    content: str = Field(default="", max_length=500_000)
    faq: list[FAQItem] | None = Field(default=None, max_length=500)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        return v


def doc_out(d: KnowledgeDocument, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"id": d.id, "title": d.title, "kind": d.kind, "source_filename": d.source_filename,
                           "size_bytes": d.size_bytes, "status": d.status, "error": d.error,
                           "chunk_count": d.chunk_count, "created_at": d.created_at, "updated_at": d.updated_at,
                           "preview": d.content[:240]}  # fmt: skip
    if full:
        out["content"] = d.content
    return out


async def _get(db: AsyncSession, ws_id: str, doc_id: str) -> KnowledgeDocument:
    d = await db.get(KnowledgeDocument, doc_id)
    if d is None or d.workspace_id != ws_id:
        raise not_found("Document")
    return d


@router.get("")
async def list_docs(ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db),
                    kind: str | None = None) -> dict[str, Any]:  # fmt: skip
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.workspace_id == ctx.id)
    if kind:
        stmt = stmt.where(KnowledgeDocument.kind == kind)
    rows = (await db.execute(stmt.order_by(KnowledgeDocument.updated_at.desc()))).scalars().all()
    counts = dict((await db.execute(select(KnowledgeDocument.kind, func.count()).where(
        KnowledgeDocument.workspace_id == ctx.id).group_by(KnowledgeDocument.kind))).all())  # fmt: skip
    return {"items": [doc_out(d) for d in rows], "counts": counts}


@router.post("", status_code=201)
async def create_doc(body: DocIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    content = faq_to_text([i.model_dump() for i in body.faq]) if body.faq else body.content
    if not content.strip():
        raise bad_request("Document content is empty")
    doc = KnowledgeDocument(workspace_id=ctx.id, title=body.title, kind="faq" if body.faq else body.kind,
                            content=content, size_bytes=len(content.encode()), mime_type="text/plain")  # fmt: skip
    db.add(doc)
    await db.flush()
    await index_document(db, doc)
    audit(db, "knowledge.created", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="knowledge",
          entity_id=doc.id, request=request)  # fmt: skip
    await db.commit()
    return doc_out(doc, full=True)


@router.post("/upload", status_code=201)
async def upload_doc(request: Request, file: UploadFile = File(...), title: str = Form("", max_length=200),
                     kind: str = Form("document"), ctx: WorkspaceContext = Depends(ws_editor),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    if kind not in KINDS:
        raise bad_request("invalid kind")
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise APIError(413, "too_large", f"File exceeds {get_settings().max_upload_mb} MB")
    if not data:
        raise bad_request("File is empty", "invalid_file")
    name = safe_filename(file.filename or "document")
    detected = sniff_document(data, name)
    if detected is None or not name.lower().endswith((".txt", ".md", ".markdown", ".csv", ".pdf")):
        raise bad_request("Supported files: .txt, .md, .csv (UTF-8) and text-based .pdf", "unsupported_file")
    if detected == "pdf" and not name.lower().endswith(".pdf"):
        raise bad_request("File content does not match its extension", "unsupported_file")
    try:
        content = extract_text(data, detected)
    except ExtractionError as exc:
        raise bad_request(str(exc), "extraction_failed") from exc
    doc = KnowledgeDocument(workspace_id=ctx.id, title=title.strip() or name, kind=kind, source_filename=name,
                            mime_type="application/pdf" if detected == "pdf" else "text/plain",
                            size_bytes=len(data), content=content[:500_000])  # fmt: skip
    db.add(doc)
    await db.flush()
    await index_document(db, doc)
    audit(db, "knowledge.uploaded", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="knowledge",
          entity_id=doc.id, details={"filename": name, "bytes": len(data)}, request=request)  # fmt: skip
    await db.commit()
    return doc_out(doc)


@router.get("/search")
async def search_docs(q: str = Query(..., min_length=1, max_length=300), ctx: WorkspaceContext = Depends(ws_viewer),
                      db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:  # fmt: skip
    hits = await search(db, ctx.id, q, limit=5)
    return [{"title": h.title, "content": h.content, "score": round(h.score, 3), "document_id": h.document_id}
            for h in hits]  # fmt: skip


@router.get("/{doc_id}")
async def get_doc(doc_id: str, ctx: WorkspaceContext = Depends(ws_viewer), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return doc_out(await _get(db, ctx.id, doc_id), full=True)


@router.put("/{doc_id}")
async def update_doc(doc_id: str, body: DocIn, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    doc = await _get(db, ctx.id, doc_id)
    content = faq_to_text([i.model_dump() for i in body.faq]) if body.faq else body.content
    if not content.strip():
        raise bad_request("Document content is empty")
    doc.title, doc.kind, doc.content, doc.size_bytes = body.title, body.kind, content, len(content.encode())
    await index_document(db, doc)
    audit(db, "knowledge.updated", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="knowledge",
          entity_id=doc.id, request=request)  # fmt: skip
    await db.commit()
    return doc_out(doc, full=True)


@router.delete("/{doc_id}")
async def delete_doc(doc_id: str, request: Request, ctx: WorkspaceContext = Depends(ws_editor),
                     db: AsyncSession = Depends(get_db)) -> dict[str, bool]:  # fmt: skip
    doc = await _get(db, ctx.id, doc_id)
    await db.delete(doc)
    audit(db, "knowledge.deleted", user_id=ctx.user.id, workspace_id=ctx.id, entity_type="knowledge",
          entity_id=doc_id, request=request)  # fmt: skip
    await db.commit()
    return {"ok": True}
