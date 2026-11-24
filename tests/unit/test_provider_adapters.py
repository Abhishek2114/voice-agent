"""Provider adapters against mocked HTTP servers (no real services needed)."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from engine.audio.pcm import tone, wav_bytes
from engine.providers.llm.base import ChatMessage, LLMError, LLMOptions
from engine.providers.llm.ollama import OllamaProvider
from engine.providers.stt.base import STTError, clean_transcript
from engine.providers.stt.whisper_cpp import WhisperServerSTT
from engine.providers.telephony.asterisk_ari import AriConfig, AsteriskAriProvider
from engine.providers.telephony.base import CallStatus, TelephonyCall, TelephonyError
from engine.providers.telephony.rtp import RtpPacket, payload_to_pcm, pcm_to_payload
from engine.providers.telephony.simulator import (
    PERSONA_SCRIPTS,
    ScriptedCallee,
    SimulatedTelephonyProvider,
    SimulationScenario,
    pick_persona,
)
from engine.providers.tts.base import TTSError, VoiceSettings
from engine.providers.tts.piper_http import PiperHttpTTS

TAGS = {"models": [
    {"name": "qwen3:4b", "size": 2_500_000_000, "details": {"family": "qwen3", "parameter_size": "4B"},
     "capabilities": ["completion", "thinking"]},
    {"name": "glm-5.3:cloud", "remote_host": "https://ollama.com", "size": 300, "details": {}},
]}  # fmt: skip


def ollama_transport(chat_lines: list[dict] | None = None, status: int = 200, captured: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.16"})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if req.url.path == "/api/chat":
            if captured is not None:
                captured.append(json.loads(req.content))
            body = "\n".join(json.dumps(x) for x in (chat_lines or []))
            return httpx.Response(status, content=body.encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_ollama_lists_and_flags_remote_models():
    p = OllamaProvider(transport=ollama_transport())
    models = {m.name: m for m in await p.list_models()}
    assert models["qwen3:4b"].is_local and not models["glm-5.3:cloud"].is_local
    h = await p.health()
    assert h.status == "ok" and h.extra["remote_models"] == ["glm-5.3:cloud"]


async def test_ollama_streams_and_disables_thinking():
    captured: list = []
    lines = [{"message": {"content": "Hello "}}, {"message": {"content": "there."}},
             {"done": True, "eval_count": 3, "eval_duration": 1_000_000}]  # fmt: skip
    p = OllamaProvider(transport=ollama_transport(lines, captured=captured))
    chunks = [c async for c in p.stream_chat([ChatMessage("user", "hi")], LLMOptions(model="qwen3:4b"))]
    assert "".join(c.text for c in chunks) == "Hello there."
    assert chunks[0].metrics["ttft_ms"] >= 0 and chunks[-1].done
    assert captured[0]["think"] is False and captured[0]["options"]["num_predict"] == 220


async def test_ollama_parses_tool_calls():
    lines = [{"message": {"content": "", "tool_calls": [{"function": {"name": "end_call", "arguments": {"reason": "completed"}}}]}},
             {"done": True}]  # fmt: skip
    p = OllamaProvider(transport=ollama_transport(lines))
    chunks = [c async for c in p.stream_chat([ChatMessage("user", "bye")], LLMOptions(model="qwen3:4b"))]
    assert chunks[0].tool_calls[0].name == "end_call" and chunks[0].tool_calls[0].arguments == {"reason": "completed"}


async def test_ollama_refuses_remote_and_unknown_models():
    p = OllamaProvider(transport=ollama_transport())
    with pytest.raises(LLMError) as e:
        async for _ in p.stream_chat([ChatMessage("user", "x")], LLMOptions(model="glm-5.3:cloud")):
            pass
    assert e.value.code == "remote_model_refused"
    with pytest.raises(LLMError) as e:
        async for _ in p.stream_chat([ChatMessage("user", "x")], LLMOptions(model="nope")):
            pass
    assert e.value.code == "model_not_found"


async def test_ollama_unreachable_is_explicit():
    def boom(_req):
        raise httpx.ConnectError("refused")

    p = OllamaProvider(transport=httpx.MockTransport(boom))
    assert (await p.health()).status == "unavailable"
    with pytest.raises(LLMError) as e:
        await p.list_models()
    assert e.value.code == "llm_unavailable"


async def test_ollama_server_error_and_json_mode():
    p = OllamaProvider(transport=ollama_transport([{"error": "out of memory"}]))
    with pytest.raises(LLMError):
        async for _ in p.stream_chat([ChatMessage("user", "x")], LLMOptions(model="qwen3:4b")):
            pass

    def handler(req):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        return httpx.Response(200, json={"message": {"content": '{"outcome": "interested", "summary": "s"}'}})

    p = OllamaProvider(transport=httpx.MockTransport(handler))
    out = await p.chat_json([ChatMessage("user", "x")], LLMOptions(model="qwen3:4b"), {"type": "object"})
    assert out["outcome"] == "interested"


async def test_whisper_server_adapter():
    seen = {}

    def handler(req):
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        seen["body"] = req.content
        return httpx.Response(200, json={"text": " Hello there [BLANK_AUDIO]\n"})

    stt = WhisperServerSTT(transport=httpx.MockTransport(handler))
    assert (await stt.health()).ok
    res = await stt.transcribe(tone(200, 1.0, 48000), 48000)
    assert res.text == "Hello there" and abs(res.audio_seconds - 1.0) < 0.05
    assert b"RIFF" in seen["body"]
    bad = WhisperServerSTT(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(STTError):
        await bad.transcribe(tone(200, 0.5, 16000))


def test_transcript_cleaning():
    assert clean_transcript("[BLANK_AUDIO]", 0.5) == ""
    assert clean_transcript(" Thank you.", 0.6) == ""
    assert clean_transcript(" Thank you.", 2.0) == "Thank you."
    assert clean_transcript("(music) Hi *coughs* there", 2) == "Hi there"


async def test_piper_adapter():
    sent = {}

    def handler(req):
        if req.url.path == "/voices":
            return httpx.Response(200, json={"en_US-x": {"num_speakers": 2, "speaker_id_map": {"a": 0, "b": 1},
                                                         "language": {"code": "en_US"}}})  # fmt: skip
        sent.update(json.loads(req.content))
        return httpx.Response(200, content=wav_bytes(tone(300, 0.5, 22050), 22050))

    tts = PiperHttpTTS(transport=httpx.MockTransport(handler))
    voices = await tts.list_voices()
    assert voices[0].num_speakers == 2 and voices[0].language == "en_US"
    res = await tts.synthesize("Hello", VoiceSettings(voice="en_US-x", speaker="1", speed=1.25))
    assert res.sample_rate == 22050 and abs(res.seconds - 0.5) < 0.01
    assert sent == {"text": "Hello", "voice": "en_US-x", "speaker_id": 1, "length_scale": 0.8}
    with pytest.raises(TTSError):
        await tts.synthesize("   ", VoiceSettings(voice="x"))


# ---------------------------------------------------------------- telephony
async def test_simulator_is_deterministic_and_validates():
    sim = SimulatedTelephonyProvider(SimulationScenario(answer_rate=0.5, busy_rate=0.2, fail_rate=0.1, ring_seconds=0))
    a = await sim.dial("+919812345678", None, seed="job-1")
    b = await sim.dial("+919812345678", None, seed="job-1")
    assert a.status == b.status and a.status in set(CallStatus)
    outcomes = {(await sim.dial("+91981234567", None, seed=f"s{i}")).status for i in range(60)}
    assert {CallStatus.ANSWERED, CallStatus.NO_ANSWER, CallStatus.BUSY} <= outcomes
    with pytest.raises(TelephonyError):
        await SimulatedTelephonyProvider(SimulationScenario(answer_rate=0.9, busy_rate=0.5)).dial("+91", None)
    inbound = await sim.simulate_inbound("+919000000001")
    assert inbound.direction == "inbound" and inbound.status == CallStatus.ANSWERED
    await sim.hangup(inbound)
    assert inbound.status == CallStatus.COMPLETED
    with pytest.raises(TelephonyError):
        inbound.transition(CallStatus.ANSWERED)  # terminal states are final


async def test_scripted_callees_and_persona_choice():
    callee = ScriptedCallee("wrong_number", "Ravi")
    first = await callee.reply("Hi")
    second = await callee.reply("Hello")
    assert first == "Hello?" and "Ravi" in second
    for _ in range(5):
        await callee.reply("x")
    assert await callee.reply("x") is None
    assert pick_persona({"opt_out": 1}, "seed") == "opt_out"
    assert pick_persona({"interested": 1, "callback": 1}, "a") == pick_persona({"interested": 1, "callback": 1}, "a")
    assert set(PERSONA_SCRIPTS) >= {"interested", "not_interested", "callback", "wrong_number", "opt_out"}


def test_rtp_roundtrip():
    pcm = (np.arange(320) - 160).astype(np.int16)
    pkt = RtpPacket(118, 65535, 4294967295, 0xABCDEF, pcm_to_payload(pcm), marker=True)
    decoded = RtpPacket.decode(pkt.encode())
    assert (decoded.payload_type, decoded.sequence, decoded.timestamp, decoded.ssrc, decoded.marker) == (118, 65535, 4294967295, 0xABCDEF, True)
    assert np.array_equal(payload_to_pcm(decoded.payload), pcm)
    assert pcm_to_payload(np.array([1], np.int16)) == b"\x00\x01"  # network byte order
    with pytest.raises(ValueError):
        RtpPacket.decode(b"\x00" * 5)


def test_ari_config_validation():
    errs = AriConfig(base_url="ftp://x", username="", app="bad app!", endpoint_template="PJSIP/x").validate()
    assert len(errs) == 4
    assert AriConfig(username="u", password="p").validate() == []


async def test_ari_health_and_dial_failure_mapping():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/ari/asterisk/info":
            return httpx.Response(200, json={"system": {"version": "22.1.0"}})
        if req.url.path == "/ari/channels" and req.method == "POST":
            return httpx.Response(200, json={"id": req.url.params["channelId"]})
        return httpx.Response(204)

    ari = AsteriskAriProvider(AriConfig(username="va", password="pw", endpoint_template="PJSIP/{number}@trunk"),
                              transport=httpx.MockTransport(handler))  # fmt: skip
    h = await ari.health()
    assert h.ok and "22.1.0" in h.detail and h.is_local is False

    async def no_events():
        return None

    ari.ensure_events = no_events  # type: ignore[method-assign]
    import asyncio

    task = asyncio.create_task(ari.dial("+919812345678", None, ring_timeout_s=5))
    for _ in range(50):
        if ari._waiters:
            break
        await asyncio.sleep(0.01)
    channel_id = next(iter(ari._waiters))
    await ari.handle_event({"type": "ChannelDestroyed", "cause": 17, "cause_txt": "User busy", "channel": {"id": channel_id}})
    call: TelephonyCall = await task
    assert call.status == CallStatus.BUSY
    originate = next(c for c in calls if c[1] == "/ari/channels")
    assert originate[2]["endpoint"] == "PJSIP/+919812345678@trunk" and originate[2]["app"] == "voice-agent"
    with pytest.raises(TelephonyError):
        await ari.dial("not-a-number", None)
    assert "api_key=va:pw" in ari.events_url()


async def test_ari_bad_credentials():
    ari = AsteriskAriProvider(AriConfig(username="va", password="pw"),
                              transport=httpx.MockTransport(lambda r: httpx.Response(401)))  # fmt: skip
    h = await ari.health()
    assert h.status == "unavailable" and "credentials" in h.detail


def test_webrtc_rejects_video_offers():
    from engine.streaming.webrtc import OfferRejectedError, check_offer

    check_offer("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n")
    with pytest.raises(OfferRejectedError):
        check_offer("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n")


async def test_rtp_ignores_packets_from_other_peers():
    from engine.providers.telephony.rtp import RtpMedia

    media = RtpMedia("127.0.0.1", 0)
    got: list = []
    media.set_audio_handler(got.append)
    pcm = np.zeros(320, np.int16)
    good = RtpPacket(118, 1, 0, 111, pcm_to_payload(pcm)).encode()
    media._on_datagram(good, ("10.0.0.2", 4000))
    media._on_datagram(RtpPacket(0, 2, 0, 111, pcm_to_payload(pcm)).encode(), ("10.0.0.9", 4000))  # other host
    media._on_datagram(RtpPacket(118, 3, 0, 999, pcm_to_payload(pcm)).encode(), ("10.0.0.2", 4000))  # other ssrc
    media._on_datagram(good, ("10.0.0.2", 4000))
    assert len(got) == 2 and media.packets_dropped == 2 and media.payload_type == 118
