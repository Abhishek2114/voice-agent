"""Workspace-scoped knowledge base: extraction, chunking and BM25 retrieval (SQLite FTS5).

Retrieval is lexical and fully local: no embeddings model is required and no document
ever leaves the machine. Every query is filtered by ``workspace_id``.
"""

from __future__ import annotations

import io
import re

from engine.conversation.tools import KnowledgeSnippet
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import KnowledgeChunk, KnowledgeDocument

KINDS = ("faq", "business_info", "product", "service", "pricing", "policy", "document")
_STOP = set(["a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from", "have", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "our", "please", "so", "that", "the", "this", "to", "us", "was", "we", "what", "when", "where", "which", "who", "why", "will", "with", "you", "your", "about", "tell", "any", "there"])


class ExtractionError(ValueError):
    pass


def extract_text(data: bytes, kind: str) -> str:
    if kind == "pdf":
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ExtractionError("Encrypted PDFs are not supported")
            if len(reader.pages) > 300:
                raise ExtractionError("PDF has more than 300 pages")
            parts = [(page.extract_text() or "") for page in reader.pages]
        except PdfReadError as exc:
            raise ExtractionError(f"Could not read PDF: {exc}") from exc
        out = "\n\n".join(p.strip() for p in parts if p.strip())
        if not out:
            raise ExtractionError("No extractable text found (scanned PDFs need OCR first)")
        return out
    try:
        return data.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise ExtractionError("Text files must be UTF-8") from exc


def chunk_text(content: str, target: int = 900, overlap: int = 120) -> list[str]:
    content = re.sub(r"[ \t]+", " ", content).strip()
    if not content:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > target * 1.5:  # split very long paragraphs on sentence boundaries
            cut = para.rfind(". ", 0, target)
            cut = cut + 1 if cut > target // 3 else target
            pieces = para[:cut].strip()
            para = para[cut:].strip()
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(pieces)
        if len(buf) + len(para) + 2 <= target:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            tail = buf[-overlap:] if buf and overlap else ""
            buf = f"{tail} {para}".strip() if tail and len(tail) + len(para) < target else para
    if buf:
        chunks.append(buf)
    return chunks


async def index_document(db: AsyncSession, doc: KnowledgeDocument) -> int:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
    chunks = chunk_text(doc.content)
    for i, c in enumerate(chunks):
        db.add(KnowledgeChunk(document_id=doc.id, workspace_id=doc.workspace_id, seq=i, content=c))
    doc.chunk_count = len(chunks)
    await db.flush()
    return len(chunks)


def fts_query(query: str) -> str | None:
    tokens = [t for t in re.findall(r"[\w]+", query.lower()) if t not in _STOP and len(t) > 1][:12]
    if not tokens:
        return None
    # quote every token: user input can never inject FTS5 operators
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))


async def search(db: AsyncSession, workspace_id: str, query: str, limit: int = 3) -> list[KnowledgeSnippet]:
    q = fts_query(query)
    if not q:
        return []
    rows = (await db.execute(
        text("SELECT title, content, document_id, bm25(knowledge_fts) AS score FROM knowledge_fts "
             "WHERE knowledge_fts MATCH :q AND workspace_id = :ws ORDER BY score LIMIT :lim"),
        {"q": q, "ws": workspace_id, "lim": max(1, min(limit, 10))},
    )).all()  # fmt: skip
    return [KnowledgeSnippet(title=r.title, content=r.content, score=-float(r.score), document_id=r.document_id)
            for r in rows]  # fmt: skip


def faq_to_text(items: list[dict[str, str]]) -> str:
    return "\n\n".join(f"Q: {i['question'].strip()}\nA: {i['answer'].strip()}" for i in items
                       if i.get("question") and i.get("answer"))  # fmt: skip
