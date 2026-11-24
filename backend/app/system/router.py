"""System health, diagnostics, provider settings, model management and backups."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import time
from datetime import timedelta
from typing import Any, Literal

import psutil
from engine.audio.pcm import wav_bytes
from engine.providers.base import ProviderError
from engine.providers.llm.base import ChatMessage, LLMOptions
from engine.providers.tts.base import VoiceSettings
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.hub import STT_PROVIDERS, TELEPHONY_PROVIDERS, TTS_PROVIDERS, get_hub
from app.audio.sessions import manager
from app.auth.deps import current_user, instance_admin
from app.core.audit import audit
from app.core.config import get_settings
from app.core.errors import APIError, bad_request, not_found
from app.core.ssrf import UnsafeURLError, validate_provider_url
from app.database.base import utcnow
from app.database.models import ProviderConfig, SystemEvent, SystemState, User
from app.database.session import get_db
from app.system import backup as backups

router = APIRouter(tags=["system"])
_started = time.monotonic()


@router.get("/api/health")
async def liveness(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Unauthenticated liveness probe used by scripts; reveals no data."""
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok}


async def _scheduler_status(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(SystemState, "scheduler")
    if row is None:
        return {"status": "unavailable", "detail": "Scheduler has never run"}
    v = row.value or {}
    age = (utcnow() - row.updated_at).total_seconds()
    if v.get("state") != "running":
        return {"status": "unavailable", "detail": "Scheduler is stopped", **v}
    return {"status": "ok" if age < 20 else "unavailable", "detail": f"heartbeat {age:.0f}s ago", **v}


@router.get("/api/system/health")
async def health(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        db_status = {"status": "ok", "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as exc:
        db_status = {"status": "unavailable", "detail": str(exc)}
    providers = await get_hub().health()
    settings = get_settings()
    disk = shutil.disk_usage(settings.data_dir)
    mem = psutil.virtual_memory()
    return {
        "database": db_status,
        "scheduler": await _scheduler_status(db),
        **providers,
        "voice_sessions": {"active": manager.active, "max": settings.max_voice_sessions},
        "resources": {"memory_total_gb": round(mem.total / 1024**3, 1), "memory_used_percent": mem.percent,
                      "disk_free_gb": round(disk.free / 1024**3, 1), "cpu_percent": psutil.cpu_percent(interval=None)},
        "uptime_seconds": round(time.monotonic() - _started),
        "real_telephony_enabled": settings.real_telephony_enabled,
    }  # fmt: skip


@router.get("/api/system/diagnostics")
async def diagnostics(_: User = Depends(instance_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    models_dir = settings.models_dir
    model_files = {p.relative_to(models_dir).as_posix(): p.stat().st_size for p in models_dir.rglob("*")
                   if p.is_file() and p.suffix in (".bin", ".onnx", ".gguf")} if models_dir.exists() else {}  # fmt: skip
    events = (await db.execute(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(30))).scalars()
    proc = psutil.Process(os.getpid())
    return {
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                     "python": sys.version.split()[0]},
        "process": {"pid": proc.pid, "rss_mb": round(proc.memory_info().rss / 1024**2, 1)},
        "settings": {"host": settings.host, "port": settings.port, "ollama_url": settings.ollama_url,
                     "default_llm_model": settings.default_llm_model, "whisper_server_url": settings.whisper_server_url,
                     "piper_url": settings.piper_url, "vad": settings.vad_kind, "ai_concurrency": settings.ai_concurrency,
                     "max_voice_sessions": settings.max_voice_sessions,
                     "telephony_concurrency": settings.telephony_concurrency,
                     "real_telephony_enabled": settings.real_telephony_enabled,
                     "public_calls_enabled": settings.public_calls_enabled, "cookie_secure": settings.cookie_secure},
        "model_files": model_files,
        "events": [{"level": e.level, "source": e.source, "message": e.message, "created_at": e.created_at}
                   for e in events],
    }  # fmt: skip


# ------------------------------------------------------------------ providers
class ProviderIn(BaseModel):
    provider: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] | None = None
    enabled: bool = True


URL_KEYS = ("base_url",)
ALLOWED = {"llm": ("ollama",), "stt": STT_PROVIDERS, "tts": TTS_PROVIDERS, "telephony": TELEPHONY_PROVIDERS,
           "audio": ("default",)}  # fmt: skip


@router.get("/api/system/providers")
async def get_providers(_: User = Depends(current_user)) -> dict[str, Any]:
    hub = get_hub()
    out: dict[str, Any] = {}
    defaults = {"llm": "ollama", "stt": "whisper_cpp_server", "tts": "piper", "telephony": "simulator", "audio": "default"}
    for kind in ALLOWED:
        row = hub.configs.get(kind)
        out[kind] = {"provider": row.provider if row else defaults[kind], "config": dict(row.config) if row else {},
                     "enabled": row.enabled if row else kind != "telephony",
                     "has_secrets": bool(row and row.secrets_encrypted), "options": list(ALLOWED[kind])}  # fmt: skip
    s = get_settings()
    out["defaults"] = {"ollama_url": s.ollama_url, "default_llm_model": hub.default_model(),
                       "whisper_server_url": s.whisper_server_url, "piper_url": s.piper_url,
                       "default_tts_voice": hub.default_voice(), "audio": hub.audio()}  # fmt: skip
    return out


@router.put("/api/system/providers/{kind}")
async def put_provider(kind: Literal["llm", "stt", "tts", "telephony", "audio"], body: ProviderIn, request: Request,
                       admin: User = Depends(instance_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    settings = get_settings()
    if body.provider not in ALLOWED[kind]:
        raise bad_request(f"provider must be one of {', '.join(ALLOWED[kind])}")
    cfg = dict(body.config)
    if len(str(cfg)) > 10_000:
        raise bad_request("configuration too large")
    for key in URL_KEYS:
        if cfg.get(key):
            try:
                cfg[key] = validate_provider_url(str(cfg[key]), allow_remote=settings.allow_remote_provider_hosts)
            except UnsafeURLError as exc:
                raise APIError(400, "unsafe_url", str(exc)) from exc
    cfg.pop("password", None)  # never stored in plain config
    if kind == "telephony" and body.provider == "asterisk_ari" and body.enabled and not settings.real_telephony_enabled:
        raise APIError(400, "pstn_disabled", "Set VA_REAL_TELEPHONY_ENABLED=true before enabling a real provider")
    row = (await db.execute(select(ProviderConfig).where(ProviderConfig.scope == "global",
                                                         ProviderConfig.kind == kind))).scalar_one_or_none()  # fmt: skip
    if row is None:
        row = ProviderConfig(scope="global", kind=kind, provider=body.provider, config=cfg, enabled=body.enabled)
        db.add(row)
    else:
        row.provider, row.config, row.enabled = body.provider, cfg, body.enabled
    hub = get_hub()
    if body.secrets is not None:
        row.secrets_encrypted = hub.encrypt_secrets(body.secrets) if body.secrets else None
    audit(db, "settings.provider_updated", user_id=admin.id, details={"kind": kind, "provider": body.provider,
          "config_keys": sorted(cfg)}, request=request)  # fmt: skip
    await db.commit()
    await hub.load(db)
    return {"ok": True}


# ------------------------------------------------------------------ models
class ModelTestIn(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    prompt: str = Field(default="Say hello in one short sentence.", max_length=500)


@router.get("/api/system/models")
async def models(_: User = Depends(current_user)) -> dict[str, Any]:
    hub = get_hub()
    try:
        items = await hub.llm().list_models(force=True)
    except ProviderError as exc:
        return {"available": False, "error": str(exc), "models": [], "default": hub.default_model()}
    mem = psutil.virtual_memory().total
    return {"available": True, "default": hub.default_model(), "allow_remote": get_settings().allow_remote_llm_models,
            "models": [{"name": m.name, "size_bytes": m.size_bytes, "family": m.family, "parameter_size": m.parameter_size,
                        "quantization": m.quantization, "is_local": m.is_local, "remote_host": m.remote_host,
                        "capabilities": m.capabilities, "fits_memory": (m.size_bytes or 0) < mem * 0.4}
                       for m in items]}  # fmt: skip


@router.post("/api/system/models/test")
async def test_model(body: ModelTestIn, _: User = Depends(current_user)) -> dict[str, Any]:
    hub = get_hub()
    opts = LLMOptions(model=body.model, max_tokens=60, temperature=0.3)
    text_out, ttft = "", None
    t0 = time.perf_counter()
    try:
        async with hub.ai_slot:
            async for chunk in hub.llm().stream_chat([ChatMessage("user", body.prompt)], opts):
                if chunk.text and ttft is None:
                    ttft = round((time.perf_counter() - t0) * 1000, 1)
                text_out += chunk.text
    except ProviderError as exc:
        raise APIError(503, exc.code, str(exc)) from exc
    return {"ok": True, "reply": text_out.strip(), "ttft_ms": ttft, "total_ms": round((time.perf_counter() - t0) * 1000, 1)}


@router.post("/api/system/models/warmup")
async def warmup(body: ModelTestIn, _: User = Depends(current_user)) -> dict[str, Any]:
    llm = get_hub().llm()
    if not hasattr(llm, "warm_up"):
        raise bad_request("provider does not support warm-up")
    try:
        ms = await llm.warm_up(body.model)
    except ProviderError as exc:
        raise APIError(503, exc.code, str(exc)) from exc
    return {"ok": True, "load_ms": ms}


# ------------------------------------------------------------------ voices
class PreviewIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    provider: str = "piper"
    voice: str | None = Field(default=None, max_length=120)
    speaker: str | None = Field(default=None, max_length=60)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@router.get("/api/system/voices")
async def voices(provider: str = "piper", _: User = Depends(current_user)) -> dict[str, Any]:
    if provider not in TTS_PROVIDERS:
        raise bad_request("unknown TTS provider")
    try:
        items = await get_hub().tts(provider).list_voices()
    except ProviderError as exc:
        return {"available": False, "error": str(exc), "voices": []}
    return {"available": True, "voices": [v.__dict__ for v in items]}


@router.post("/api/system/voices/preview")
async def preview_voice(body: PreviewIn, _: User = Depends(current_user)) -> Response:
    if body.provider not in TTS_PROVIDERS:
        raise bad_request("unknown TTS provider")
    hub = get_hub()
    voice = VoiceSettings(voice=body.voice or (hub.default_voice() if body.provider == "piper" else ""),
                          speaker=body.speaker or None, speed=body.speed)  # fmt: skip
    try:
        res = await hub.tts(body.provider).synthesize(body.text, voice)
    except ProviderError as exc:
        raise APIError(503, exc.code, str(exc)) from exc
    return Response(wav_bytes(res.pcm, res.sample_rate), media_type="audio/wav",
                    headers={"X-Synthesis-Ms": str(res.latency_ms), "Cache-Control": "no-store"})  # fmt: skip


# ------------------------------------------------------------------ backups
@router.get("/api/system/backups")
async def get_backups(_: User = Depends(instance_admin)) -> dict[str, Any]:
    settings = get_settings()
    pending = settings.data_dir / "restore-pending" / "READY"
    return {"backups": backups.list_backups(), "restore_pending": pending.exists()}


@router.post("/api/system/backups", status_code=201)
async def make_backup(request: Request, passphrase: str | None = Form(None, max_length=256),
                      admin: User = Depends(instance_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    if passphrase is not None and 0 < len(passphrase) < 10:
        raise bad_request("Backup passphrase must be at least 10 characters")
    path = await asyncio.to_thread(backups.create_backup, None, passphrase or None)
    audit(db, "backup.created", user_id=admin.id, details={"name": path.name, "encrypted": bool(passphrase)},
          request=request)  # fmt: skip
    await db.commit()
    return {"name": path.name, "size_bytes": path.stat().st_size}


@router.get("/api/system/backups/{name}")
async def download_backup(name: str, request: Request, admin: User = Depends(instance_admin),
                          db: AsyncSession = Depends(get_db)) -> Response:  # fmt: skip
    settings = get_settings()
    path = (settings.backups_dir / name).resolve()
    if not name.startswith("voice-agent-backup-") or path.parent != settings.backups_dir.resolve() or not path.is_file():
        raise not_found("Backup")
    audit(db, "backup.downloaded", user_id=admin.id, details={"name": name}, request=request)
    await db.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.post("/api/system/backups/restore")
async def restore_backup(request: Request, file: UploadFile = File(...), passphrase: str | None = Form(None),
                         admin: User = Depends(instance_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:  # fmt: skip
    data = await file.read(backups.MAX_ARCHIVE_BYTES + 1)
    try:
        info = await asyncio.to_thread(backups.stage_restore, data, passphrase or None)
    except backups.BackupError as exc:
        raise bad_request(str(exc), "invalid_backup") from exc
    audit(db, "backup.restore_staged", user_id=admin.id, details=info, request=request)
    await db.commit()
    return {**info, "message": "Backup verified and staged. Restart the application (./scripts/stop.sh && "
                               "./scripts/start.sh) to apply it. Current data will be preserved in data/backups."}


@router.get("/api/system/events")
async def events(_: User = Depends(instance_admin), db: AsyncSession = Depends(get_db),
                 hours: int = 72) -> list[dict[str, Any]]:  # fmt: skip
    since = utcnow() - timedelta(hours=max(1, min(hours, 720)))
    rows = (await db.execute(select(SystemEvent).where(SystemEvent.created_at >= since)
                             .order_by(SystemEvent.created_at.desc()).limit(200))).scalars()  # fmt: skip
    return [{"level": e.level, "source": e.source, "message": e.message, "details": e.details,
             "created_at": e.created_at} for e in rows]  # fmt: skip
