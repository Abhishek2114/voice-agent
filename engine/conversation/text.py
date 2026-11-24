"""Response processing: make LLM output safe and natural to speak, and split it into
sentences so TTS can start before the full response has been generated."""

from __future__ import annotations

import re

_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)
_MD = re.compile(r"(\*\*|__|`{1,3}|^#{1,6}\s*|^>\s*)", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002700-\U000027bf\U0001f000-\U0001f2ff\U00002600-\U000026ff️]",
    flags=re.UNICODE,
)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_WS = re.compile(r"\s+")


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text)


def speechify(text: str) -> str:
    """Remove formatting that should not be read aloud."""
    text = strip_thinking(text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("our website", text)
    text = _BULLET.sub("", text)
    text = _MD.sub("", text)
    text = text.replace("*", "").replace("#", "")
    text = _EMOJI.sub("", text)
    return _WS.sub(" ", text).strip()


_SENTENCE_END = re.compile(r"([.!?]+)(?=\s|$)|([।])")
_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "e.g.", "i.e.", "etc.", "approx.", "no."}


class SentenceSplitter:
    """Incremental sentence splitter for streamed text.

    ``push`` returns complete sentences as soon as they are available; ``flush``
    returns whatever remains at the end of the stream.
    """

    def __init__(self, min_chars: int = 8, max_chars: int = 220) -> None:
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buf = ""
        self._in_think = False

    def push(self, delta: str) -> list[str]:
        self._buf += delta
        # drop reasoning blocks that some models emit despite think=false
        if "<think>" in self._buf.lower():
            self._buf = strip_thinking(self._buf)
        out: list[str] = []
        while True:
            sentence = self._take_sentence()
            if sentence is None:
                break
            cleaned = speechify(sentence)
            if cleaned:
                out.append(cleaned)
        return out

    def flush(self) -> list[str]:
        rest = speechify(self._buf)
        self._buf = ""
        return [rest] if rest else []

    def _take_sentence(self) -> str | None:
        buf = self._buf
        for m in _SENTENCE_END.finditer(buf):
            end = m.end()
            candidate = buf[:end]
            last_word = candidate.strip().split(" ")[-1].lower()
            if last_word in _ABBREV:
                continue
            if len(candidate.strip()) < self.min_chars:
                continue
            # only cut if something follows (otherwise we cannot be sure the sentence ended)
            if end >= len(buf):
                return None
            self._buf = buf[end:].lstrip()
            return candidate
        if len(buf) > self.max_chars:
            cut = max(buf.rfind(", ", 0, self.max_chars), buf.rfind("; ", 0, self.max_chars))
            if cut > self.min_chars:
                self._buf = buf[cut + 2 :]
                return buf[: cut + 1]
        return None
