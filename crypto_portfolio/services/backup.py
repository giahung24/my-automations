"""Backups of the SQLite history: a consistent local copy, and an off-site one through rclone.

The history cannot be fetched again: a missed snapshot is gone, and the diary notes exist nowhere
else. A backup is

1. a copy taken with SQLite's online backup API, so it is consistent even while the collector is
   writing (copying the file would miss what still sits in the ``-wal`` file);
2. checked (``PRAGMA integrity_check``), gzipped into ``backup.dir`` as
   ``portfolio-YYYYmmdd-HHMMSS.db.gz`` with mode 0600, and the newest ``backup.keep`` are kept;
3. when ``backup.remote`` is set, sent with ``rclone copy`` to that destination. rclone speaks
   Google Drive, Mega, iCloud Drive and some seventy others, and owns the credentials: this code
   never sees them. A failed upload is recorded and reported; the local copy stays.

The collector runs it after a snapshot once every ``backup.every_hours``. Restore: stop the
services, ``gunzip`` a copy over the database file, start them again.

The copy holds balances and wallet addresses in clear. A remote that is not end-to-end encrypted
should be wrapped in an rclone ``crypt`` remote.
"""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from services.db import Database, utc_now_iso

PATTERN = "portfolio-*.db.gz"
STATUS_KEY = "backup.status"
RCLONE_TIMEOUT_S = 600
OVERDUE_FACTOR = 2   # no backup for this many periods: say so on the dashboard


def backup_files(directory: Path) -> list[Path]:
    """Local backups, newest first (the timestamp in the name sorts as text)."""
    return sorted(directory.glob(PATTERN), reverse=True) if directory.is_dir() else []


def create_backup(db_path: Path, directory: Path, keep: int, now: datetime | None = None) -> Path:
    """Write one checked, gzipped copy of the database and drop the copies beyond ``keep``."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    target = directory / f"portfolio-{stamp}.db.gz"
    plain = directory / f".portfolio-{stamp}.db.tmp"
    try:
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        copy = sqlite3.connect(plain)
        try:
            source.backup(copy)
            verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
            source.close()
        if verdict != "ok":
            raise RuntimeError(f"the copy failed its integrity check: {verdict}")
        partial = target.with_name(target.name + ".part")
        with open(plain, "rb") as raw, gzip.open(partial, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed)
        partial.chmod(0o600)
        partial.replace(target)   # a reader never sees a half-written backup
    finally:
        plain.unlink(missing_ok=True)
    for old in backup_files(directory)[max(keep, 1):]:
        old.unlink(missing_ok=True)
    return target


def upload(path: Path, remote: str, keep_days: int = 0, run=subprocess.run) -> None:
    """``rclone copy`` one backup to ``remote``; raises RuntimeError with rclone's own words."""

    def rclone(*arguments: str) -> None:
        try:
            done = run(["rclone", *arguments], capture_output=True, text=True, timeout=RCLONE_TIMEOUT_S)
        except FileNotFoundError as exc:
            raise RuntimeError("rclone is not installed on this machine (https://rclone.org/install/)") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"rclone did not finish within {RCLONE_TIMEOUT_S} s") from exc
        if done.returncode != 0:
            lines = (done.stderr or done.stdout or "").strip().splitlines()
            raise RuntimeError(f"rclone failed: {lines[-1] if lines else f'exit code {done.returncode}'}")

    rclone("copy", str(path), remote)
    if keep_days > 0:
        rclone("delete", "--min-age", f"{keep_days}d", "--include", PATTERN, remote)


def run_backup(config, database: Database, now: datetime | None = None, run=subprocess.run) -> dict:
    """Take a backup now, upload it when a remote is configured, store and return what happened."""
    settings = config.backup_settings
    status = {"at": utc_now_iso(), "file": "", "size": 0, "remote": settings.remote, "uploaded": None, "error": ""}
    try:
        path = create_backup(Path(database.path), config.backup_dir, settings.keep, now)
        status.update(file=path.name, size=path.stat().st_size)
        if settings.remote_error:
            status.update(uploaded=False, error=settings.remote_error)
        elif settings.remote:
            try:
                upload(path, settings.remote, settings.remote_keep_days, run)
                status["uploaded"] = True
            except RuntimeError as exc:   # the local copy exists: say the off-site one does not
                status.update(uploaded=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a backup that cannot be taken is reported, never raised
        status["error"] = f"backup failed: {exc}"
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (STATUS_KEY, json.dumps(status), status["at"]))
    return status


def backup_status(config, database: Database, now: float | None = None) -> dict:
    """What the dashboard shows: the newest local copy, the last run, and whether one is overdue."""
    settings = config.backup_settings
    files = backup_files(config.backup_dir)
    with database.connect() as connection:
        row = connection.execute("SELECT value FROM sync_state WHERE key = ?", (STATUS_KEY,)).fetchone()
    try:
        last = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        last = None
    newest = files[0].stat().st_mtime if files else None
    age_hours = ((now or time.time()) - newest) / 3600 if newest else None
    return {
        "enabled": settings.enabled, "every_hours": settings.every_hours, "keep": settings.keep,
        "remote": settings.remote, "config_error": settings.remote_error,
        "count": len(files), "latest": files[0].name if files else None,
        "latest_at": datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds") if newest else None,
        "latest_size": files[0].stat().st_size if files else None,
        "due": settings.enabled and (age_hours is None or age_hours >= settings.every_hours),
        "overdue": settings.enabled and (age_hours is None or age_hours >= OVERDUE_FACTOR * settings.every_hours),
        "last_run": last,
    }


def run_if_due(config, database: Database) -> dict | None:
    """The collector hook: back up when the newest copy is older than ``every_hours``."""
    return run_backup(config, database) if backup_status(config, database)["due"] else None
