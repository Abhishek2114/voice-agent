"""Knowledge base (extraction, FTS retrieval) and agent builder."""

from __future__ import annotations

from conftest import make_agent
from engine.providers.llm.base import ModelInfo
from engine.providers.llm.scripted import ScriptedLLM

from app.knowledge.service import chunk_text, fts_query


def _pdf(text: str) -> bytes:
    """Minimal valid single-page PDF with one text line."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]  # fmt: skip
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_chunking_and_query_sanitising():
    chunks = chunk_text("\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(10)))
    assert len(chunks) > 3 and all(len(c) <= 1400 for c in chunks)
    assert fts_query('price" OR 1=1; DROP TABLE x --') == '"price" OR "drop" OR "table"'
    assert fts_query("the and of") is None


async def test_knowledge_crud_upload_search(owner):
    c, ws = owner["client"], owner["ws"]
    faq = await c.post(f"/api/workspaces/{ws}/knowledge", json={"title": "Support FAQ", "kind": "faq", "faq": [
        {"question": "What are your support hours?", "answer": "Support is available 24x7 via ticket."},
        {"question": "Do you offer backups?", "answer": "Daily backups are included on all plans."}]})  # fmt: skip
    assert faq.status_code == 201 and faq.json()["chunk_count"] >= 1
    up = await c.post(f"/api/workspaces/{ws}/knowledge/upload", data={"kind": "policy", "title": "Refunds"},
                      files={"file": ("refund.md", b"# Refund policy\n\nRefunds are handled by the billing team within 7 days.", "text/markdown")})  # fmt: skip
    assert up.status_code == 201, up.text
    pdf = await c.post(f"/api/workspaces/{ws}/knowledge/upload", data={"kind": "product"},
                       files={"file": ("plans.pdf", _pdf("Managed VPS includes monitoring"), "application/pdf")})  # fmt: skip
    assert pdf.status_code == 201, pdf.text
    hits = (await c.get(f"/api/workspaces/{ws}/knowledge/search", params={"q": "how do refunds work?"})).json()
    assert hits[0]["title"] == "Refunds"
    hits = (await c.get(f"/api/workspaces/{ws}/knowledge/search", params={"q": "VPS monitoring"})).json()
    assert hits and "monitoring" in hits[0]["content"]
    # injection-style query must not error
    r = await c.get(f"/api/workspaces/{ws}/knowledge/search", params={"q": 'backup" NEAR(x) * OR "'})
    assert r.status_code == 200
    # deleting a document removes it from the index
    await c.delete(f"/api/workspaces/{ws}/knowledge/{up.json()['id']}")
    assert (await c.get(f"/api/workspaces/{ws}/knowledge/search", params={"q": "refunds billing"})).json() == []


async def test_knowledge_rejects_bad_files(owner):
    c, ws = owner["client"], owner["ws"]
    url = f"/api/workspaces/{ws}/knowledge/upload"
    assert (await c.post(url, files={"file": ("a.docx", b"PK\x03\x04zip", "application/octet-stream")})).status_code == 400
    assert (await c.post(url, files={"file": ("a.txt", b"%PDF-1.4 fake", "text/plain")})).status_code == 400
    assert (await c.post(url, files={"file": ("a.pdf", b"%PDF-1.4 broken", "application/pdf")})).status_code == 400
    assert (await c.post(url, files={"file": ("a.sh", b"echo hi", "text/plain")})).status_code == 400


async def test_agent_lifecycle_versions(owner):
    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws, objectives=["Qualify leads"], tools=["end_call", "capture_lead"])
    assert a["version"] == 1 and a["tools"] == ["end_call", "capture_lead"]
    body = {**a, "greeting": "Namaste! How may I help?", "version_note": "New greeting"}
    for k in ("id", "version", "created_at", "updated_at", "stats", "public_link_enabled"):
        body.pop(k)
    upd = (await c.put(f"/api/workspaces/{ws}/agents/{a['id']}", json=body)).json()
    assert upd["version"] == 2
    versions = (await c.get(f"/api/workspaces/{ws}/agents/{a['id']}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    restored = (await c.post(f"/api/workspaces/{ws}/agents/{a['id']}/versions/1/restore")).json()
    assert restored["greeting"] == a["greeting"] and restored["version"] == 3
    dup = (await c.post(f"/api/workspaces/{ws}/agents/{a['id']}/duplicate")).json()
    assert dup["name"].endswith("(copy)") and dup["is_active"] is False
    off = (await c.post(f"/api/workspaces/{ws}/agents/{a['id']}/activate", params={"active": False})).json()
    assert off["is_active"] is False
    assert (await c.delete(f"/api/workspaces/{ws}/agents/{dup['id']}")).status_code == 200
    assert len((await c.get(f"/api/workspaces/{ws}/agents")).json()) == 1


async def test_agent_validation(owner):
    c, ws = owner["client"], owner["ws"]
    r = await c.post(f"/api/workspaces/{ws}/agents", json={"name": "x", "tools": ["run_shell"]})
    assert r.status_code == 422
    r = await c.post(f"/api/workspaces/{ws}/agents", json={"name": "x", "llm": {"model": "not-installed"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_found"
    r = await c.post(f"/api/workspaces/{ws}/agents", json={"name": "", "max_duration_seconds": 5})
    assert r.status_code == 422


async def test_remote_model_refused(owner, hub):
    class CloudAware(ScriptedLLM):
        async def list_models(self, force=False):
            return [ModelInfo("scripted"), ModelInfo("glm-5.3:cloud", is_local=False, remote_host="https://ollama.com")]

    hub.overrides["llm"] = CloudAware()
    c, ws = owner["client"], owner["ws"]
    r = await c.post(f"/api/workspaces/{ws}/agents", json={"name": "x", "llm": {"model": "glm-5.3:cloud"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "remote_model_refused"


async def test_agent_test_console(owner, llm):
    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    r = await c.post(f"/api/workspaces/{ws}/agents/{a['id']}/test", json={"message": "What do you offer?"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["reply"].startswith("Thanks for asking")
    assert "AI assistant" in data["greeting"]  # disclosure prepended
    assert "Never claim to have completed a booking" in data["system_prompt"]


async def test_agent_test_console_reports_llm_outage(owner, hub):
    from engine.providers.llm.base import LLMError

    c, ws = owner["client"], owner["ws"]
    a = await make_agent(c, ws)
    hub.overrides["llm"] = ScriptedLLM(fail_with=LLMError("Ollama down", code="llm_unavailable"))
    r = await c.post(f"/api/workspaces/{ws}/agents/{a['id']}/test", json={"message": "hi"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_unavailable"
