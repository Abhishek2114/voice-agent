"""Browser WebRTC transport built on aiortc.

* Inbound: the browser's microphone track (Opus) is decoded by aiortc, resampled to
  16 kHz mono and pushed into the pipeline.
* Outbound: :class:`SinkAudioTrack` pulls 20 ms frames from the pipeline's sink in
  real time and aiortc encodes them as Opus.
* Control/events: a data channel named ``events`` (created by the browser) carries JSON
  messages in both directions.
* ICE: no STUN/TURN servers by default — media stays on the local machine/LAN, and
  nothing is sent to third-party ICE servers. An optional STUN URL enables remote callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from engine.audio.recorder import CallRecorder
from engine.streaming.sink import QueueAudioSink

log = logging.getLogger(__name__)

OUT_RATE = 48_000
FRAME_SAMPLES = 960  # 20 ms


class SinkAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, sink: QueueAudioSink, recorder: CallRecorder | None = None) -> None:
        super().__init__()
        self.sink = sink
        self.recorder = recorder
        self._start: float | None = None
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.monotonic()
        else:
            self._pts += FRAME_SAMPLES
            wait = self._start + self._pts / OUT_RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        pcm = self.sink.read(FRAME_SAMPLES)
        if self.recorder:
            self.recorder.add_agent(pcm, OUT_RATE)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = OUT_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, OUT_RATE)
        return frame


ControlHandler = Callable[[dict[str, Any]], Awaitable[None]]
AudioHandler = Callable[[Any], None]


class OfferRejectedError(ValueError):
    pass


def check_offer(sdp: str) -> None:
    """Accept only audio (+ the events data channel). Rejecting video before negotiation keeps
    FFmpeg's video decoders off the attack surface entirely."""
    for line in sdp.splitlines():
        if line.startswith("m="):
            media = line[2:].split(" ", 1)[0]
            if media not in ("audio", "application"):
                raise OfferRejectedError(f"unsupported media section: {media}")


class WebRTCTransport:
    def __init__(
        self,
        sink: QueueAudioSink,
        *,
        recorder: CallRecorder | None = None,
        on_control: ControlHandler | None = None,
        on_closed: Callable[[str], Awaitable[None]] | None = None,
        stun_url: str = "",
    ) -> None:
        self.sink = sink
        self.recorder = recorder
        self.on_control = on_control
        self.on_closed = on_closed
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=stun_url)] if stun_url else []))
        self.out_track = SinkAudioTrack(sink, recorder)
        self.channel: Any = None
        self._pending: list[str] = []
        self._audio_handler: AudioHandler | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self.connected = asyncio.Event()
        self.frames_in = 0

    def set_audio_handler(self, handler: AudioHandler) -> None:
        self._audio_handler = handler

    async def accept_offer(self, sdp: str, sdp_type: str = "offer") -> RTCSessionDescription:
        check_offer(sdp)
        pc = self.pc

        @pc.on("datachannel")
        def _on_channel(channel: Any) -> None:
            self.channel = channel

            @channel.on("open")
            def _open() -> None:
                self._flush()

            @channel.on("message")
            def _on_message(message: Any) -> None:
                if not isinstance(message, str) or len(message) > 8192 or self.on_control is None:
                    return
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    return
                if isinstance(data, dict):
                    self._tasks.append(asyncio.ensure_future(self.on_control(data)))

            if channel.readyState == "open":
                self._flush()

        @pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.ensure_future(self._consume(track)))

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            state = pc.connectionState
            log.info("webrtc connection state: %s", state)
            if state == "connected":
                self.connected.set()
            elif state in ("failed", "closed"):
                await self.close(f"connection_{state}")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        pc.addTrack(self.out_track)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return pc.localDescription

    async def _consume(self, track: MediaStreamTrack) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16_000)
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                break
            self.frames_in += 1
            for f in resampler.resample(frame):  # type: ignore[arg-type]  # audio tracks yield AudioFrame
                pcm = f.to_ndarray().reshape(-1)
                if self.recorder:
                    self.recorder.add_user(pcm)
                if self._audio_handler:
                    self._audio_handler(pcm)
        if not self._closed:
            await self.close("remote_track_ended")

    def send_event(self, kind: str, data: dict[str, Any]) -> None:
        msg = json.dumps({"type": kind, **data}, default=str)
        ch = self.channel
        if ch is not None and ch.readyState == "open":
            ch.send(msg)
        elif len(self._pending) < 500:
            self._pending.append(msg)

    def _flush(self) -> None:
        ch = self.channel
        while self._pending and ch is not None and ch.readyState == "open":
            ch.send(self._pending.pop(0))

    async def close(self, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self.out_track.stop()
        for t in self._tasks:
            if not t.done() and t is not asyncio.current_task():
                t.cancel()
        with contextlib.suppress(Exception):
            await self.pc.close()
        if self.on_closed:
            await self.on_closed(reason)
