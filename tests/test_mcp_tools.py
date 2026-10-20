"""The agent-facing WHOOP tools, with a fake WHOOP: which cycle a date means,
and the timezone override. No network."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from coach import db, mcp_server, whoop

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")


class FakeWhoop:
    """Real cycle_containing() logic over a canned list of cycles."""

    def __init__(self, cycles, recoveries=None, sleeps=None, workouts=None):
        self.cycles = cycles
        self.recoveries = recoveries or {}
        self.sleeps = sleeps or {}
        self.workouts = workouts or []
        self.workout_window = None

    def _get_collection(self, path, params):
        return self.cycles

    def cycle_containing(self, instant):
        return whoop.WhoopClient.cycle_containing(self, instant)

    def recovery_for_cycle(self, cycle_id):
        return self.recoveries.get(cycle_id)

    def sleep_for_cycle(self, cycle_id):
        return self.sleeps.get(cycle_id)

    def workouts_between(self, start, end):
        self.workout_window = (start, end)
        return self.workouts


def recovery(score, created):
    return {"created_at": created, "score": {"recovery_score": score, "hrv_rmssd_milli": 50.0, "resting_heart_rate": 58}}


# A realistic shape: a cycle runs ~01:00 to ~23:45 Eastern.
CYCLES = [
    {"id": 2, "start": "2026-10-19T03:44:00.000Z", "end": None, "score": {"strain": 5.2, "average_heart_rate": 70}},
    {"id": 1, "start": "2026-10-18T05:08:00.000Z", "end": "2026-10-19T03:44:00.000Z", "score": {"strain": 5.3, "average_heart_rate": 71}},
    {"id": 0, "start": "2026-10-17T06:44:00.000Z", "end": "2026-10-18T05:08:00.000Z", "score": {"strain": 5.0, "average_heart_rate": 69}},
]


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "tools_test.db")
    db.init_db()
    db.set_setting("tz_override", "America/New_York")


def at(monkeypatch, moment):
    monkeypatch.setattr(mcp_server, "_now", lambda: moment)


def use(monkeypatch, fake):
    monkeypatch.setattr(mcp_server, "_whoop", lambda: fake)


# --- which instant a date means ----------------------------------------------


def test_today_means_now():
    now = datetime(2026, 10, 19, 23, 15, tzinfo=ET)
    assert mcp_server._reference_instant("today", now) == (now, None)


def test_a_past_date_means_that_days_local_noon():
    now = datetime(2026, 10, 19, 23, 15, tzinfo=ET)
    instant, error = mcp_server._reference_instant("2026-10-18", now)
    assert error is None and instant == datetime(2026, 10, 18, 12, 0, tzinfo=ET)


def test_todays_date_before_noon_means_now():
    now = datetime(2026, 10, 19, 8, 0, tzinfo=ET)
    instant, _ = mcp_server._reference_instant("2026-10-19", now)
    assert instant == now


def test_future_and_garbage_dates_are_rejected():
    now = datetime(2026, 10, 19, 8, 0, tzinfo=ET)
    assert mcp_server._reference_instant("2026-10-20", now)[0] is None
    assert "future" in mcp_server._reference_instant("2026-10-20", now)[1]
    assert "YYYY-MM-DD" in mcp_server._reference_instant("yesterday", now)[1]


# --- which cycle a date maps to ----------------------------------------------


def test_each_date_gets_its_own_cycle(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 23, 15, tzinfo=ET))
    use(monkeypatch, FakeWhoop(CYCLES, {2: recovery(64, "2026-10-19T12:25:00Z"), 1: recovery(55, "2026-10-18T17:53:00Z")}))
    assert mcp_server.get_recovery("2026-10-18")["recovery_score"] == 55
    assert mcp_server.get_recovery("2026-10-19")["recovery_score"] == 64
    assert mcp_server.get_recovery("2026-10-18")["cycle_id"] == 1


def test_the_evening_after_utc_midnight_still_finds_todays_cycle(monkeypatch):
    """23:15 Eastern on the 18th is already the 19th in UTC. A UTC-day window
    returned nothing here; the cycle lookup finds the cycle that is running."""
    running = [dict(CYCLES[1], end=None)]
    at(monkeypatch, datetime(2026, 10, 18, 23, 15, tzinfo=ET))
    use(monkeypatch, FakeWhoop(running, {1: recovery(55, "2026-10-18T17:53:00Z")}))
    result = mcp_server.get_recovery("today")
    assert result["found"] and result["recovery_score"] == 55


def test_east_of_utc_the_morning_cycle_is_found(monkeypatch):
    """Tokyo: sleep began 23:00 JST (14:00Z the day before). A UTC-day window
    for 'today' missed it; the cycle lookup does not."""
    db.set_setting("tz_override", "Asia/Tokyo")
    cycles = [{"id": 9, "start": "2026-10-18T14:00:00.000Z", "end": None, "score": {"strain": 1.0}}]
    at(monkeypatch, datetime(2026, 10, 19, 7, 30, tzinfo=JST))
    use(monkeypatch, FakeWhoop(cycles, {9: recovery(71, "2026-10-18T22:00:00Z")}))
    assert mcp_server.get_recovery("today")["recovery_score"] == 71


def test_a_cycle_without_a_scored_recovery_says_so(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 8, 0, tzinfo=ET))
    use(monkeypatch, FakeWhoop(CYCLES))
    result = mcp_server.get_recovery("today")
    assert result["found"] is False and result["cycle_id"] == 2 and "no scored recovery" in result["note"]


def test_no_cycle_for_a_date_is_not_found(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 8, 0, tzinfo=ET))
    use(monkeypatch, FakeWhoop([]))
    assert mcp_server.get_recovery("2026-10-10") == {"found": False, "date": "2026-10-10"}


def test_a_future_date_is_an_error_not_a_lookup(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 8, 0, tzinfo=ET))
    use(monkeypatch, FakeWhoop(CYCLES))
    assert mcp_server.get_sleep("2026-12-01")["error"] == "that date is in the future"


def test_recovery_snapshot_is_filed_under_the_records_own_local_date(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 23, 15, tzinfo=ET))
    use(monkeypatch, FakeWhoop(CYCLES, {1: recovery(55, "2026-10-18T17:53:00Z")}))
    mcp_server.get_recovery("2026-10-18")
    assert db.get_snapshot("2026-10-18")["recovery"] == 55
    assert db.get_snapshot("2026-10-19") is None


def test_sleep_uses_the_cycles_sleep(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 12, 0, tzinfo=ET))
    sleep = {"start": "2026-10-19T03:44:00Z", "end": "2026-10-19T11:30:00Z",
             "score": {"sleep_performance_percentage": 82, "stage_summary": {
                 "total_light_sleep_time_milli": 3_600_000, "total_slow_wave_sleep_time_milli": 3_600_000,
                 "total_rem_sleep_time_milli": 3_600_000}}}
    use(monkeypatch, FakeWhoop(CYCLES, sleeps={2: sleep}))
    result = mcp_server.get_sleep("today")
    assert result["performance_pct"] == 82 and result["hours_asleep"] == 3.0 and result["cycle_id"] == 2


def test_strain_of_the_running_cycle_updates_todays_snapshot(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 19, 0, tzinfo=ET))
    fake = FakeWhoop(CYCLES, workouts=[{}, {}])
    use(monkeypatch, fake)
    result = mcp_server.get_strain("today")
    assert result["in_progress"] and result["day_strain"] == 5.2 and result["workouts_count"] == 2
    assert db.get_snapshot("2026-10-19")["strain"] == 5.2
    assert fake.workout_window[0] == "2026-10-19T03:44:00.000Z"


def test_strain_of_a_finished_day_is_not_filed_under_today(monkeypatch):
    at(monkeypatch, datetime(2026, 10, 19, 19, 0, tzinfo=ET))
    use(monkeypatch, FakeWhoop(CYCLES))
    result = mcp_server.get_strain("2026-10-18")
    assert result["in_progress"] is False and result["day_strain"] == 5.3
    assert db.get_snapshot("2026-10-19") is None


# --- the timezone override tool ----------------------------------------------


def test_set_timezone_overrides_and_reports_local_time():
    db.clear_setting("tz_override")
    db.set_setting("slack_tz", "America/New_York")
    result = mcp_server.set_timezone("Asia/Tokyo")
    assert result["ok"] and result["timezone"] == "Asia/Tokyo"
    assert "JST" in result["local_time_now"]
    assert str(db.resolve_timezone()) == "Asia/Tokyo"


def test_set_timezone_rejects_a_name_it_does_not_know_and_changes_nothing():
    db.set_setting("tz_override", "America/New_York")
    result = mcp_server.set_timezone("Tokyo")
    assert result["ok"] is False and "IANA" in result["error"]
    assert db.get_setting("tz_override") == "America/New_York"


def test_set_timezone_auto_goes_back_to_the_slack_zone():
    db.set_setting("slack_tz", "Europe/London")
    mcp_server.set_timezone("Asia/Tokyo")
    result = mcp_server.set_timezone("auto")
    assert result["ok"] and result["timezone"] == "Europe/London"
    assert db.get_setting("tz_override") is None


def test_set_timezone_trims_whitespace():
    assert mcp_server.set_timezone("  Europe/Paris ")["timezone"] == "Europe/Paris"
