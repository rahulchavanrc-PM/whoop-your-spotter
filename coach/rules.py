"""Deterministic decision engine — no LLM, no Slack.

Decides *whether* to wake the agent. Every judgement below is a pure
function of its arguments so tests can drive them without a clock or a
network; `tick()` is the only part that touches the outside world.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from coach import db
from coach.whoop import WhoopClient

log = logging.getLogger(__name__)

QUIET_START_HOUR = 23
QUIET_END_HOUR = 7

NUDGE_SLOTS = [(10, 30), (14, 30), (18, 30)]
CHECKIN_SLOT = (20, 30)

# A slot fires only inside this window after its time, so a laptop that was
# asleep at 10:30 doesn't deliver a stale "anything to log?" at 14:00.
SLOT_GRACE = timedelta(minutes=15)
QUIET_AFTER_EVENT = timedelta(hours=2)

RECOVERY_DROP_RATIO = 0.85
HRV_DROP_RATIO = 0.80
HIGH_STRAIN = 18.0
LOW_RECOVERY = 50

# Below this many prior days there's no trustworthy baseline, so alerts stay
# quiet rather than firing off one or two readings.
MIN_BASELINE_DAYS = 3

# A recovery older than this is never announced as "new". Polling the *latest*
# recovery (no date window) means that after an outage the latest one can be
# days old and still unbriefed; briefing it would be wrong.
BRIEF_MAX_AGE = timedelta(hours=24)

# Consecutive failed polls (5 min apart) before he is told, at most once a day.
WHOOP_FAILS_BEFORE_ALERT = 3


def in_quiet_hours(local_now: datetime) -> bool:
    """23:00–07:00 local. No exceptions in the MVP."""
    return local_now.hour >= QUIET_START_HOUR or local_now.hour < QUIET_END_HOUR


def _slot_start(local_now: datetime, slot: tuple[int, int]) -> datetime:
    return local_now.replace(hour=slot[0], minute=slot[1], second=0, microsecond=0)


def _slot_is_live(local_now: datetime, slot: tuple[int, int], last_fired: datetime | None) -> bool:
    start = _slot_start(local_now, slot)
    if not start <= local_now < start + SLOT_GRACE:
        return False
    return last_fired is None or last_fired < start


def due_nudge(
    local_now: datetime,
    last_event_at: datetime | None,
    last_nudge_at: datetime | None,
) -> bool:
    """An 'anything to log?' nudge, unless he just logged something."""
    if in_quiet_hours(local_now):
        return False
    for slot in NUDGE_SLOTS:
        if not _slot_is_live(local_now, slot, last_nudge_at):
            continue
        if last_event_at and local_now - last_event_at < QUIET_AFTER_EVENT:
            return False
        return True
    return False


def due_checkin(local_now: datetime, last_checkin_at: datetime | None) -> bool:
    if in_quiet_hours(local_now):
        return False
    return _slot_is_live(local_now, CHECKIN_SLOT, last_checkin_at)


def due_brief(
    recovery: dict | None,
    last_brief_cycle_id: str | None,
    now: datetime | None = None,
) -> bool:
    """Fire once per genuinely new, recent recovery.

    Keyed on the cycle's own id rather than on "is there a record for
    today", because WHOOP cycles run wake-to-wake: a cycle spanning two
    calendar days answers a date-filtered lookup on both of them, which
    would otherwise brief the same recovery twice. When `now` is given, a
    recovery older than BRIEF_MAX_AGE is treated as stale and not briefed.
    """
    if not recovery:
        return False
    cycle_id = recovery.get("cycle_id")
    if cycle_id is None:
        return False
    if now is not None:
        created = _parse(recovery.get("created_at"))
        if created and now - created > BRIEF_MAX_AGE:
            return False
    return str(cycle_id) != last_brief_cycle_id


BACKUP_MAX_AGE = timedelta(hours=36)


def backup_overdue(last_backup_at: datetime | None, enabled: bool, now: datetime) -> bool:
    """True if nightly backups are set up but none has succeeded recently.

    `enabled` flips on after the first successful backup, so a coach that has
    never been backed up (e.g. before setup) stays quiet, while a backup that
    later breaks (expired link, full bucket) reaches the owner within ~36 h.
    """
    if not enabled:
        return False
    return last_backup_at is None or now - last_backup_at > BACKUP_MAX_AGE


def alerts_to_fire(today: dict, prior: list[dict], already_fired: set[str]) -> list[str]:
    """Threshold alerts, at most one of each kind per day."""
    fired = []

    recoveries = [row["recovery"] for row in prior if row["recovery"] is not None]
    hrvs = [row["hrv"] for row in prior if row["hrv"] is not None]
    recovery = today.get("recovery")
    hrv = today.get("hrv")
    strain = today.get("strain")

    if (
        "recovery" not in already_fired
        and recovery is not None
        and len(recoveries) >= MIN_BASELINE_DAYS
        and recovery < RECOVERY_DROP_RATIO * (sum(recoveries) / len(recoveries))
    ):
        fired.append("recovery")

    if (
        "hrv" not in already_fired
        and hrv is not None
        and len(hrvs) >= MIN_BASELINE_DAYS
        and hrv < HRV_DROP_RATIO * (sum(hrvs) / len(hrvs))
    ):
        fired.append("hrv")

    if (
        "strain" not in already_fired
        and strain is not None
        and recovery is not None
        and strain > HIGH_STRAIN
        and recovery < LOW_RECOVERY
    ):
        fired.append("strain")

    return fired


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _local_date_of(stamp: str | None, tz) -> str | None:
    parsed = _parse(stamp)
    return parsed.astimezone(tz).date().isoformat() if parsed else None


_client: WhoopClient | None = None


def _whoop() -> WhoopClient:
    """One client for the process's lifetime: its access token is good for
    about an hour, so a fresh client every tick meant a needless refresh (and
    key rotation) every 5 minutes."""
    global _client
    if _client is None:
        _client = WhoopClient()
    return _client


def poll_and_snapshot(tz, now: datetime | None = None) -> dict | None:
    """Fetch the *latest* recovery/sleep/cycle and upsert the snapshot rows.

    Latest-record lookups, not date-window ones, on purpose. WHOOP keys a cycle
    by when sleep began, which lines up with no fixed midnight, so any "today"
    window (UTC or local) goes empty between the day rolling over and the next
    sleep onset — from 20:00 Eastern for a UTC "today", and for zones far east
    of UTC the window can miss the morning's cycle entirely. "Latest" has no
    such gap. Each figure is still filed under the local date *its own record*
    belongs to, so a stale latest record can never be filed under today, and
    `daily_snapshots` (keyed by date) stays clean for the alert averages.
    """
    client = _whoop()
    recovery = client.latest_recovery()
    sleep = client.latest_sleep()
    cycle = client.latest_cycle()

    if recovery:
        score = recovery.get("score", {})
        date = _local_date_of(recovery.get("created_at"), tz)
        if date:
            db.upsert_snapshot(
                date,
                recovery=score.get("recovery_score"),
                hrv=score.get("hrv_rmssd_milli"),
                rhr=score.get("resting_heart_rate"),
            )

    if sleep:
        score = sleep.get("score", {})
        stages = score.get("stage_summary", {})
        date = _local_date_of(sleep.get("end"), tz)
        asleep_milli = (
            stages.get("total_light_sleep_time_milli", 0)
            + stages.get("total_slow_wave_sleep_time_milli", 0)
            + stages.get("total_rem_sleep_time_milli", 0)
        )
        if date:
            db.upsert_snapshot(
                date,
                sleep_perf=score.get("sleep_performance_percentage"),
                sleep_hours=round(asleep_milli / 3_600_000, 2),
            )

    # Only an in-progress cycle (no end yet) is "today's strain"; a finished
    # one is history and must not be filed under today.
    if cycle and cycle.get("end") is None:
        today = (now.astimezone(tz) if now else datetime.now(tz)).date().isoformat()
        db.upsert_snapshot(today, strain=cycle.get("score", {}).get("strain"))

    return recovery


async def tick(wake, now: datetime | None = None) -> None:
    """One scheduler beat: refresh data, decide what (if anything) to say, and
    log exactly one line saying what happened.

    The line is what makes an unattended run auditable — a healthy tick used to
    log nothing, so "did it poll while I was away?" had to be pieced together
    from side effects. `grep "coach.rules: tick"` now counts beats directly.

    `wake(prompt)` is injected by app.py so this module stays free of Slack
    and of the agent itself. `now` is an injection seam for tests; the
    scheduler never passes it.
    """
    started = time.monotonic()
    poll, decision, local_now = await _tick(wake, now)
    log.info(
        "%s: poll=%s decision=%s local=%s took=%.1fs",
        "tick ok" if poll == "ok" else "tick DEGRADED",
        poll,
        decision,
        f"{local_now:%Y-%m-%d %H:%M %Z}",
        time.monotonic() - started,
    )


async def _deliver(wake, prompt: str) -> bool:
    """Send a proactive message; False if it failed.

    Callers record a brief/alert/nudge as done ONLY on True. Swallowing the
    failure (the old behaviour) marked it sent when it was not, so a rate-limited
    morning brief was lost for good. A failed one is simply still due on the
    next tick.
    """
    try:
        await wake(prompt)
        return True
    except Exception:
        log.exception("proactive message failed; it stays due and will be retried")
        return False


async def _tick(wake, now: datetime | None) -> tuple[str, str, datetime]:
    tz = db.resolve_timezone()
    local_now = now or datetime.now(tz)
    today_key = local_now.date().isoformat()

    recovery = None
    poll = "ok"
    try:
        recovery = poll_and_snapshot(tz, now=local_now)
        db.set_setting("whoop_fail_count", "0")
    except Exception:
        fails = int(db.get_setting("whoop_fail_count") or 0) + 1
        db.set_setting("whoop_fail_count", str(fails))
        poll = f"FAILED({fails} in a row)"
        log.exception("whoop poll failed (%d in a row)", fails)

    if in_quiet_hours(local_now):
        return poll, "quiet-hours", local_now

    if poll != "ok":
        # Nudges and the check-in don't need WHOOP, so an outage must not
        # silence them. But a WHOOP outage must never be silent to him either.
        if fails >= WHOOP_FAILS_BEFORE_ALERT and db.get_setting("last_whoop_alert") != today_key:
            sent = await _deliver(
                wake,
                "[SYSTEM EVENT] Your WHOOP connection has been failing for a while, so "
                "briefs and alerts are paused. Tell him plainly, in one or two sentences, "
                "that you've lost access to his WHOOP data and he may need to re-authorize.",
            )
            if sent:
                db.set_setting("last_whoop_alert", today_key)
            return poll, "whoop-outage-notice" if sent else "whoop-outage-notice-FAILED", local_now
    else:
        if due_brief(recovery, db.get_setting("last_brief_cycle_id"), now=local_now):
            sent = await _deliver(
                wake, "[SYSTEM EVENT] New recovery available. Write the morning brief: fetch today's "
                "recovery and sleep, and what he has logged recently, first."
            )
            if sent:
                db.set_setting("last_brief_cycle_id", str(recovery["cycle_id"]))
            return poll, "brief" if sent else "brief-FAILED", local_now

        today = db.get_snapshot(today_key)
        if today:
            prior = [dict(row) for row in db.get_snapshots(days=8) if row["date"] != today_key]
            already = {
                kind
                for kind in ("recovery", "hrv", "strain")
                if db.get_setting(f"last_alert_{kind}") == today_key
            }
            for kind in alerts_to_fire(dict(today), prior, already):
                sent = await _deliver(
                    wake,
                    f"[SYSTEM EVENT] {kind} threshold crossed versus his 7-day average. "
                    "Flag it in one or two sentences, citing the numbers.",
                )
                if sent:
                    db.set_setting(f"last_alert_{kind}", today_key)
                return poll, f"alert:{kind}" if sent else f"alert:{kind}-FAILED", local_now

    if backup_overdue(
        _parse(db.get_setting("last_backup_at")),
        db.get_setting("backup_enabled") == "1",
        local_now,
    ) and db.get_setting("last_backup_alert") != today_key:
        reason = (db.get_setting("last_backup_error") or "no error recorded")[:120]
        sent = await _deliver(
            wake,
            "[SYSTEM EVENT] The nightly backup of his memory (coach.db) has not succeeded "
            f"for over 36 hours. Last error: {reason}. Tell him plainly, in one or two "
            "sentences; the likely cause is an expired backup link that needs replacing.",
        )
        if sent:
            db.set_setting("last_backup_alert", today_key)
        return poll, "backup-alert" if sent else "backup-alert-FAILED", local_now

    if due_checkin(local_now, _parse(db.get_setting("last_checkin_at"))):
        sent = await _deliver(
            wake,
            "[SYSTEM EVENT] Evening check-in. Fetch tonight's sleep need, today's strain "
            "and what he logged today, then give tonight's sleep target, today's strain, "
            "and what it means for tomorrow.",
        )
        if sent:
            db.set_setting("last_checkin_at", datetime.now(timezone.utc).isoformat())
        return poll, "checkin" if sent else "checkin-FAILED", local_now

    events = db.get_events(days=1)
    last_event_at = _parse(events[0]["at"]) if events else None
    if due_nudge(local_now, last_event_at, _parse(db.get_setting("last_nudge_at"))):
        sent = await _deliver(
            wake, "[SYSTEM EVENT] Nudge him: anything to log? Keep it to one line."
        )
        if sent:
            db.set_setting("last_nudge_at", datetime.now(timezone.utc).isoformat())
        return poll, "nudge" if sent else "nudge-FAILED", local_now

    return poll, "none", local_now
