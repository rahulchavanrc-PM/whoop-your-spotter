"""Nightly backup over a write-only link. A local fake server; no network."""

import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from coach import backup, db

SECRET = "SECRETTOKEN123"


@pytest.fixture
def server():
    seen = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            seen.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(state["status"])
            self.end_headers()

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, seen, state
    httpd.shutdown()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch, server):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "coach.db")
    db.init_db()
    db.insert_event("note", "something worth keeping")
    httpd, _, _ = server
    env_file = tmp_path / ".env"
    env_file.write_text(f"BACKUP_PAR_URL=http://127.0.0.1:{httpd.server_port}/p/{SECRET}/o/coach.db\n")
    monkeypatch.setattr(backup, "ENV_PATH", env_file)
    return env_file


def test_snapshot_copies_a_live_wal_database_consistently(tmp_path):
    copy = tmp_path / "copy.db"
    backup.snapshot(db.DB_PATH, copy)
    rows = sqlite3.connect(copy).execute("select note from events").fetchall()
    assert rows == [("something worth keeping",)]


def test_a_successful_backup_uploads_a_real_sqlite_file_and_records_it(server):
    _, seen, _ = server
    assert backup.run() == 0
    assert len(seen) == 1
    path, body = seen[0]
    assert SECRET in path and body.startswith(b"SQLite format 3\x00")
    assert db.get_setting("backup_enabled") == "1"
    datetime.fromisoformat(db.get_setting("last_backup_at"))
    assert db.get_setting("last_backup_error") is None


def test_a_refused_upload_fails_loudly_and_never_leaks_the_link(server):
    _, _, state = server
    state["status"] = 403
    assert backup.run() == 1
    error = db.get_setting("last_backup_error")
    assert "403" in error and "expired" in error
    assert SECRET not in error
    assert db.get_setting("last_backup_at") is None and db.get_setting("backup_enabled") is None


def test_an_unreachable_server_fails_without_the_link(server):
    httpd, _, _ = server
    httpd.shutdown()
    httpd.server_close()
    assert backup.run() == 1
    assert SECRET not in db.get_setting("last_backup_error")


def test_a_later_success_clears_the_earlier_error(server):
    _, _, state = server
    state["status"] = 500
    backup.run()
    assert db.get_setting("last_backup_error")
    state["status"] = 200
    assert backup.run() == 0
    assert db.get_setting("last_backup_error") is None


def test_no_link_configured_does_nothing_and_says_so(env):
    env.write_text("OTHER=1\n")
    assert backup.run() == 2
    assert db.get_setting("backup_enabled") is None
