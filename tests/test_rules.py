import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from coach import db, rules

TZ = ZoneInfo("Europe/London")


def local(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


# --- quiet hours -------------------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [(22, False), (23, True), (0, True), (6, True), (7, False), (12, False)],
)
def test_in_quiet_hours(hour, expected):
    assert rules.in_quiet_hours(local(2026, 9, 18, hour, 0)) is expected


# --- nudges --------------------------------------------------------------


def test_nudge_fires_in_grace_window():
    assert rules.due_nudge(local(2026, 9, 18, 10, 35), None, None) is True


def test_nudge_does_not_fire_outside_grace_window():
    assert rules.due_nudge(local(2026, 9, 18, 10, 50), None, None) is False


def test_nudge_skipped_if_recently_logged():
    now = local(2026, 9, 18, 10, 35)
    last_event = now - timedelta(minutes=30)
    assert rules.due_nudge(now, last_event, None) is False


def test_nudge_fires_if_event_was_over_two_hours_ago():
    now = local(2026, 9, 18, 10, 35)
    last_event = now - timedelta(hours=3)
    assert rules.due_nudge(now, last_event, None) is True


def test_nudge_does_not_repeat_within_same_slot():
    now = local(2026, 9, 18, 10, 35)
    last_nudge = local(2026, 9, 18, 10, 32)
    assert rules.due_nudge(now, None, last_nudge) is False


def test_nudge_fires_again_at_the_next_slot():
    now = local(2026, 9, 18, 14, 35)
    last_nudge = local(2026, 9, 18, 10, 32)  # fired at the 10:30 slot
    assert rules.due_nudge(now, None, last_nudge) is True


def test_nudge_suppressed_in_quiet_hours():
    # Not a real slot time, but quiet hours must win regardless.
    assert rules.due_nudge(local(2026, 9, 18, 23, 30), None, None) is False


# --- evening check-in ------------------------------------------------------


def test_checkin_fires_once_in_its_window():
    assert rules.due_checkin(local(2026, 9, 18, 20, 35), None) is True


def test_checkin_does_not_repeat_same_day():
    last = local(2026, 9, 18, 20, 31)
    assert rules.due_checkin(local(2026, 9, 18, 20, 40), last) is False


def test_checkin_fires_next_day_even_with_a_prior_checkin():
    last = local(2026, 9, 18, 20, 31)
    assert rules.due_checkin(local(2026, 9, 19, 20, 35), last) is True


# --- morning brief: the wake-to-wake trap ---------------------------------


def test_brief_fires_for_a_new_cycle():
    recovery = {"cycle_id": 123, "score": {"recovery_score": 80}}
    assert rules.due_brief(recovery, last_brief_cycle_id=None) is True


def test_brief_does_not_refire_for_the_same_cycle():
    """The exact wake-to-wake case: a lookup for two different calendar
    dates can return the identical record. Gating on cycle id (not on
    "a record exists for today") is what stops a double brief."""
    recovery = {"cycle_id": 123, "score": {"recovery_score": 80}}
    assert rules.due_brief(recovery, last_brief_cycle_id="123") is False


def test_brief_fires_for_a_genuinely_new_cycle():
    recovery = {"cycle_id": 456, "score": {"recovery_score": 85}}
    assert rules.due_brief(recovery, last_brief_cycle_id="123") is True


def test_brief_does_not_fire_with_no_recovery():
    assert rules.due_brief(None, last_brief_cycle_id="123") is False


def test_brief_does_not_fire_without_a_cycle_id():
    assert rules.due_brief({"score": {}}, last_brief_cycle_id=None) is False


# --- threshold alerts --------------------------------------------------


def _prior_days(recoveries, hrvs):
    return [{"recovery": r, "hrv": h} for r, h in zip(recoveries, hrvs)]


def test_recovery_alert_fires_below_85pct_of_average():
    prior = _prior_days([80, 82, 78], [60, 62, 58])
    today = {"recovery": 60, "hrv": 61, "strain": 5}  # 60 < 0.85 * 80
    assert rules.alerts_to_fire(today, prior, already_fired=set()) == ["recovery"]


