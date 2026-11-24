"""Workspaces, roles, members and compliance policy."""

from __future__ import annotations

from conftest import create_user, login, make_agent, make_client


async def test_create_and_list(owner):
    c = owner["client"]
    r = await c.post("/api/workspaces", json={"name": "ShrotiHost", "timezone": "Asia/Kolkata"})
    assert r.status_code == 201
    names = [w["name"] for w in (await c.get("/api/workspaces")).json()]
    assert names == ["Acme Hosting", "ShrotiHost"]


async def test_invalid_timezone_and_color(owner):
    c = owner["client"]
    assert (await c.post("/api/workspaces", json={"name": "x", "timezone": "Mars/Base"})).status_code == 422
    assert (await c.post("/api/workspaces", json={"name": "x", "brand_color": "red"})).status_code == 422


async def test_role_enforcement(owner):
    c, ws = owner["client"], owner["ws"]
    await create_user(c, "viewer@example.com")
    assert (await c.post(f"/api/workspaces/{ws}/members", json={"email": "viewer@example.com", "role": "viewer"})).status_code == 201
    async with make_client() as v:
        await login(v, "viewer@example.com")
        assert (await v.get(f"/api/workspaces/{ws}/agents")).status_code == 200
        r = await v.post(f"/api/workspaces/{ws}/agents", json={"name": "nope"})
        assert r.status_code == 403
        assert (await v.put(f"/api/workspaces/{ws}/compliance", json={})).status_code == 403
        assert (await v.delete(f"/api/workspaces/{ws}")).status_code == 403


async def test_editor_cannot_escalate(owner):
    c, ws = owner["client"], owner["ws"]
    await create_user(c, "ed@example.com")
    await create_user(c, "other@example.com")
    await c.post(f"/api/workspaces/{ws}/members", json={"email": "ed@example.com", "role": "editor"})
    async with make_client() as e:
        await login(e, "ed@example.com")
        r = await e.post(f"/api/workspaces/{ws}/members", json={"email": "other@example.com", "role": "admin"})
        assert r.status_code == 403  # members require admin


async def test_last_owner_protected(owner):
    c, ws = owner["client"], owner["ws"]
    members = (await c.get(f"/api/workspaces/{ws}/members")).json()
    r = await c.patch(f"/api/workspaces/{ws}/members/{members[0]['id']}", json={"role": "admin"})
    assert r.status_code == 400


async def test_compliance_policy_validation(owner):
    c, ws = owner["client"], owner["ws"]
    policy = (await c.get(f"/api/workspaces/{ws}/compliance")).json()
    assert policy["policy"]["ai_disclosure_required"] is True
    assert policy["pstn_missing"]  # PSTN prerequisites are missing by default
    bad = {**policy["policy"], "calling_window_start": "22:00", "calling_window_end": "09:00"}
    assert (await c.put(f"/api/workspaces/{ws}/compliance", json=bad)).status_code == 400
    good = {**policy["policy"], "calling_window_start": "10:00", "max_calls_per_contact_per_day": 2}
    r = await c.put(f"/api/workspaces/{ws}/compliance", json=good)
    assert r.status_code == 200 and r.json()["policy"]["calling_window_start"] == "10:00"


async def test_export_import_roundtrip(owner):
    c, ws = owner["client"], owner["ws"]
    await make_agent(c, ws)
    await c.post(f"/api/workspaces/{ws}/knowledge", json={"title": "FAQ", "kind": "faq",
                 "faq": [{"question": "Do you offer support?", "answer": "Yes, by email."}]})  # fmt: skip
    await c.post(f"/api/workspaces/{ws}/contacts", json={"first_name": "A", "phone": "+919812345611"})
    exported = await c.get(f"/api/workspaces/{ws}/export")
    assert exported.status_code == 200
    r = await c.post("/api/workspaces/import", files={"file": ("ws.json", exported.content, "application/json")})
    assert r.status_code == 201, r.text
    rep = r.json()["report"]
    assert rep["agents"] == 1 and rep["knowledge"] == 1 and rep["contacts"] == 1
    new_ws = r.json()["workspace"]["id"]
    hits = (await c.get(f"/api/workspaces/{new_ws}/knowledge/search", params={"q": "support"})).json()
    assert hits and "email" in hits[0]["content"]


async def test_import_rejects_garbage(owner):
    c = owner["client"]
    r = await c.post("/api/workspaces/import", files={"file": ("x.json", b"not json", "application/json")})
    assert r.status_code == 400
    r = await c.post("/api/workspaces/import", files={"file": ("x.json", b'{"format":"other"}', "application/json")})
    assert r.status_code == 400


async def test_soft_delete_hides_workspace(owner):
    c = owner["client"]
    ws2 = (await c.post("/api/workspaces", json={"name": "Temp"})).json()["id"]
    assert (await c.delete(f"/api/workspaces/{ws2}")).status_code == 200
    assert (await c.get(f"/api/workspaces/{ws2}")).status_code == 404
