"""Provider hub: constructs provider adapters from settings + stored configuration.

Adding a provider = implement the engine Protocol and register a builder here; no
other part of the application changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from engine.audio.vad import VAD, create_vad
from engine.providers.base import ProviderHealth
from engine.providers.llm.base import LLMOptions, LLMProvider
from engine.providers.llm.ollama import OllamaProvider
from engine.providers.stt.base import STTProvider
from engine.providers.stt.whisper_cpp import WhisperCliSTT, WhisperServerSTT
from engine.providers.telephony.asterisk_ari import AriConfig, AsteriskAriProvider
from engine.providers.telephony.base import TelephonyProvider
from engine.providers.telephony.simulator import SimulatedTelephonyProvider, SimulationScenario
from engine.providers.tts.base import TTSProvider
from engine.providers.tts.macos_say import MacSayTTS
from engine.providers.tts.piper_http import PiperHttpTTS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import SecretBox
from app.database.models import ProviderConfig

log = logging.getLogger(__name__)

STT_PROVIDERS = ("whisper_cpp_server", "whisper_cpp_cli")
TTS_PROVIDERS = ("piper", "macos_say")
TELEPHONY_PROVIDERS = ("simulator", "asterisk_ari")

DEFAULT_AUDIO = {
    "vad": "silero", "vad_threshold": 0.5, "end_silence_ms": 700, "min_speech_ms": 160,
    "barge_in": True, "barge_in_min_ms": 300, "pre_roll_ms": 300,
}  # fmt: skip


class ProviderHub:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.ai_slot = asyncio.Semaphore(max(1, self.settings.ai_concurrency))
        self.configs: dict[str, ProviderConfig] = {}
        self._secretbox = SecretBox(self.settings.resolved_secret())
        self._llm: LLMProvider | None = None
        self._stt: STTProvider | None = None
        self._tts: dict[str, TTSProvider] = {}
        self.overrides: dict[str, Any] = {}  # tests inject fakes here

    # ------------------------------------------------------------ config
    async def load(self, db: AsyncSession) -> None:
        rows = (await db.execute(select(ProviderConfig).where(ProviderConfig.scope == "global"))).scalars().all()
        self.configs = {r.kind: r for r in rows}
        self._llm = self._stt = None
        self._tts = {}

    def cfg(self, kind: str) -> dict[str, Any]:
        row = self.configs.get(kind)
        return dict(row.config) if row else {}

    def provider_name(self, kind: str, default: str) -> str:
        row = self.configs.get(kind)
        return row.provider if row else default

    def secrets(self, kind: str) -> dict[str, Any]:
        row = self.configs.get(kind)
        if not row or not row.secrets_encrypted:
            return {}
        raw = self._secretbox.decrypt(row.secrets_encrypted)
        return json.loads(raw) if raw else {}

    def encrypt_secrets(self, data: dict[str, Any]) -> str:
        return self._secretbox.encrypt(json.dumps(data))

    # ------------------------------------------------------------ builders
    def llm(self) -> LLMProvider:
        if "llm" in self.overrides:
            return self.overrides["llm"]
        if self._llm is None:
            c = self.cfg("llm")
            self._llm = OllamaProvider(c.get("base_url") or self.settings.ollama_url,
                                       allow_remote_models=self.settings.allow_remote_llm_models)  # fmt: skip
        return self._llm

    def default_model(self) -> str:
        return str(self.cfg("llm").get("default_model") or self.settings.default_llm_model)

    def llm_options(self, agent_llm: dict[str, Any] | None = None) -> LLMOptions:
        base = self.cfg("llm")
        a = agent_llm or {}
        return LLMOptions(
            model=str(a.get("model") or self.default_model()),
            temperature=float(a.get("temperature", base.get("temperature", 0.6))),
            max_tokens=int(a.get("max_tokens", base.get("max_tokens", 220))),
            num_ctx=int(base.get("num_ctx", 4096)),
            keep_alive=str(base.get("keep_alive", "15m")),
        )

    def stt(self) -> STTProvider:
        if "stt" in self.overrides:
            return self.overrides["stt"]
        if self._stt is None:
            c = self.cfg("stt")
            name = self.provider_name("stt", "whisper_cpp_server")
            if name == "whisper_cpp_cli":
                model = c.get("model_file") or self.settings.whisper_model
                self._stt = WhisperCliSTT(self.settings.models_dir / "whisper" / str(model))
            else:
                self._stt = WhisperServerSTT(c.get("base_url") or self.settings.whisper_server_url)
        return self._stt

    def stt_language(self) -> str:
        return str(self.cfg("stt").get("language") or "en")

    def tts(self, name: str | None = None) -> TTSProvider:
        if "tts" in self.overrides:
            return self.overrides["tts"]
        name = name or self.provider_name("tts", "piper")
        if name not in TTS_PROVIDERS:
            name = "piper"
        if name not in self._tts:
            if name == "macos_say":
                self._tts[name] = MacSayTTS()
            else:
                self._tts[name] = PiperHttpTTS(self.cfg("tts").get("base_url") or self.settings.piper_url)
        return self._tts[name]

    def default_voice(self) -> str:
        return str(self.cfg("tts").get("default_voice") or self.settings.default_tts_voice)

    def audio(self) -> dict[str, Any]:
        return {**DEFAULT_AUDIO, **self.cfg("audio")}

    def vad(self) -> VAD:
        if "vad" in self.overrides:
            return self.overrides["vad"]()
        kind = str(self.audio().get("vad") or self.settings.vad_kind)
        return create_vad(kind, self.settings.vad_model_path)

    def telephony(self, real: bool) -> TelephonyProvider:
        if "telephony" in self.overrides:
            return self.overrides["telephony"]
        if not real:
            sim = self.cfg("telephony").get("simulation") or {}
            return SimulatedTelephonyProvider(SimulationScenario(**{k: v for k, v in sim.items()
                                                                    if k in SimulationScenario.__dataclass_fields__}))  # fmt: skip
        row = self.configs.get("telephony")
        if row is None or row.provider != "asterisk_ari" or not row.enabled:
            raise RuntimeError("No real telephony provider is configured and enabled")
        c = self.cfg("telephony")
        fields = {k: v for k, v in c.items() if k in AriConfig.__dataclass_fields__ and k != "password"}
        return AsteriskAriProvider(AriConfig(**fields, password=str(self.secrets("telephony").get("password", ""))))

    # ------------------------------------------------------------ health
    async def health(self) -> dict[str, dict[str, Any]]:
        async def safe(coro: Any, name: str) -> ProviderHealth:
            try:
                return await asyncio.wait_for(coro, timeout=6)
            except Exception as exc:
                return ProviderHealth(name, "unavailable", f"health check failed: {exc.__class__.__name__}")

        tts_name = self.provider_name("tts", "piper")
        llm_h, stt_h, tts_h = await asyncio.gather(
            safe(self.llm().health(), "ollama"), safe(self.stt().health(), "stt"), safe(self.tts(tts_name).health(), tts_name)
        )
        try:
            self.vad()
            vad_h = ProviderHealth(str(self.audio().get("vad")), "ok", "VAD model loaded")
        except Exception as exc:
            vad_h = ProviderHealth(str(self.audio().get("vad")), "unavailable", str(exc))
        tel_row = self.configs.get("telephony")
        if tel_row and tel_row.provider == "asterisk_ari" and tel_row.enabled and self.settings.real_telephony_enabled:
            try:
                tel_h = await safe(self.telephony(True).health(), "asterisk_ari")
            except RuntimeError as exc:
                tel_h = ProviderHealth("asterisk_ari", "unavailable", str(exc), is_local=False)
        else:
            tel_h = ProviderHealth("simulator", "ok", "Simulation only — real telephony is disabled")
        return {"llm": llm_h.to_dict(), "stt": stt_h.to_dict(), "tts": tts_h.to_dict(), "vad": vad_h.to_dict(),
                "telephony": tel_h.to_dict()}  # fmt: skip


_hub: ProviderHub | None = None


def get_hub() -> ProviderHub:
    global _hub
    if _hub is None:
        _hub = ProviderHub()
    return _hub


def set_hub(hub: ProviderHub | None) -> None:
    global _hub
    _hub = hub
