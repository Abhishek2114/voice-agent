"""Application settings (environment variables prefixed ``VA_``; see .env.example)."""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VA_", env_file=str(REPO_ROOT / ".env"), extra="ignore")

    env: str = "development"  # development | production | test
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = REPO_ROOT / "data"
    database_url: str | None = None

    # security
    secret_key: str | None = None
    session_ttl_hours: int = 72
    session_idle_hours: int = 12
    cookie_secure: bool = False  # set true when served over HTTPS
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"])
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    max_upload_mb: int = 10
    # Provider URLs must resolve to loopback/private addresses unless explicitly allowed.
    allow_remote_provider_hosts: bool = False
    # Public "talk to us" links: off unless enabled here AND per agent.
    public_calls_enabled: bool = False
    # WebRTC STUN server (e.g. stun:stun.cloudflare.com:3478). Empty keeps media LAN-only and
    # contacts no third party; set it so browsers outside this network can connect.
    stun_url: str = ""
    # Piper voice used for Hindi (Devanagari) sentences, e.g. "hi_IN-priyamvada-medium". Empty disables switching.
    hindi_tts_voice: str = ""

    # AI providers (local only)
    ollama_url: str = "http://127.0.0.1:11434"
    default_llm_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    allow_remote_llm_models: bool = False
    whisper_server_url: str = "http://127.0.0.1:8178"
    whisper_model: str = "ggml-base.en-q5_1.bin"
    piper_url: str = "http://127.0.0.1:5005"
    default_tts_voice: str = "en_GB-alba-medium"
    vad_kind: str = "silero"  # silero | energy

    # capacity
    ai_concurrency: int = 1
    max_voice_sessions: int = 2
    telephony_concurrency: int = 4

    # telephony
    real_telephony_enabled: bool = False

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        return Path(str(v)).expanduser() if v else v

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'database' / 'voice_agent.db'}"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def vad_model_path(self) -> Path:
        return self.models_dir / "vad" / "silero_vad.onnx"

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.data_dir / "database", self.recordings_dir, self.uploads_dir,
                  self.transcripts_dir, self.backups_dir, self.models_dir):  # fmt: skip
            d.mkdir(parents=True, exist_ok=True)
            if d != self.models_dir:
                os.chmod(d, 0o700)

    def resolved_secret(self) -> str:
        """Secret from env, or a generated per-install key stored with 0600 permissions."""
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / ".secret_key"
        if path.exists():
            return path.read_text().strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
