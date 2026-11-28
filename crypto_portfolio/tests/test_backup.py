"""Backups: a consistent local copy, rotation, the rclone hand-off, the API. Offline."""
from __future__ import annotations

import gzip
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_api
from config import BackupSettings, load_config
from services.backup import backup_files, backup_status, create_backup, run_backup, run_if_due, upload
from services.db import Database


def make(tmp_path, **backup):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"settings": {"db_path": str(tmp_path / "history.db")}, "backup": backup}), encoding="utf-8")
    config = load_config(path)
    database = Database(config.db_path)
    database.set_note("2026-11-25", "irreplaceable")
    return config, database


def restored_notes(archive: Path, tmp_path) -> list:
    plain = tmp_path / "restored.db"
    plain.write_bytes(gzip.decompress(archive.read_bytes()))
    with sqlite3.connect(plain) as connection:
        return connection.execute("SELECT day, body FROM notes").fetchall()


def test_backup_is_a_restorable_copy_with_what_is_still_in_the_wal(tmp_path):
    config, database = make(tmp_path)
    writer = sqlite3.connect(database.path)            # a writer is mid-session: its row sits in the -wal file
    writer.execute("INSERT INTO notes VALUES ('2026-11-26', 'second', '2026-11-26T00:00:00+00:00')")
    writer.commit()
    status = run_backup(config, database)
    writer.close()
    archive = config.backup_dir / status["file"]
    assert config.backup_dir == tmp_path / "backups" and status["error"] == "" and status["uploaded"] is None
    assert restored_notes(archive, tmp_path) == [("2026-11-25", "irreplaceable"), ("2026-11-26", "second")]
    assert archive.stat().st_mode & 0o777 == 0o600 and status["size"] == archive.stat().st_size
    assert [p.name for p in config.backup_dir.iterdir()] == [archive.name]   # no temp file left behind


def test_only_the_newest_copies_are_kept(tmp_path):
    config, database = make(tmp_path, keep=2)
    start = datetime(2026, 11, 20, tzinfo=timezone.utc)
    names = [create_backup(database.path, config.backup_dir, 2, start + timedelta(days=d)).name for d in range(4)]
    assert [p.name for p in backup_files(config.backup_dir)] == [names[3], names[2]]


def test_due_follows_the_age_of_the_newest_copy(tmp_path):
    config, database = make(tmp_path, every_hours=24)
    assert backup_status(config, database)["due"] and backup_status(config, database)["overdue"]   # never backed up
    assert run_if_due(config, database)["file"]
    assert run_if_due(config, database) is None                                                    # fresh: nothing to do
    later = datetime.now(timezone.utc).timestamp()
    assert backup_status(config, database, now=later + 25 * 3600)["due"]
    assert not backup_status(config, database, now=later + 25 * 3600)["overdue"]
    assert backup_status(config, database, now=later + 49 * 3600)["overdue"]
    off, other = make(tmp_path / "off", enabled=False)
    assert run_if_due(off, other) is None and not backup_status(off, other)["overdue"]


def test_upload_hands_the_file_to_rclone_without_a_shell(tmp_path):
    calls = []

    def run(arguments, **options):
        calls.append(arguments)
        assert "shell" not in options
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    config, database = make(tmp_path, remote="gdrive:crypto backups", remote_keep_days=90)
    status = run_backup(config, database, run=run)
    assert status["uploaded"] is True and status["error"] == ""
    assert calls == [["rclone", "copy", str(config.backup_dir / status["file"]), "gdrive:crypto backups"],
                     ["rclone", "delete", "--min-age", "90d", "--include", "portfolio-*.db.gz", "gdrive:crypto backups"]]


def test_a_failed_upload_is_reported_and_keeps_the_local_copy(tmp_path):
    def refused(arguments, **options):
        return SimpleNamespace(returncode=1, stdout="", stderr="2026/11/28 NOTICE: retrying\nFailed to copy: token expired")

    def missing(arguments, **options):
        raise FileNotFoundError("rclone")

    def hung(arguments, **options):
        raise subprocess.TimeoutExpired(arguments, 600)

    config, database = make(tmp_path, remote="mega:backups")
    status = run_backup(config, database, run=refused)
    assert status["uploaded"] is False and status["error"] == "rclone failed: Failed to copy: token expired"
    assert backup_files(config.backup_dir) and backup_status(config, database)["last_run"]["error"] == status["error"]
    assert "not installed" in run_backup(config, database, run=missing)["error"]
    assert "did not finish" in run_backup(config, database, run=hung)["error"]
    with pytest.raises(RuntimeError):
        upload(tmp_path / "x", "mega:backups", run=refused)


def test_a_remote_that_is_not_an_rclone_destination_is_never_run(tmp_path):
    for bad in ("--config=/etc/passwd", "/tmp/somewhere", "gdrive:x; rm -rf ~", "$(whoami):x"):
        assert BackupSettings.from_raw({"remote": bad}).remote == "", bad
    config, database = make(tmp_path, remote="--config=/etc/passwd")
    status = run_backup(config, database, run=lambda *a, **k: pytest.fail("rclone must not be called"))
    assert status["uploaded"] is False and "not an rclone destination" in status["error"] and status["file"]


def test_backup_routes(tmp_path, monkeypatch):
    config, database = make(tmp_path)
    client = TestClient(web_api.create_app(config.path, fetcher=lambda *a: None))
    assert client.get("/api/backup").json()["latest"] is None
    assert client.get("/api/backup/download").status_code == 404
    state = client.post("/api/backup").json()
    assert state["count"] == 1 and state["last_run"]["error"] == "" and not state["overdue"]
    download = client.get("/api/backup/download")
    assert download.status_code == 200 and state["latest"] in download.headers["content-disposition"]
    assert gzip.decompress(download.content)[:15] == b"SQLite format 3"
    monkeypatch.setenv("PORTFOLIO_WEB_TOKEN", "secret-token-123")
    for call in (client.get, client.post):
        assert call("/api/backup").status_code == 401
    assert client.get("/api/backup/download").status_code == 401