def test_recovery_alert_does_not_fire_above_threshold():
    prior = _prior_days([80, 82, 78], [60, 62, 58])
    today = {"recovery": 75, "hrv": 61, "strain": 5}
    assert "recovery" not in rules.alerts_to_fire(today, prior, already_fired=set())


def test_hrv_alert_fires_below_80pct_of_average():
    prior = _prior_days([80, 82, 78], [60, 60, 60])
    today = {"recovery": 79, "hrv": 45, "strain": 5}  # 45 < 0.8 * 60
    assert rules.alerts_to_fire(today, prior, already_fired=set()) == ["hrv"]


def test_strain_alert_fires_for_high_strain_low_recovery():
    today = {"recovery": 40, "hrv": 60, "strain": 19}
    assert rules.alerts_to_fire(today, prior=[], already_fired=set()) == ["strain"]


def test_strain_alert_needs_both_conditions():
    high_strain_ok_recovery = {"recovery": 70, "hrv": 60, "strain": 19}
    assert rules.alerts_to_fire(high_strain_ok_recovery, [], set()) == []

    low_recovery_ok_strain = {"recovery": 40, "hrv": 60, "strain": 10}
    assert rules.alerts_to_fire(low_recovery_ok_strain, [], set()) == []


def test_alert_does_not_refire_same_kind_same_day():
    today = {"recovery": 40, "hrv": 60, "strain": 19}
    assert rules.alerts_to_fire(today, [], already_fired={"strain"}) == []


def test_no_alerts_without_enough_baseline_days():
    prior = _prior_days([80, 82], [60, 62])  # only 2 days, below MIN_BASELINE_DAYS
    today = {"recovery": 10, "hrv": 10, "strain": 5}
    assert rules.alerts_to_fire(today, prior, already_fired=set()) == []


def test_multiple_alerts_can_fire_together():
    prior = _prior_days([80, 82, 78], [60, 62, 58])
    today = {"recovery": 50, "hrv": 40, "strain": 5}
    fired = rules.alerts_to_fire(today, prior, already_fired=set())
    assert set(fired) == {"recovery", "hrv"}


# --- tick(): the full orchestration, per Phase 4's done-when criterion -----


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rules_test.db")
    db.init_db()
    db.set_setting("tz_override", "Europe/London")


def test_simulated_tick_fires_the_morning_brief_once(monkeypatch):
    """A wake-to-wake cycle can satisfy 'today's recovery exists' on two
    separate ticks (or two separate calendar dates) with the SAME record.
    tick() must brief it exactly once, not once per tick."""
    recovery = {"cycle_id": 999, "created_at": "2026-09-18T09:00:00Z", "score": {}}
    monkeypatch.setattr(rules, "poll_and_snapshot", lambda tz, now=None: recovery)

    woken = []

    async def wake(prompt):
        woken.append(prompt)

    morning = local(2026, 9, 18, 8, 0)
    asyncio.run(rules.tick(wake, now=morning))
    asyncio.run(rules.tick(wake, now=morning + timedelta(minutes=5)))
    asyncio.run(rules.tick(wake, now=morning + timedelta(hours=2)))

    briefs = [p for p in woken if "morning brief" in p]
    assert len(briefs) == 1
    assert db.get_setting("last_brief_cycle_id") == "999"


def test_tick_briefs_again_for_a_genuinely_new_cycle_next_day(monkeypatch):
    def make_recovery(cycle_id):
        return {"cycle_id": cycle_id, "created_at": "2026-09-18T09:00:00Z", "score": {}}

    woken = []

    async def wake(prompt):
        woken.append(prompt)

    async def run():
        monkeypatch.setattr(rules, "poll_and_snapshot", lambda tz, now=None: make_recovery(1))
        await rules.tick(wake, now=local(2026, 9, 18, 8, 0))
        monkeypatch.setattr(rules, "poll_and_snapshot", lambda tz, now=None: make_recovery(2))
        await rules.tick(wake, now=local(2026, 9, 19, 8, 0))

    asyncio.run(run())
    briefs = [p for p in woken if "morning brief" in p]
    assert len(briefs) == 2


