"""Proves that one workspace cannot read or act on another workspace's data."""

from __future__ import annotations

import asyncio

from app.database.models import Recording
from app.database.session import sessionmaker

from conftest import create_user, login, make_agent, make_client, make_contact


async def _two_tenants(owner):
    """Workspace A (owner) with data; user B owns only workspace B."""
    a, ws_a = owner["client"], owner["ws"]
    agent = await make_agent(a, ws_a)
    contact = await make_contact(a, ws_a)
    doc = (await a.post(f"/api/workspaces/{ws_a}/knowledge", json={"title": "Secret pricing", "kind": "pricing",
           "content": "Confidential enterprise discount code ALPHA-42"})).json()  # fmt: skip
    sim = (await a.post(f"/api/workspaces/{ws_a}/calls/simulate", json={"agent_id": agent["id"],
           "contact_id": contact["id"], "persona": "interested"})).json()  # fmt: skip
    for _ in range(100):
        detail = (await a.get(f"/api/workspaces/{ws_a}/calls/{sim['call_id']}")).json()
        if detail["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    async with sessionmaker()() as db:
        db.add(Recording(call_id=sim["call_id"], workspace_id=ws_a, relative_path=f"recordings/{ws_a}/x.wav"))
        await db.commit()
    await create_user(a, "b@example.com")
    b = make_client()
    await b.__aenter__()
    await login(b, "b@example.com")
    ws_b = (await b.post("/api/workspaces", json={"name": "Tenant B"})).json()["id"]
    return a, b, ws_a, ws_b, agent, contact, doc, sim["call_id"]


async def test_cross_workspace_reads_are_404(owner):
    _a, b, ws_a, _ws_b, agent, contact, doc, call_id = await _two_tenants(owner)
    try:
        paths = [
            f"/api/workspaces/{ws_a}", f"/api/workspaces/{ws_a}/contacts", f"/api/workspaces/{ws_a}/contacts/{contact['id']}",
            f"/api/workspaces/{ws_a}/calls", f"/api/workspaces/{ws_a}/calls/{call_id}",
            f"/api/workspaces/{ws_a}/calls/{call_id}/recording", f"/api/workspaces/{ws_a}/knowledge",
            f"/api/workspaces/{ws_a}/knowledge/{doc['id']}", f"/api/workspaces/{ws_a}/agents/{agent['id']}",
            f"/api/workspaces/{ws_a}/analytics/overview", f"/api/workspaces/{ws_a}/compliance/consents",
            f"/api/workspaces/{ws_a}/export",
        ]  # fmt: skip
        for p in paths:
            r = await b.get(p)
            assert r.status_code == 404, (p, r.status_code)
    finally:
        await b.__aexit__(None, None, None)


async def test_foreign_ids_inside_own_workspace_are_404(owner):
    _a, b, _ws_a, ws_b, agent, contact, doc, call_id = await _two_tenants(owner)
    try:
        # B passes A's resource ids through B's own workspace path
        assert (await b.get(f"/api/workspaces/{ws_b}/contacts/{contact['id']}")).status_code == 404
        assert (await b.get(f"/api/workspaces/{ws_b}/calls/{call_id}")).status_code == 404
        assert (await b.get(f"/api/workspaces/{ws_b}/calls/{call_id}/recording")).status_code == 404
        assert (await b.get(f"/api/workspaces/{ws_b}/knowledge/{doc['id']}")).status_code == 404
        assert (await b.get(f"/api/workspaces/{ws_b}/agents/{agent['id']}")).status_code == 404
        assert (await b.patch(f"/api/workspaces/{ws_b}/contacts/{contact['id']}", json={"notes": "pwned"})).status_code == 404
        assert (await b.delete(f"/api/workspaces/{ws_b}/contacts/{contact['id']}")).status_code == 404
        r = await b.post(f"/api/workspaces/{ws_b}/calls/simulate", json={"agent_id": agent["id"]})
        assert r.status_code == 404
        r = await b.post(f"/api/workspaces/{ws_b}/campaigns", json={"name": "x", "agent_id": agent["id"]})
        assert r.status_code == 404
        r = await b.post(f"/api/workspaces/{ws_b}/compliance/consents", json={"contact_id": contact["id"],
                         "purpose": "promotional", "source": "written", "evidence": "forged"})  # fmt: skip
        assert r.status_code == 404
        r = await b.post(f"/api/workspaces/{ws_b}/voice/sessions", json={"agent_id": agent["id"], "sdp": "v=0 fake sdp"})
        assert r.status_code == 404
    finally:
        await b.__aexit__(None, None, None)


async def test_knowledge_search_is_workspace_scoped(owner):
    a, b, ws_a, ws_b, *_ = await _two_tenants(owner)
    try:
        await b.post(f"/api/workspaces/{ws_b}/knowledge", json={"title": "B doc", "content": "Tenant B public info"})
        hits_b = (await b.get(f"/api/workspaces/{ws_b}/knowledge/search", params={"q": "confidential discount ALPHA"})).json()
        assert hits_b == []
        hits_a = (await a.get(f"/api/workspaces/{ws_a}/knowledge/search", params={"q": "discount"})).json()
        assert hits_a and "ALPHA-42" in hits_a[0]["content"]
    finally:
        await b.__aexit__(None, None, None)


async def test_transcripts_not_visible_across_workspaces(owner):
    a, b, ws_a, ws_b, *_ , call_id = await _two_tenants(owner)
    try:
        calls_b = (await b.get(f"/api/workspaces/{ws_b}/calls", params={"q": "hosting"})).json()
        assert calls_b["total"] == 0
        detail = (await a.get(f"/api/workspaces/{ws_a}/calls/{call_id}")).json()
        assert detail["messages"]
    finally:
        await b.__aexit__(None, None, None)


async def test_client_supplied_workspace_id_in_body_is_ignored(owner):
    a, b, ws_a, ws_b, *_ = await _two_tenants(owner)
    try:
        r = await b.post(f"/api/workspaces/{ws_b}/contacts", json={"first_name": "x", "phone": "+919812345699",
                                                                    "workspace_id": ws_a})  # fmt: skip
        assert r.status_code == 201
        assert (await a.get(f"/api/workspaces/{ws_a}/contacts", params={"q": "+919812345699"})).json()["total"] == 0
    finally:
        await b.__aexit__(None, None, None)
