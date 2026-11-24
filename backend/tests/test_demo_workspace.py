"""The optional ShrotiHost sample workspace imports cleanly and stays simulation-only."""

from __future__ import annotations

import json
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "demo" / "shrotihost" / "workspace.json"


async def test_demo_workspace_imports(owner):
    c = owner["client"]
    r = await c.post("/api/workspaces/import", files={"file": ("workspace.json", DEMO.read_bytes(), "application/json")})
    assert r.status_code == 201, r.text
    rep, ws = r.json()["report"], r.json()["workspace"]["id"]
    assert rep == {"agents": 2, "knowledge": 6, "contacts": 8, "consents": 4, "suppression": 0, "campaigns": 1, "skipped": 0}
    contacts = (await c.get(f"/api/workspaces/{ws}/contacts", params={"page_size": 50})).json()["items"]
    assert all(x["is_fictional"] for x in contacts)
    camps = (await c.get(f"/api/workspaces/{ws}/campaigns")).json()
    assert camps[0]["mode"] == "SIMULATION" and camps[0]["status"] == "draft" and camps[0]["progress"]["total"] == 4
    policy = (await c.get(f"/api/workspaces/{ws}/compliance")).json()
    assert policy["policy"]["pstn_authorized"] is False
    hits = (await c.get(f"/api/workspaces/{ws}/knowledge/search", params={"q": "migration"})).json()
    assert hits


def test_demo_contains_no_price_figures():
    text = json.dumps(json.loads(DEMO.read_text()))
    import re

    assert not re.search(r"(₹|rs\.?\s?\d|inr\s?\d|\$\s?\d)", text, re.IGNORECASE)