def test_tick_stays_silent_in_quiet_hours(monkeypatch):
    calls = []
    monkeypatch.setattr(rules, "poll_and_snapshot", lambda tz, now=None: calls.append(1))

    async def wake(prompt):
        raise AssertionError(f"should not have woken the agent: {prompt}")

    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 23, 30)))
    assert calls == [1]  # data still polled; just nothing said


def test_tick_fires_a_nudge_when_nothing_else_is_due(monkeypatch):
    monkeypatch.setattr(rules, "poll_and_snapshot", lambda tz, now=None: None)

    woken = []

    async def wake(prompt):
        woken.append(prompt)

    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 10, 32)))
    assert len(woken) == 1
    assert "nudge" in woken[0].lower()
    assert db.get_setting("last_nudge_at") is not None


def _boom(tz, now=None):
    raise RuntimeError("WHOOP is down")


def test_whoop_failure_at_a_quiet_moment_does_not_crash_or_speak(monkeypatch):
    monkeypatch.setattr(rules, "poll_and_snapshot", _boom)

    async def wake(prompt):
        raise AssertionError(f"should stay silent: {prompt}")

    # 12:00 is no nudge/check-in slot, and this is only the 1st failure.
    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 12, 0)))
    assert db.get_setting("whoop_fail_count") == "1"


def test_whoop_outage_does_not_silence_the_nudge(monkeypatch):
    """Nudges don't need WHOOP data, so a broken WHOOP login must not stop them
    (it did, for hours, before this was fixed)."""
    monkeypatch.setattr(rules, "poll_and_snapshot", _boom)
    woken = []

    async def wake(prompt):
        woken.append(prompt)

    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 10, 32)))
    assert len(woken) == 1 and "nudge" in woken[0].lower()


def test_third_consecutive_failure_tells_him_once_a_day(monkeypatch):
    monkeypatch.setattr(rules, "poll_and_snapshot", _boom)
    woken = []

    async def wake(prompt):
        woken.append(prompt)

    t = local(2026, 9, 18, 12, 0)
    for minutes in (0, 5, 10, 15, 20):
        asyncio.run(rules.tick(wake, now=t + timedelta(minutes=minutes)))

    alerts = [p for p in woken if "WHOOP connection" in p]
    assert len(alerts) == 1  # fired on the 3rd failure, not again on the 4th/5th


def test_a_success_resets_the_failure_count(monkeypatch):
    async def wake(prompt):
        pass

    monkeypatch.setattr(rules, "poll_and_snapshot", _boom)
    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 12, 0)))
    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 12, 5)))
    assert db.get_setting("whoop_fail_count") == "2"

    monkeypatch.setattr(rules, "poll_and_snapshot", lambda tz, now=None: None)
    asyncio.run(rules.tick(wake, now=local(2026, 9, 18, 12, 10)))
    assert db.get_setting("whoop_fail_count") == "0"


# --- the fixes for a user far from UTC (2026-10-19) ---------

ET = ZoneInfo("America/New_York")


def et(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=ET)


class FakeWhoopLatest:
    """Only the *latest* lookups exist. If poll_and_snapshot ever goes back to
    date-window queries it will hit AttributeError and these tests will fail."""

    def __init__(self, recovery=None, sleep=None, cycle=None):
        self._r, self._s, self._c = recovery, sleep, cycle

    def latest_recovery(self):
        return self._r

    def latest_sleep(self):
        return self._s

    def latest_cycle(self):
        return self._c


def _recovery(created="2026-10-19T12:25:00Z", cycle_id=7):
    return {"cycle_id": cycle_id, "created_at": created,
            "score": {"recovery_score": 64, "hrv_rmssd_milli": 54.0, "resting_heart_rate": 60}}


