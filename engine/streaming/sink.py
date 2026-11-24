"""Outbound audio queue shared by all transports.

The pipeline *enqueues* synthesized audio; the transport *pulls* fixed-size frames in
real time (WebRTC track, RTP sender, test clock). Pulling yields silence when idle,
so the outbound stream is continuous. Markers let the pipeline learn which sentences
actually started playing (for accurate transcripts after an interruption).
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass

import numpy as np

from engine.audio.pcm import resample


@dataclass
class _Item:
    pcm: np.ndarray
    marker: int | None
    offset: int = 0


class QueueAudioSink:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._items: collections.deque[_Item] = collections.deque()
        self._idle = asyncio.Event()
        self._idle.set()
        self.started_markers: set[int] = set()
        self.samples_played = 0

    # ---------------------------------------------------------------- producer side
    def enqueue(self, pcm: np.ndarray, sample_rate: int, marker: int | None = None) -> None:
        if pcm.size == 0:
            return
        data = resample(pcm, sample_rate, self.sample_rate)
        self._items.append(_Item(data, marker))
        self._idle.clear()

    def clear(self) -> int:
        dropped = sum(i.pcm.size - i.offset for i in self._items)
        self._items.clear()
        self._idle.set()
        return dropped

    @property
    def playing(self) -> bool:
        return bool(self._items)

    def queued_seconds(self) -> float:
        return sum(i.pcm.size - i.offset for i in self._items) / self.sample_rate

    async def wait_idle(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # ---------------------------------------------------------------- consumer side
    def read(self, n: int) -> np.ndarray:
        """Return exactly ``n`` samples (audio or silence)."""
        out = np.zeros(n, dtype=np.int16)
        filled = 0
        while filled < n and self._items:
            item = self._items[0]
            if item.offset == 0 and item.marker is not None:
                self.started_markers.add(item.marker)
            take = min(n - filled, item.pcm.size - item.offset)
            out[filled : filled + take] = item.pcm[item.offset : item.offset + take]
            item.offset += take
            filled += take
            if item.offset >= item.pcm.size:
                self._items.popleft()
        self.samples_played += filled
        if not self._items:
            self._idle.set()
        return out
