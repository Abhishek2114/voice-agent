"""Contacts, phone normalisation, CSV import workflow and CSV-injection protection."""

from __future__ import annotations

import csv
import io

from conftest import make_contact

from app.contacts.csv_safety import escape_cell, is_formula_like
from app.contacts.phone import normalize_phone


def test_phone_normalisation():
    assert normalize_phone("98123 45678", "IN").e164 == "+919812345678"
    assert normalize_phone("+91-98123-45678").e164 == "+919812345678"
    assert normalize_phone("12345").valid is False
    assert normalize_phone("").error == "empty"
    assert normalize_phone("'+919812345678").e164 == "+919812345678"


def test_formula_detection():
    assert is_formula_like("=HYPERLINK(\"http://x\")")
    assert is_formula_like("@SUM(A1)")
    assert not is_formula_like("+919812345678")
    assert not is_formula_like("-42")
    assert escape_cell("=1+1") == "'=1+1"
    assert escape_cell("+91 98123 45678") == "+91 98123 45678"


async def test_contact_crud_search_and_duplicates(owner):
    c, ws = owner["client"], owner["ws"]
    a = await make_contact(c, ws, "+919812345601", last_name="Kumar", tags=["VIP", "hosting"],
                           custom_fields={"plan": "vps"})  # fmt: skip
    assert a["phone_e164"] == "+919812345601" and a["tags"] == ["vip", "hosting"] and a["custom_fields"] == {"plan": "vps"}
    dup = await c.post(f"/api/workspaces/{ws}/contacts", json={"first_name": "Dup", "phone": "098123 45601"})
    assert dup.status_code == 409 and dup.json()["error"]["duplicate_id"] == a["id"]
    bad = await c.post(f"/api/workspaces/{ws}/contacts", json={"first_name": "Bad", "phone": "123"})
    assert bad.status_code == 400
    for i in range(3):
        await make_contact(c, ws, f"+91981234570{i}", first_name=f"P{i}")
    page = (await c.get(f"/api/workspaces/{ws}/contacts", params={"page_size": 2})).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    assert (await c.get(f"/api/workspaces/{ws}/contacts", params={"q": "kumar"})).json()["total"] == 1
    assert (await c.get(f"/api/workspaces/{ws}/contacts", params={"tag": "vip"})).json()["total"] == 1
    upd = await c.patch(f"/api/workspaces/{ws}/contacts/{a['id']}", json={"status": "interested", "custom_fields": {"plan": "dedicated"}})
    assert upd.json()["status"] == "interested" and upd.json()["custom_fields"] == {"plan": "dedicated"}
    hist = (await c.get(f"/api/workspaces/{ws}/contacts/{a['id']}/history")).json()
    assert {"calls", "consents", "callbacks", "events"} <= set(hist)
    assert (await c.delete(f"/api/workspaces/{ws}/contacts/{a['id']}")).status_code == 200
    assert (await c.get(f"/api/workspaces/{ws}/contacts/{a['id']}")).status_code == 404


async def test_export_neutralises_formulas(owner):
    c, ws = owner["client"], owner["ws"]
    await make_contact(c, ws, first_name="=cmd|' /C calc'!A0", company="@SUM(1+1)")
    r = await c.get(f"/api/workspaces/{ws}/contacts/export")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[1][1].startswith("'=") and rows[1][5].startswith("'@")


async def test_erasure_keeps_suppression(owner):
    c, ws = owner["client"], owner["ws"]
    a = await make_contact(c, ws, "+919812345621")
    r = await c.post(f"/api/workspaces/{ws}/contacts/{a['id']}/erase")
    assert r.status_code == 200
    sup = (await c.get(f"/api/workspaces/{ws}/compliance/suppression")).json()
    assert sup["items"][0]["phone_e164"] == "+919812345621" and sup["items"][0]["reason"] == "erasure"


