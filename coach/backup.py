"""Nightly backup of coach.db to Oracle Object Storage, over a write-only link.

The VM needs no Oracle login for this. `BACKUP_PAR_URL` (in .env) is a
pre-authenticated request: a long unguessable URL that lets the holder PUT one
named object and nothing else. It cannot read, list or delete. The bucket has
versioning on, so each night's upload becomes a new version and an overwrite
(even by a leaked link) never destroys an earlier one. Restoring uses the
owner's own Oracle console login, not a key.

Run as `python -m coach.backup` (a systemd timer does it daily). On success it
records `last_backup_at` and flips `backup_enabled`, which lets rules.py warn
the owner if backups later stop; on failure it records a short error (never
the URL, which is a secret) and exits non-zero.
"""

import hashlib
import logging
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from coach import db

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

log = logging.getLogger(__name__)


def snapshot(src: Path, dest: Path) -> None:
    """A consistent copy of a live SQLite file, safe while the bot is writing.

    Uses SQLite's online-backup API rather than copying the file, which could
    catch a write half-done (WAL mode keeps recent writes outside the main
    file). The copy is integrity-checked before anything is uploaded.
    """
    source = sqlite3.connect(src)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(dest)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise RuntimeError(f"integrity check failed on the copy: {verdict}")


def upload(url: str, path: Path, timeout: int = 60) -> None:
    request = urllib.request.Request(
        url,
        data=path.read_bytes(),
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"upload answered HTTP {response.status}")


def describe(exc: Exception) -> str:
    """A short, URL-free description of a failure (the URL is a secret)."""
    if isinstance(exc, urllib.error.HTTPError):
        hint = " (the backup link has probably expired)" if exc.code in (401, 403, 404) else ""
        return f"upload refused: HTTP {exc.code}{hint}"
    if isinstance(exc, urllib.error.URLError):
        return f"upload failed: {type(exc.reason).__name__}"
    return f"{type(exc).__name__}: {str(exc)[:80]}"


def run() -> int:
    url = (dotenv_values(ENV_PATH).get("BACKUP_PAR_URL") or "").strip()
    if not url:
        log.error("BACKUP_PAR_URL is not set in .env; nothing to back up to")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "coach.db"
        try:
            snapshot(db.DB_PATH, copy)
            digest = hashlib.sha256(copy.read_bytes()).hexdigest()
            size = copy.stat().st_size
            upload(url, copy)
        except Exception as exc:
            message = describe(exc)
            db.set_setting("last_backup_error", message)
            log.error("backup FAILED: %s", message)
            return 1
    db.set_setting("last_backup_at", datetime.now(timezone.utc).isoformat())
    db.set_setting("backup_enabled", "1")
    db.clear_setting("last_backup_error")
    log.info("backup ok: %d bytes, sha256=%s", size, digest)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())
