"""Shared fixtures: isolated data dir + fresh migrated SQLite DB per test, fake providers."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

_TMP = tempfile.mkdtemp(prefix="va-test-")
os.environ.update({
    "VA_ENV": "test",
    "VA_DATA_DIR": _TMP,
    "VA_SECRET_KEY": "test-secret-key-not-for-production-0123456789",
    "VA_VAD_KIND": "energy",
    "VA_PUBLIC_CALLS_ENABLED": "true",
    "VA_LOGIN_MAX_ATTEMPTS": "5",
    "VA_ALLOWED_HOSTS": '["localhost", "127.0.0.1", "testserver"]',
})  # fmt: skip

import httpx
import pytest
from app.ai.hub import ProviderHub, set_hub
from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.database.migrate import upgrade_database
from app.database.session import dispose_engine, init_engine
from app.main import app as fastapi_app

from engine.audio.vad import EnergyVAD
from engine.providers.llm.scripted import ScriptedLLM
from engine.providers.stt.fake import FakeSTT
from engine.providers.tts.fake import FakeTTS

PASSWORD = "correct-horse-battery-staple"
JSON_RESULT = {"outcome": "interested", "summary": "The caller asked about hosting and wants a follow-up.",
               "sentiment": "positive", "key_points": ["hosting"]}  # fmt: skip


def default_responder(messages: list[Any]) -> str:
    last = messages[-1].content.lower() if messages else ""
    if "bye" in last:
        return "Thank you for your time. Goodbye!"
    return "Thanks for asking. Our team can help with that. Is there anything else?"


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM(default_responder, json_result=JSON_RESULT)


@pytest.fixture
def hub(llm: ScriptedLLM) -> ProviderHub:
    h = ProviderHub(get_settings())
    h.overrides = {"llm": llm, "stt": FakeSTT(["hello there", "tell me about plans", "bye"]),
                   "tts": FakeTTS(sample_rate=16000, seconds_per_char=0.002), "vad": EnergyVAD}  # fmt: skip
    set_hub(h)
    return h


@pytest.fixture
async def db_url(tmp_path: Path) -> AsyncIterator[str]:
    settings = get_settings()
    settings.ensure_dirs()
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    init_engine(url)
    await upgrade_database()
    limiter.reset()
    yield url
    await dispose_engine()


class Client(httpx.AsyncClient):
    csrf: str | None = None

    async def request(self, method: str, url: Any, **kw: Any) -> httpx.Response:  # type: ignore[override]
        if self.csrf and method.upper() not in ("GET", "HEAD", "OPTIONS"):
            kw.setdefault("headers", {})
            kw["headers"] = {"x-csrf-token": self.csrf, **(kw["headers"] or {})}
        resp = await super().request(method, url, **kw)
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("csrf_token"):
                    self.csrf = body["csrf_token"]
            except ValueError:
                pass
        return resp


def make_client() -> Client:
    return Client(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver")


@pytest.fixture
async def client(db_url: str, hub: ProviderHub) -> AsyncIterator[Client]:
    async with make_client() as c:
        yield c


async def setup_owner(c: Client, email: str = "owner@example.com", ws: str = "Acme Hosting") -> dict[str, Any]:
    r = await c.post("/api/auth/setup", json={"name": "Owner", "email": email, "password": PASSWORD, "workspace_name": ws})
    assert r.status_code == 201, r.text
    return r.json()


async def create_user(admin: Client, email: str) -> None:
    r = await admin.post("/api/users", json={"email": email, "name": email.split("@")[0], "password": PASSWORD})
    assert r.status_code == 201, r.text


async def login(c: Client, email: str) -> None:
    r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
async def owner(client: Client) -> dict[str, Any]:
    """Logged-in owner with one workspace: returns {"client", "ws"}."""
    data = await setup_owner(client)
    return {"client": client, "ws": data["workspace_id"]}


async def make_agent(c: Client, ws: str, **over: Any) -> dict[str, Any]:
    body = {"name": "Asha", "greeting": "How can I help you today?", "llm": {"model": "scripted"},
            "tts": {"provider": "piper", "voice": "fake"}, **over}  # fmt: skip
    r = await c.post(f"/api/workspaces/{ws}/agents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def make_contact(c: Client, ws: str, phone: str = "+919812345601", **over: Any) -> dict[str, Any]:
    r = await c.post(f"/api/workspaces/{ws}/contacts", json={"first_name": "Ravi", "phone": phone, **over})
    assert r.status_code == 201, r.text
    return r.json()