def _sleep(end="2026-10-19T11:30:00Z"):
    return {"end": end, "score": {"sleep_performance_percentage": 88, "stage_summary": {
        "total_light_sleep_time_milli": 3_600_000, "total_slow_wave_sleep_time_milli": 3_600_000,
        "total_rem_sleep_time_milli": 3_600_000}}}


def test_evening_poll_keeps_updating_strain_after_utc_midnight(monkeypatch):
    """The bug: at 21:00 Eastern it is already tomorrow in UTC, and a 'today'
    lookup returned nothing, so strain stopped updating every evening."""
    cycle = {"end": None, "score": {"strain": 11.5}}
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(_recovery(), _sleep(), cycle))
    woken = []

    async def wake(prompt):
        woken.append(prompt)

    db.set_setting("tz_override", "America/New_York")
    asyncio.run(rules.tick(wake, now=et(2026, 10, 19, 21, 0)))  # 01:00Z on the 20th
    assert db.get_snapshot("2026-10-19")["strain"] == 11.5
    assert db.get_snapshot("2026-10-20") is None  # nothing misfiled under the UTC date


def test_snapshot_rows_are_filed_under_each_records_own_local_date(monkeypatch):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(
        _recovery(created="2026-10-19T12:25:00Z"), _sleep(end="2026-10-19T11:30:00Z"), None))
    rules.poll_and_snapshot(ET, now=et(2026, 10, 19, 14, 0))
    row = db.get_snapshot("2026-10-19")
    assert row["recovery"] == 64 and row["sleep_perf"] == 88 and row["sleep_hours"] == 3.0


def test_a_stale_latest_record_is_never_filed_under_today(monkeypatch):
    old = _recovery(created="2026-10-10T12:00:00Z")
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(old, None, None))
    rules.poll_and_snapshot(ET, now=et(2026, 10, 19, 14, 0))
    assert db.get_snapshot("2026-10-19") is None
    assert db.get_snapshot("2026-10-10")["recovery"] == 64


def test_a_finished_cycle_does_not_write_todays_strain(monkeypatch):
    done = {"end": "2026-10-18T05:00:00Z", "score": {"strain": 14.0}}
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(None, None, done))
    rules.poll_and_snapshot(ET, now=et(2026, 10, 19, 14, 0))
    assert db.get_snapshot("2026-10-19") is None


def test_east_of_utc_the_morning_cycle_is_still_found(monkeypatch):
    """Tokyo: sleep began 14:00Z the day before, recovery ready 22:00Z. A UTC- or
    local-date window misses it; 'latest' does not, so the brief still fires."""
    tokyo = ZoneInfo("Asia/Tokyo")
    rec = _recovery(created="2026-10-18T22:00:00Z", cycle_id=42)
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(rec, None, None))
    woken = []

    async def wake(prompt):
        woken.append(prompt)

    db.set_setting("tz_override", "Asia/Tokyo")
    asyncio.run(rules.tick(wake, now=datetime(2026, 10, 19, 7, 30, tzinfo=tokyo)))
    assert any("morning brief" in p for p in woken)


def test_a_stale_recovery_is_not_announced_as_new():
    old = {"cycle_id": 5, "created_at": "2026-10-17T12:00:00Z"}
    assert rules.due_brief(old, None, now=et(2026, 10, 19, 14, 0)) is False  # ~50 h old


def test_a_recent_recovery_is_announced():
    fresh = {"cycle_id": 5, "created_at": "2026-10-19T12:25:00Z"}
    assert rules.due_brief(fresh, None, now=et(2026, 10, 19, 14, 0)) is True


def test_every_tick_logs_exactly_one_line(monkeypatch, caplog):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(None, None, None))

    async def wake(prompt):
        pass

    with caplog.at_level("INFO", logger="coach.rules"):
        asyncio.run(rules.tick(wake, now=et(2026, 10, 19, 12, 0)))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tick")]
    assert len(lines) == 1
    assert lines[0].startswith("tick ok: poll=ok decision=none local=2026-10-19 12:00 EDT")


