"""Minimal RTP endpoint for Asterisk ``externalMedia`` (slin16, 20 ms packets).

RFC 3550 fixed header, no CSRC/extension on send. Linear PCM in RTP (L16) is
big-endian (network byte order, RFC 3551 §4.5.11); ``byte_order`` is configurable
because some deployments transcode differently.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from engine.streaming.sink import QueueAudioSink

RTP_RATE = 16_000
PACKET_SAMPLES = 320  # 20 ms


@dataclass
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False

    def encode(self) -> bytes:
        b0 = 0x80  # version 2
        b1 = (0x80 if self.marker else 0) | (self.payload_type & 0x7F)
        return struct.pack("!BBHII", b0, b1, self.sequence & 0xFFFF, self.timestamp & 0xFFFFFFFF, self.ssrc) + self.payload

    @classmethod
    def decode(cls, data: bytes) -> RtpPacket:
        if len(data) < 12:
            raise ValueError("RTP packet too short")
        b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
        if b0 >> 6 != 2:
            raise ValueError("not RTP v2")
        csrc = b0 & 0x0F
        offset = 12 + 4 * csrc
        if b0 & 0x10:  # header extension
            if len(data) < offset + 4:
                raise ValueError("truncated extension")
            ext_len = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
            offset += 4 + 4 * ext_len
        payload = data[offset:]
        if b0 & 0x20 and payload:  # padding
            payload = payload[: -payload[-1]]
        return cls(b1 & 0x7F, seq, ts, ssrc, payload, bool(b1 & 0x80))


def pcm_to_payload(pcm: np.ndarray, byte_order: str = "big") -> bytes:
    return pcm.astype(">i2" if byte_order == "big" else "<i2").tobytes()


def payload_to_pcm(payload: bytes, byte_order: str = "big") -> np.ndarray:
    if len(payload) % 2:
        payload = payload[:-1]
    return np.frombuffer(payload, dtype=">i2" if byte_order == "big" else "<i2").astype(np.int16)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: RtpMedia) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner._on_datagram(data, addr)


class RtpMedia:
    """Bidirectional RTP media for one call. Implements the ``CallMedia`` protocol."""

    def __init__(self, bind_host: str, bind_port: int, *, byte_order: str = "big", payload_type: int = 118) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.byte_order = byte_order
        self.payload_type = payload_type
        self.sink = QueueAudioSink(RTP_RATE)
        self.remote: tuple[str, int] | None = None
        self._handler: Callable[[Any], None] | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._sender: asyncio.Task[None] | None = None
        self._seq = random.randint(0, 0xFFFF)
        self._ts = random.randint(0, 0xFFFFFFFF)
        self._ssrc = random.randint(1, 0xFFFFFFFF)
        self.packets_in = 0
        self.packets_out = 0
        self.packets_dropped = 0
        self._remote_ssrc: int | None = None

    def set_audio_handler(self, handler: Callable[[Any], None]) -> None:
        self._handler = handler

    async def start(self, remote: tuple[str, int] | None = None) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), local_addr=(self.bind_host, self.bind_port)
        )
        self._transport = transport
        self.remote = remote
        self._sender = asyncio.create_task(self._send_loop())
        sock = transport.get_extra_info("sockname")
        return sock[0], sock[1]

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            pkt = RtpPacket.decode(data)
        except ValueError:
            return
        if self.remote is None:
            self.remote = addr  # symmetric RTP: latch onto the first sender
            self.payload_type = pkt.payload_type
            self._remote_ssrc = pkt.ssrc
        elif self._remote_ssrc is None and addr == self.remote:
            self._remote_ssrc = pkt.ssrc
            self.payload_type = pkt.payload_type
        elif addr != self.remote or pkt.ssrc != self._remote_ssrc:
            self.packets_dropped += 1  # not our peer: never play injected audio into the call
            return
        self.packets_in += 1
        if self._handler:
            self._handler(payload_to_pcm(pkt.payload, self.byte_order))

    async def _send_loop(self) -> None:
        start = time.monotonic()
        n = 0
        while True:
            n += 1
            wait = start + n * 0.02 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            pcm = self.sink.read(PACKET_SAMPLES)
            if self.remote is None or self._transport is None:
                continue
            pkt = RtpPacket(self.payload_type, self._seq, self._ts, self._ssrc, pcm_to_payload(pcm, self.byte_order))
            self._transport.sendto(pkt.encode(), self.remote)
            self.packets_out += 1
            self._seq = (self._seq + 1) & 0xFFFF
            self._ts = (self._ts + PACKET_SAMPLES) & 0xFFFFFFFF

    async def close(self, reason: str = "closed") -> None:
        if self._sender:
            self._sender.cancel()
            with contextlib.suppress(BaseException):
                await self._sender
        if self._transport:
            self._transport.close()
            self._transport = None
