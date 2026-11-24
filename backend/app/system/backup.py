"""Backup & restore.

Archive layout (tar.gz): ``manifest.json``, ``database/voice_agent.db`` (SQLite online
backup — consistent while the app runs), ``recordings/**``, ``uploads/**``.
Optional encryption: AES-256-GCM, key = scrypt(passphrase, random salt).
Restores are *staged* (verified + extracted to ``data/restore-pending``) and applied by
``python -m app.system.backup apply-pending`` while services are stopped
(``scripts/start.sh`` does this automatically). The previous data is kept in
``data/backups/pre-restore-<timestamp>/``.

CLI:
    python -m app.system.backup create [--passphrase-env VAR]
    python -m app.system.backup verify FILE [--passphrase-env VAR]
    python -m app.system.backup stage FILE [--passphrase-env VAR]
    python -m app.system.backup apply-pending
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.core.config import Settings, get_settings

MAGIC = b"VABK1"
FORMAT = "voice-agent.backup"
VERSION = 1
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_UNCOMPRESSED_BYTES = 32 * 1024**3


class BackupError(Exception):
    pass


def _db_path(settings: Settings) -> Path:
    url = settings.db_url
    if not url.startswith("sqlite"):
        raise BackupError("Only SQLite databases are supported")
    return Path(url.split("///", 1)[1])


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def create_backup(settings: Settings | None = None, passphrase: str | None = None) -> Path:
    settings = settings or get_settings()
    db = _db_path(settings)
    if not db.exists():
        raise BackupError("Database not found")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "voice_agent.db"
        src = sqlite3.connect(db)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)  # online, consistent snapshot
            revision = dst.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            dst.close()
            src.close()
        files: list[tuple[Path, str]] = [(snap, "database/voice_agent.db")]
        for folder in ("recordings", "uploads"):
            base = settings.data_dir / folder
            if base.exists():
                for p in sorted(base.rglob("*")):
                    if p.is_file() and not p.is_symlink():
                        files.append((p, f"{folder}/{p.relative_to(base).as_posix()}"))
        manifest = {"format": FORMAT, "version": VERSION, "created_at": datetime.now(UTC).isoformat(),
                    "schema_revision": revision[0] if revision else None, "encrypted": bool(passphrase),
                    "files": [{"path": arc, "size": p.stat().st_size, "sha256": _sha256(p)} for p, arc in files]}  # fmt: skip
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            m = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(m)
            tar.addfile(info, io.BytesIO(m))
            for p, arc in files:
                tar.add(p, arcname=arc, recursive=False)
        payload = buf.getvalue()
    name = f"voice-agent-backup-{stamp}.tar.gz"
    if passphrase:
        salt, nonce = os.urandom(16), os.urandom(12)
        payload = MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, payload, MAGIC)
        name += ".enc"
    out = settings.backups_dir / name
    _write_private(out, payload)
    return out


def _decrypt(data: bytes, passphrase: str | None) -> bytes:
    if not data.startswith(MAGIC):
        return data
    if not passphrase:
        raise BackupError("This backup is encrypted; a passphrase is required")
    salt, nonce, ct = data[5:21], data[21:33], data[33:]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, MAGIC)
    except Exception as exc:
        raise BackupError("Wrong passphrase or corrupted backup") from exc


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    total = 0
    for m in tar.getmembers():
        total += max(m.size, 0)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise BackupError("Backup expands beyond the allowed size")
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise BackupError(f"Unsafe entry in archive: {m.name}")
        if m.isfile() and not (m.name == "manifest.json" or m.name.startswith(("database/", "recordings/", "uploads/"))):
            raise BackupError(f"Unexpected entry in archive: {m.name}")
        members.append(m)
    return members


def verify_backup(data: bytes, passphrase: str | None = None, extract_to: Path | None = None) -> dict[str, Any]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise BackupError("Backup too large")
    raw = _decrypt(data, passphrase)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")  # noqa: SIM115 - closed by the with-block below
    except tarfile.TarError as exc:
        raise BackupError("Not a valid backup archive") from exc
    with tar, tempfile.TemporaryDirectory() as tmp:
        members = _safe_members(tar)
        mf = tar.extractfile("manifest.json") if "manifest.json" in tar.getnames() else None
        if mf is None:
            raise BackupError("Backup manifest missing")
        manifest = json.loads(mf.read())
        if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
            raise BackupError("Unsupported backup format")
        target = Path(extract_to or tmp)
        tar.extractall(target, members=members, filter="data")
        for f in manifest.get("files", []):
            p = (target / str(f["path"])).resolve()
            if not p.is_relative_to(target.resolve()):
                raise BackupError("Manifest path escapes the archive")
            if not p.is_file() or _sha256(p) != f["sha256"]:
                raise BackupError(f"Checksum mismatch for {f['path']}")
        db = target / "database" / "voice_agent.db"
        conn = sqlite3.connect(db)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if ok != "ok":
            raise BackupError(f"Database integrity check failed: {ok}")
        return {"created_at": manifest.get("created_at"), "schema_revision": manifest.get("schema_revision"),
                "files": len(manifest.get("files", [])), "encrypted": data.startswith(MAGIC)}  # fmt: skip


def stage_restore(data: bytes, passphrase: str | None = None, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    staging = settings.data_dir / "restore-pending"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    try:
        info = verify_backup(data, passphrase, extract_to=staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    (staging / "READY").write_text(json.dumps(info))
    return info


def apply_pending_restore(settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    staging = settings.data_dir / "restore-pending"
    if not (staging / "READY").exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    keep = settings.backups_dir / f"pre-restore-{stamp}"
    keep.mkdir(parents=True, mode=0o700)
    db = _db_path(settings)
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db}{suffix}")
        if p.exists():
            shutil.move(str(p), keep / p.name)
    for folder in ("recordings", "uploads"):
        cur = settings.data_dir / folder
        if cur.exists():
            shutil.move(str(cur), keep / folder)
    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging / "database" / "voice_agent.db"), db)
    os.chmod(db, 0o600)
    for folder in ("recordings", "uploads"):
        src = staging / folder
        dst = settings.data_dir / folder
        if src.exists():
            shutil.move(str(src), dst)
        else:
            dst.mkdir(mode=0o700, exist_ok=True)
    info = json.loads((staging / "READY").read_text())
    shutil.rmtree(staging, ignore_errors=True)
    return {**info, "previous_data": str(keep)}


def list_backups(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    out = []
    for p in sorted(settings.backups_dir.glob("voice-agent-backup-*"), reverse=True):
        if p.is_file():
            out.append({"name": p.name, "size_bytes": p.stat().st_size, "encrypted": p.name.endswith(".enc"),
                        "created_at": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat()})  # fmt: skip
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.system.backup")
    ap.add_argument("command", choices=["create", "verify", "stage", "apply-pending", "list"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--passphrase-env", help="environment variable holding the passphrase")
    args = ap.parse_args(argv)
    passphrase = os.environ.get(args.passphrase_env) if args.passphrase_env else None
    settings = get_settings()
    settings.ensure_dirs()
    try:
        if args.command == "create":
            print(create_backup(settings, passphrase))
        elif args.command == "list":
            print(json.dumps(list_backups(settings), indent=2))
        elif args.command in ("verify", "stage"):
            if not args.file:
                ap.error("FILE is required")
            data = Path(args.file).read_bytes()
            info = verify_backup(data, passphrase) if args.command == "verify" else stage_restore(data, passphrase, settings)
            print(json.dumps(info, indent=2))
        else:
            res = apply_pending_restore(settings)
            print(json.dumps(res, indent=2) if res else "no pending restore")
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
