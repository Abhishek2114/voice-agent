"""End-to-end WebRTC voice session through the real signalling API.

A real aiortc peer (standing in for the browser) negotiates with the backend, streams
"speech" (tone bursts) as Opus, receives the agent's audio track and the JSON events
on the data channel. Providers are deterministic fakes so this runs in CI; the same
path with whisper.cpp/Piper/Ollama is covered by tests/integration/test_real_providers.py.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import time

import av
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from app.audio.sessions import manager

from conftest import make_agent
from engine.audio.pcm import silence, tone
from engine.providers.stt.fake import FakeSTT
from engine.providers.tts.fake import FakeTTS

RATE = 48_000


class ScriptedMic(MediaStreamTrack):
    """Microphone stand-in: silence, then a 0.6 s 'utterance', then silence (repeats)."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        burst = np.concatenate([silence(1.5, RATE), tone(220, 0.6, RATE, 0.4), silence(1.5, RATE)])
        self.audio = burst
        self.pos = 0
        self.pts = 0
        self.start: float | None = None

    async def recv(self) -> av.AudioFrame:
        n = 960
        if self.start is None:
            self.start = time.monotonic()
        wait = self.start + self.pts / RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        chunk = np.take(self.audio, range(self.pos, self.pos + n), mode="wrap")
        self.pos = (self.pos + n) % self.audio.size
        frame = av.AudioFrame.from_ndarray(chunk.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = RATE, self.pts, fractions.Fraction(1, RATE)
        self.pts += n
        return frame


async def test_browser_like_webrtc_conversation(owner, hub):
    hub.overrides["stt"] = FakeSTT(["What hosting plans do you have?", "Okay thanks, bye"])
    hub.overrides["tts"] = FakeTTS(sample_rate=22050, seconds_per_char=0.01)
    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    events: list[dict] = []
    channel = pc.createDataChannel("events")
    channel.on("message", lambda m: events.append(json.loads(m)))
    pc.addTrack(ScriptedMic())
    received_frames = []
    loud_frames = 0

    @pc.on("track")
    def on_track(track):
        async def consume():
            nonlocal loud_frames
            while True:
                try:
                    f = await track.recv()
                except Exception:  # noqa: BLE001 - track ended
                    return
                received_frames.append(f)
                if np.abs(f.to_ndarray()).max() > 300:
                    loud_frames += 1

        asyncio.ensure_future(consume())

    await pc.setLocalDescription(await pc.createOffer())
    r = await c.post(f"/api/workspaces/{ws}/voice/sessions",
                     json={"agent_id": agent["id"], "sdp": pc.localDescription.sdp, "type": "offer"})  # fmt: skip
    assert r.status_code == 201, r.text
    call_id = r.json()["call_id"]
    await pc.setRemoteDescription(RTCSessionDescription(r.json()["sdp"], r.json()["type"]))

    async def wait_for(pred, timeout=25.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if any(pred(e) for e in events):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out; events={[e.get('type') for e in events]}")

    await wait_for(lambda e: e["type"] == "transcript" and e["role"] == "user")
    await wait_for(lambda e: e["type"] == "transcript" and e["role"] == "assistant" and "Thanks for asking" in e["text"])
    await wait_for(lambda e: e["type"] == "ended", timeout=30)
    live = manager.sessions.get(call_id)
    if live:
        await asyncio.wait_for(live.finalized.wait(), 20)
    await pc.close()

    assert received_frames, "no audio received from the agent"
    assert loud_frames > 10, "agent audio track carried no synthesized speech"
    detail = (await c.get(f"/api/workspaces/{ws}/calls/{call_id}")).json()
    assert detail["mode"] == "WEBRTC" and detail["status"] == "completed"
    roles = [m["role"] for m in detail["messages"]]
    assert roles[0] == "assistant" and "user" in roles
    assert detail["outcome"] == "interested" and detail["summary"]
    assert detail["metrics"]["turns"][0].get("first_audio_ms") is not None
    states = [e["state"] for e in events if e["type"] == "state"]
    assert "listening" in states and "speaking" in states


async def test_voice_session_preflight_reports_missing_model(owner, hub):
    from engine.providers.llm.base import ModelInfo
    from engine.providers.llm.scripted import ScriptedLLM

    class NoModels(ScriptedLLM):
        async def list_models(self, force=False):
            return [ModelInfo("something-else")]

    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)
    hub.overrides["llm"] = NoModels()
    r = await c.post(f"/api/workspaces/{ws}/voice/sessions", json={"agent_id": agent["id"], "sdp": "v=0\r\nfake-sdp-offer"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_not_found"


async def test_voice_capacity_limit(owner, monkeypatch):
    from app.core.config import get_settings

    c, ws = owner["client"], owner["ws"]
    agent = await make_agent(c, ws)
    monkeypatch.setattr(get_settings(), "max_voice_sessions", 0)
    r = await c.post(f"/api/workspaces/{ws}/voice/sessions", json={"agent_id": agent["id"], "sdp": "v=0\r\nfake-sdp-offer"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "capacity"
