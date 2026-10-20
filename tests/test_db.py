from datetime import datetime, timedelta, timezone

import pytest

from coach import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "coach_test.db")
    db.init_db()


def test_init_db_is_idempotent():
    db.init_db()  # should not raise on a second call
    conn = db.get_connection()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"daily_snapshots", "events", "conversations", "settings"} <= tables


def test_upsert_snapshot_inserts_then_updates():
    db.upsert_snapshot("2026-09-17", recovery=79, hrv=63.1)
    row = db.get_snapshot("2026-09-17")
    assert row["recovery"] == 79
    assert row["hrv"] == 63.1
    assert row["strain"] is None

    db.upsert_snapshot("2026-09-17", strain=12.5)
    row = db.get_snapshot("2026-09-17")
    assert row["recovery"] == 79  # untouched fields survive the update
    assert row["strain"] == 12.5


def test_get_snapshots_filters_by_days():
    today = datetime.now(timezone.utc).date()
    db.upsert_snapshot(today.isoformat(), recovery=50)
    db.upsert_snapshot((today - timedelta(days=20)).isoformat(), recovery=60)

    rows = db.get_snapshots(days=14)
    dates = {r["date"] for r in rows}
    assert today.isoformat() in dates
    assert (today - timedelta(days=20)).isoformat() not in dates


def test_insert_event_rejects_unknown_kind():
    with pytest.raises(ValueError):
        db.insert_event("skydiving", "note")


def test_insert_and_get_events():
    db.insert_event("caffeine", "double espresso")
    db.insert_event("food", "eggs")

    all_events = db.get_events()
    assert len(all_events) == 2

    caffeine_only = db.get_events(kind="caffeine")
    assert len(caffeine_only) == 1
    assert caffeine_only[0]["note"] == "double espresso"


def test_get_events_excludes_old_events():
    old_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db.insert_event("note", "old note", at=old_at)
    db.insert_event("note", "recent note")

    rows = db.get_events(days=30)
    notes = {r["note"] for r in rows}
    assert "recent note" in notes
    assert "old note" not in notes


def test_conversation_roundtrip():
    db.add_conversation("thread-1", "user", "hello")
    db.add_conversation("thread-1", "assistant", "hi there")
    db.add_conversation("thread-2", "user", "unrelated thread")

    rows = db.get_conversation("thread-1")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert [r["content"] for r in rows] == ["hello", "hi there"]


def test_settings_roundtrip():
    assert db.get_setting("tz_override") is None
    assert db.get_setting("tz_override", default="UTC") == "UTC"

    db.set_setting("tz_override", "Asia/Tokyo")
    assert db.get_setting("tz_override") == "Asia/Tokyo"

    db.set_setting("tz_override", "America/New_York")
    assert db.get_setting("tz_override") == "America/New_York"


def test_local_today_follows_the_owner_not_the_host():
    """03:30Z on the 19th is still the evening of the 18th in New York. The old
    host-date code (UTC on the VM) would have said the 19th."""
    db.set_setting("tz_override", "America/New_York")
    assert db.local_today(datetime(2026, 10, 19, 3, 30, tzinfo=timezone.utc)) == "2026-10-18"


def test_local_today_east_of_utc_is_ahead_of_utc():
    db.set_setting("tz_override", "Asia/Tokyo")
    assert db.local_today(datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)) == "2026-10-19"


def test_local_today_uses_the_slack_zone_when_there_is_no_override():
    db.set_setting("slack_tz", "America/New_York")
    assert db.local_today(datetime(2026, 10, 19, 3, 30, tzinfo=timezone.utc)) == "2026-10-18"