CSV = (
    "Name,Mobile,Email,Company,Consent Purpose,Consent Source,Consent Evidence,Consent Date,Plan\n"
    "Asha Verma,98123 45631,asha@example.com,Verma Traders,promotional,web_form,Signup form #881,2026-10-01,vps\n"
    "Bad Phone,12345,,,,,,,\n"
    "Asha Again,+91 98123 45631,,,,,,,\n"
    "=HYPERLINK(\"http://evil\"),98123 45632,,@SUM(A1),promotional,web_form,,2026-10-01,\n"
    "No Consent,98123 45633,no@example.com,,,,,,\n"
)


async def test_csv_import_workflow(owner):
    c, ws = owner["client"], owner["ws"]
    await make_contact(c, ws, "+919812345633", first_name="Existing")
    up = await c.post(f"/api/workspaces/{ws}/imports", files={"file": ("leads.csv", CSV.encode(), "text/csv")})
    assert up.status_code == 201, up.text
    job = up.json()
    assert job["row_count"] == 5 and job["mapping"]["Mobile"] == "phone" and job["mapping"]["Name"] == "full_name"
    mapping = {**job["mapping"]}
    val = await c.post(f"/api/workspaces/{ws}/imports/{job['id']}/validate", json={"mapping": mapping, "custom_columns": ["Plan"]})
    assert val.status_code == 200, val.text
    s = val.json()["summary"]
    assert s == {"total": 5, "valid": 2, "invalid": 1, "duplicate_in_file": 1, "duplicate_existing": 1,
                 "with_consent": 1, "warnings": s["warnings"]}  # fmt: skip
    assert s["warnings"] >= 2  # formula neutralised + consent ignored (no evidence)
    done = await c.post(f"/api/workspaces/{ws}/imports/{job['id']}/commit",
                        json={"mapping": mapping, "custom_columns": ["Plan"], "tags": ["Diwali"]})  # fmt: skip
    assert done.status_code == 200
    rep = done.json()["report"]
    assert rep["created"] == 2 and rep["consents_recorded"] == 1 and rep["invalid_rows"][0]["row"] == 3
    contacts = (await c.get(f"/api/workspaces/{ws}/contacts", params={"tag": "diwali"})).json()["items"]
    names = {x["first_name"] for x in contacts}
    assert "Asha" in names and any(n.startswith("'=") for n in names)
    asha = next(x for x in contacts if x["first_name"] == "Asha")
    assert asha["custom_fields"] == {"plan": "vps"}
    consents = (await c.get(f"/api/workspaces/{ws}/compliance/consents", params={"contact_id": asha["id"]})).json()
    assert consents["items"][0]["evidence"].startswith("[imported]")
    # the raw upload is deleted after commit; re-commit is refused
    again = await c.post(f"/api/workspaces/{ws}/imports/{job['id']}/commit", json={"mapping": mapping})
    assert again.status_code == 409


async def test_csv_upload_validation(owner):
    c, ws = owner["client"], owner["ws"]
    url = f"/api/workspaces/{ws}/imports"
    assert (await c.post(url, files={"file": ("x.exe", b"MZ\x90\x00", "text/csv")})).status_code == 400
    assert (await c.post(url, files={"file": ("x.csv", b"\x00\x01\x02binary", "text/csv")})).status_code == 400
    assert (await c.post(url, files={"file": ("x.csv", b"", "text/csv")})).status_code == 400
    dup_headers = b"a,a\n1,2\n"
    assert (await c.post(url, files={"file": ("x.csv", dup_headers, "text/csv")})).status_code == 400
    big = b"phone\n" + b"9812345678\n" * (11 * 1024 * 1024 // 11)
    assert (await c.post(url, files={"file": ("x.csv", big, "text/csv")})).status_code == 413
    ok = await c.post(url, files={"file": ("x.csv", b"phone\n9812345678\n", "text/csv")})
    bad_map = await c.post(f"{url}/{ok.json()['id']}/validate", json={"mapping": {"phone": "password_hash"}})
    assert bad_map.status_code == 422
