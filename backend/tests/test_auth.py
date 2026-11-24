"""Authentication, sessions, CSRF and rate limiting."""

from __future__ import annotations

from conftest import PASSWORD, create_user, login, make_client, setup_owner


async def test_status_requires_setup_first(client):
    r = await client.get("/api/auth/status")
    assert r.json()["setup_required"] is True and r.json()["authenticated"] is False


async def test_setup_creates_owner_and_session(client):
    data = await setup_owner(client)
    assert data["user"]["is_instance_admin"] is True
    cookie = client.cookies.jar
    session = next(c for c in cookie if c.name == "va_session")
    assert session.has_nonstandard_attr("HttpOnly")
    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["workspaces"][0]["role"] == "owner"


async def test_setup_only_once(client):
    await setup_owner(client)
    async with make_client() as other:
        r = await other.post("/api/auth/setup", json={"name": "X", "email": "x@example.com", "password": PASSWORD,
                                                      "workspace_name": "Evil"})  # fmt: skip
        assert r.status_code == 409


async def test_weak_password_rejected(client):
    r = await client.post("/api/auth/setup", json={"name": "O", "email": "o@example.com", "password": "short",
                                                   "workspace_name": "W"})  # fmt: skip
    assert r.status_code == 400 and r.json()["error"]["code"] == "weak_password"


async def test_login_logout_cycle(client):
    await setup_owner(client)
    async with make_client() as c:
        bad = await c.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password!"})
        assert bad.status_code == 401
        await login(c, "owner@example.com")
        assert (await c.get("/api/auth/me")).status_code == 200
        assert (await c.post("/api/auth/logout")).status_code == 200
        assert (await c.get("/api/auth/me")).status_code == 401


async def test_unknown_user_same_error(client):
    await setup_owner(client)
    r = await client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


async def test_login_rate_limit(client):
    await setup_owner(client)
    async with make_client() as c:
        codes = [(await c.post("/api/auth/login", json={"email": "owner@example.com", "password": "nope-nope-nope"})).status_code
                 for _ in range(7)]  # fmt: skip
        assert codes[:5] == [401] * 5 and codes[5] == 429
        # even the right password is refused while locked out
        r = await c.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
        assert r.status_code == 429


async def test_csrf_required_for_mutations(owner):
    c = owner["client"]
    token = c.csrf
    c.csrf = None
    r = await c.post("/api/workspaces", json={"name": "No CSRF"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = await c.post("/api/workspaces", json={"name": "Bad CSRF"}, headers={"x-csrf-token": "forged"})
    assert r.status_code == 403
    c.csrf = token
    assert (await c.post("/api/workspaces", json={"name": "Good"})).status_code == 201


async def test_cross_origin_mutation_refused(owner):
    c = owner["client"]
    r = await c.post("/api/workspaces", json={"name": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"


async def test_untrusted_host_rejected(client):
    r = await client.get("/api/health", headers={"host": "attacker.example"})
    assert r.status_code == 400


async def test_unauthenticated_access_denied(client):
    await setup_owner(client)
    async with make_client() as anon:
        for path in ("/api/workspaces", "/api/auth/me", "/api/system/health", "/api/system/models", "/api/users"):
            assert (await anon.get(path)).status_code == 401, path


async def test_password_change_revokes_other_sessions(owner):
    c = owner["client"]
    async with make_client() as second:
        await login(second, "owner@example.com")
        r = await c.post("/api/users/me/password", json={"current_password": PASSWORD,
                                                          "new_password": "another-strong-pass-42"})  # fmt: skip
        assert r.status_code == 200
        assert (await second.get("/api/auth/me")).status_code == 401
    assert (await c.get("/api/auth/me")).status_code == 200


async def test_non_admin_cannot_manage_users(owner):
    await create_user(owner["client"], "member@example.com")
    async with make_client() as m:
        await login(m, "member@example.com")
        assert (await m.get("/api/users")).status_code == 403
        assert (await m.post("/api/users", json={"email": "x@example.com", "name": "x", "password": PASSWORD})).status_code == 403


async def test_deactivated_user_loses_access(owner):
    c = owner["client"]
    await create_user(c, "temp@example.com")
    users = (await c.get("/api/users")).json()
    uid = next(u["id"] for u in users if u["email"] == "temp@example.com")
    async with make_client() as t:
        await login(t, "temp@example.com")
        await c.patch(f"/api/users/{uid}", json={"is_active": False})
        assert (await t.get("/api/auth/me")).status_code == 401