def test_a_failed_poll_logs_a_degraded_line(monkeypatch, caplog):
    monkeypatch.setattr(rules, "poll_and_snapshot", _boom)

    async def wake(prompt):
        pass

    with caplog.at_level("INFO", logger="coach.rules"):
        asyncio.run(rules.tick(wake, now=et(2026, 10, 19, 12, 0)))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tick")]
    assert len(lines) == 1 and lines[0].startswith("tick DEGRADED: poll=FAILED(1 in a row)")


def test_the_log_line_names_what_the_tick_did(monkeypatch, caplog):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(_recovery(cycle_id=99), None, None))

    async def wake(prompt):
        pass

    with caplog.at_level("INFO", logger="coach.rules"):
        asyncio.run(rules.tick(wake, now=et(2026, 10, 19, 9, 0)))
    assert any("decision=brief" in r.getMessage() for r in caplog.records)


# --- a failed proactive message stays due; the overdue-backup alert -------------


def _late(h, m):
    return et(2026, 10, 19, h, m)


def test_a_failed_morning_brief_is_not_marked_sent_and_is_retried(monkeypatch):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(_recovery(cycle_id=77)))
    sent = []

    async def wake(prompt):
        sent.append(prompt)
        if len(sent) == 1:
            raise RuntimeError("rate limited")

    asyncio.run(rules.tick(wake, now=_late(9, 0)))
    assert db.get_setting("last_brief_cycle_id") is None        # not recorded as sent
    asyncio.run(rules.tick(wake, now=_late(9, 5)))
    assert db.get_setting("last_brief_cycle_id") == "77"          # retried and delivered
    assert len(sent) == 2


def test_a_failed_nudge_is_retried_within_its_slot(monkeypatch):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest())
    attempts = []

    async def wake(prompt):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise RuntimeError("down")

    asyncio.run(rules.tick(wake, now=_late(10, 31)))
    assert db.get_setting("last_nudge_at") is None
    asyncio.run(rules.tick(wake, now=_late(10, 36)))
    assert db.get_setting("last_nudge_at") is not None


def test_the_tick_log_names_a_failed_delivery(monkeypatch, caplog):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest(_recovery(cycle_id=5)))

    async def wake(prompt):
        raise RuntimeError("down")

    with caplog.at_level("INFO", logger="coach.rules"):
        asyncio.run(rules.tick(wake, now=_late(9, 0)))
    assert any("decision=brief-FAILED" in r.getMessage() for r in caplog.records)


def test_backup_overdue_logic():
    now = _late(12, 0)
    old = now - timedelta(hours=40)
    assert rules.backup_overdue(old, True, now) is True
    assert rules.backup_overdue(now - timedelta(hours=20), True, now) is False
    assert rules.backup_overdue(None, True, now) is True      # enabled but never succeeded since
    assert rules.backup_overdue(old, False, now) is False     # backups never set up: stay quiet


def test_an_overdue_backup_reaches_the_owner_once_a_day(monkeypatch):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest())
    db.set_setting("backup_enabled", "1")
    db.set_setting("last_backup_at", "2026-10-17T00:00:00+00:00")
    db.set_setting("last_backup_error", "upload refused: HTTP 403 (the backup link has probably expired)")
    prompts = []

    async def wake(prompt):
        prompts.append(prompt)

    asyncio.run(rules.tick(wake, now=_late(12, 0)))
    asyncio.run(rules.tick(wake, now=_late(12, 5)))
    alerts = [p for p in prompts if "nightly backup" in p]
    assert len(alerts) == 1 and "HTTP 403" in alerts[0]


def test_no_backup_alert_before_backups_were_ever_set_up(monkeypatch):
    monkeypatch.setattr(rules, "_whoop", lambda: FakeWhoopLatest())
    prompts = []

    async def wake(prompt):
        prompts.append(prompt)

    asyncio.run(rules.tick(wake, now=_late(12, 0)))
    assert not [p for p in prompts if "nightly backup" in p]
