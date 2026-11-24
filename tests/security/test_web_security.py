"""Web security: injection, XSS handling, headers, uploads, public links, SSRF validator."""

from __future__ import annotations

import pytest
from app.core.logging import redact
from app.core.ssrf import UnsafeURLError, validate_provider_url

from conftest import make_agent, make_client, make_contact

SQLI = ["' OR '1'='1", "1; DROP TABLE contacts; --", "%' UNION SELECT password_hash FROM users --", '") OR 1=1 --']


async def test_sql_injection_in_filters_is_inert(owner):
    c, ws = owner["client"], owner["ws"]
    await make_contact(c, ws)
    for payload in SQLI:
        for path in ("contacts", "calls", "compliance/suppression"):
            r = await c.get(f"/api/workspaces/{ws}/{path}", params={"q": payload})
            assert r.status_code in (200, 422), (path, payload)  # 422 = rejected by length validation
            if r.status_code == 200:
                assert r.json()["total"] == 0
        r = await c.get(f"/api/workspaces/{ws}/knowledge/search", params={"q": payload})
        assert r.status_code == 200
    # data still intact
    assert (await c.get(f"/api/workspaces/{ws}/contacts")).json()["total"] == 1
    r = await c.get(f"/api/workspaces/{ws}/contacts", params={"sort": "id; DROP TABLE users"})
    assert r.status_code == 422


async def test_xss_payload_is_stored_as_data_and_served_as_json(owner):
    c, ws = owner["client"], owner["ws"]
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    created = await make_contact(c, ws, first_name=payload, notes=payload)
    r = await c.get(f"/api/workspaces/{ws}/contacts/{created['id']}")
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["first_name"] == payload  # stored verbatim; the UI renders text, never HTML
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_security_headers_present(client):
    r = await client.get("/api/health")
    h = r.headers
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"
    assert "microphone=(self)" in h["permissions-policy"]


async def test_cors_only_allows_local_dashboard(client):
    r = await client.options("/api/auth/login", headers={"origin": "https://evil.example",
                             "access-control-request-method": "POST"})  # fmt: skip
    assert "access-control-allow-origin" not in r.headers
    r = await client.options("/api/auth/login", headers={"origin": "http://localhost:3000",
                             "access-control-request-method": "POST"})  # fmt: skip
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_request_validation_rejects_oversized_and_malformed(owner):
    c, ws = owner["client"], owner["ws"]
    r = await c.post(f"/api/workspaces/{ws}/contacts", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422
    r = await c.post(f"/api/workspaces/{ws}/agents", json={"name": "x" * 500})
    assert r.status_code == 422
    r = await c.post(f"/api/workspaces/{ws}/contacts", json={"first_name": "x", "custom_fields": {"Bad Key!": "v"}})
    assert r.status_code == 422


async def test_logo_upload_validates_content(owner):
    c, ws = owner["client"], owner["ws"]
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert (await c.post(f"/api/workspaces/{ws}/logo", files={"file": ("x.png", svg, "image/png")})).status_code == 400
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert (await c.post(f"/api/workspaces/{ws}/logo", files={"file": ("../../x.png", png, "image/png")})).status_code == 200
    r = await c.get(f"/api/workspaces/{ws}/logo")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


async def test_public_link_lifecycle_and_rate_limit(owner):
    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)
    assert (await c.get("/api/public/talk/" + "x" * 32)).status_code == 404
    link = (await c.post(f"/api/workspaces/{ws}/agents/{agent['id']}/public-link", params={"enabled": True})).json()
    token = link["token"]
    async with make_client() as anon:
        info = await anon.get(f"/api/public/talk/{token}")
        assert info.status_code == 200 and "AI assistant" in info.json()["ai_notice"]
        assert "id" not in info.json() and ws not in info.text  # no internal identifiers leak
        codes = [(await anon.post(f"/api/public/talk/{token}/sessions", json={"sdp": "v=0 invalid offer"})).status_code
                 for _ in range(6)]  # fmt: skip
        assert codes[-1] == 429
    await c.post(f"/api/workspaces/{ws}/agents/{agent['id']}/public-link", params={"enabled": False})
    async with make_client() as anon:
        assert (await anon.get(f"/api/public/talk/{token}")).status_code == 404


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/", "http://[fe80::1]/", "http://0.0.0.0:80", "gopher://127.0.0.1",
    "http://8.8.8.8/", "http://127.0.0.1#frag", "http://admin:x@127.0.0.1", "http://224.0.0.1",
])  # fmt: skip
def test_ssrf_validator_blocks(url):
    with pytest.raises(UnsafeURLError):
        validate_provider_url(url)


def test_ssrf_validator_allows_local():
    assert validate_provider_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert validate_provider_url("http://localhost:8178") == "http://localhost:8178"
    assert validate_provider_url("http://192.168.1.10:8088")


def test_log_redaction():
    line = "login password=hunter2 for ravi@example.com phone +91 98123 45678 token: abc.def"
    out = redact(line)
    assert "hunter2" not in out and "ravi@example.com" not in out and "98123" not in out and "abc.def" not in out
