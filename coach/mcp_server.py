"""WHOOP MCP server: exposes 9 tools over stdio.

Runs as a child process (launched by agent.py, or directly by Claude
Desktop for manual testing). No network port — MCP is stdio only.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer

from coach import db
from coach.whoop import WhoopClient

mcp = MCPServer("whoop-coach")

_client: WhoopClient | None = None


def _whoop() -> WhoopClient:
    global _client
    if _client is None:
        _client = WhoopClient()
    return _client


def _local_tz():
    """The user's timezone, as a real IANA zone so DST is right per date.

    A fixed offset captured from "now" would mislabel dates in the other half
    of the year (a December event rendering as BST rather than GMT).
    """
    return db.resolve_timezone()


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')}"


def _to_display(at: str) -> str:
    """Stored UTC -> '00:05 (BST) on September 18th, 2026', quoted verbatim.

    Converts straight from the ZoneInfo rather than reparsing _to_local_iso's
    output — an ISO string carries only a numeric offset, so %Z would render
    "UTC+01:00" instead of the zone's name.
    """
    parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(_local_tz())
    return f"{local:%H:%M} ({local:%Z}) on {local:%B} {_ordinal(local.day)}, {local:%Y}"


def _to_utc_iso(at: str | None) -> str | None:
    """Local wall-clock (no suffix) -> UTC for storage. None means 'now'."""
    if at is None:
        return None
    parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_tz())
    return parsed.astimezone(timezone.utc).isoformat()


def _to_local_iso(at: str) -> str:
    """Stored UTC -> the user's local wall-clock, for anything he reads."""
    parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_local_tz()).isoformat()


def _local_date_of(stamp: str | None) -> str | None:
    """The local calendar date a record's own timestamp falls on.

    Snapshots must be keyed by this, never by "today" in UTC or on the
    machine's clock — a wake-to-wake recovery/sleep record can come back
    from a lookup for either of two calendar days, and daily_snapshots is
    keyed by date, so filing it under the wrong one would silently corrupt
    every 7-day average the alerts (and rules.py) read from.
    """
    if not stamp:
        return None
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_local_tz()).date().isoformat()


def _ms_to_hours(milli: float) -> float:
    return round(milli / 3_600_000, 2)


def _ms_to_minutes(milli: float) -> int:
    return round(milli / 60_000)


def _now() -> datetime:
    """The current moment in the owner's timezone (a seam for tests)."""
    return datetime.now(_local_tz())


def _reference_instant(date: str, now: datetime | None = None):
    """The moment that identifies which WHOOP cycle "that day" is: local noon.

    A cycle runs from one sleep onset to the next, so the cycle containing a
    day's midday is the one whose recovery was scored that morning. For today
    (or a date whose noon hasn't happened yet) the instant is now. Returns
    (instant, None), or (None, reason) for a bad or future date.
    """
    tz = _local_tz()
    now = (now or _now()).astimezone(tz)
    if date == "today":
        return now, None
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return None, "date must be 'today' or YYYY-MM-DD"
    if day > now.date():
        return None, "that date is in the future"
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
    return min(noon, now), None


def _cycle_for(date: str):
    """(cycle, None) for a date, or (None, the not-found result to return)."""
    instant, error = _reference_instant(date)
    if error:
        return None, {"found": False, "date": date, "error": error}
    cycle = _whoop().cycle_containing(instant)
    if cycle is None:
        return None, {"found": False, "date": date}
    return cycle, None


@mcp.tool()
def get_recovery(date: str = "today") -> dict:
    """Recovery score, HRV and resting heart rate for a date (default today).

    `date` is a local calendar date (YYYY-MM-DD) or "today". It resolves to the
    WHOOP cycle containing that day's midday. The result carries `cycle_id`:
    two dates that return the same cycle_id are the same record.
    """
    cycle, miss = _cycle_for(date)
    if miss:
        return miss
    record = _whoop().recovery_for_cycle(cycle["id"])
    if record is None:
        return {
            "found": False,
            "date": date,
            "cycle_id": cycle["id"],
            "note": "WHOOP has a cycle for that day but no scored recovery yet",
        }
    score = record.get("score", {})
    result = {
        "found": True,
        "cycle_id": cycle["id"],
        "recovery_score": score.get("recovery_score"),
        "hrv_ms": score.get("hrv_rmssd_milli"),
        "resting_heart_rate": score.get("resting_heart_rate"),
        "timestamp": record.get("created_at"),
    }
    snapshot_date = _local_date_of(record.get("created_at"))
    if snapshot_date:
        db.upsert_snapshot(
            snapshot_date,
            recovery=result["recovery_score"],
            hrv=result["hrv_ms"],
            rhr=result["resting_heart_rate"],
        )
    return result


@mcp.tool()
def get_sleep(date: str = "today") -> dict:
    """Sleep performance, hours asleep, stage breakdown and efficiency for a date.

    `date` is a local calendar date (YYYY-MM-DD) or "today"; it resolves to the
    main sleep of the WHOOP cycle containing that day's midday (the night that
    ended that morning). The result carries `cycle_id`.
    """
    cycle, miss = _cycle_for(date)
    if miss:
        return miss
    record = _whoop().sleep_for_cycle(cycle["id"])
    if record is None:
        return {"found": False, "date": date, "cycle_id": cycle["id"]}
    score = record.get("score", {})
    stages = score.get("stage_summary", {})
    hours_asleep = _ms_to_hours(
        stages.get("total_light_sleep_time_milli", 0)
        + stages.get("total_slow_wave_sleep_time_milli", 0)
        + stages.get("total_rem_sleep_time_milli", 0)
    )
    result = {
        "found": True,
        "cycle_id": cycle["id"],
        "performance_pct": score.get("sleep_performance_percentage"),
        "hours_asleep": hours_asleep,
        "efficiency_pct": score.get("sleep_efficiency_percentage"),
        "light_hours": _ms_to_hours(stages.get("total_light_sleep_time_milli", 0)),
        "deep_hours": _ms_to_hours(stages.get("total_slow_wave_sleep_time_milli", 0)),
        "rem_hours": _ms_to_hours(stages.get("total_rem_sleep_time_milli", 0)),
        "start": record.get("start"),
        "end": record.get("end"),
    }
    snapshot_date = _local_date_of(record.get("end"))
    if snapshot_date:
        db.upsert_snapshot(
            snapshot_date,
            sleep_perf=result["performance_pct"],
            sleep_hours=result["hours_asleep"],
        )
    return result


@mcp.tool()
def get_strain(date: str = "today") -> dict:
    """Day strain, average heart rate and workout count for a date.

    `date` is a local calendar date (YYYY-MM-DD) or "today"; it resolves to the
    WHOOP cycle containing that day's midday. `in_progress` is true while the
    day is still running (strain keeps rising). The result carries `cycle_id`.
    """
    cycle, miss = _cycle_for(date)
    if miss:
        return miss
    in_progress = cycle.get("end") is None
    until = cycle.get("end") or _now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    workouts = _whoop().workouts_between(cycle["start"], until)
    score = cycle.get("score", {})
    result = {
        "found": True,
        "cycle_id": cycle["id"],
        "in_progress": in_progress,
        "day_strain": score.get("strain"),
        "average_heart_rate": score.get("average_heart_rate"),
        "workouts_count": len(workouts),
    }
    if in_progress:
        # Only the running cycle is "today's strain"; a finished one is history.
        today_local = _now().astimezone(_local_tz()).date().isoformat()
        db.upsert_snapshot(today_local, strain=result["day_strain"])
    return result


@mcp.tool()
def get_sleep_need() -> dict:
    """Tonight's sleep need in minutes, broken down by baseline/debt/strain/nap."""
    record = _whoop().latest_sleep()
    if record is None:
        return {"found": False}
    need = record.get("score", {}).get("sleep_needed", {})
    baseline = need.get("baseline_milli", 0)
    debt = need.get("need_from_sleep_debt_milli", 0)
    strain = need.get("need_from_recent_strain_milli", 0)
    nap = need.get("need_from_recent_nap_milli", 0)
    return {
        "found": True,
        "baseline_minutes": _ms_to_minutes(baseline),
        "debt_minutes": _ms_to_minutes(debt),
        "strain_minutes": _ms_to_minutes(strain),
        "nap_minutes": _ms_to_minutes(nap),
        "total_minutes": _ms_to_minutes(baseline + debt + strain - nap),
        "based_on_sleep_ending": record.get("end"),
    }


@mcp.tool()
def get_workouts(days: int = 7) -> list[dict]:
    """List of workouts in the last N days: sport, start, duration, strain."""
    records = _whoop().workouts(days)
    out = []
    for w in records:
        start = w.get("start")
        end = w.get("end")
        duration_minutes = None
        if start and end:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            duration_minutes = round((end_dt - start_dt).total_seconds() / 60)
        out.append(
            {
                "sport": w.get("sport_name"),
                "start": start,
                "duration_minutes": duration_minutes,
                "strain": w.get("score", {}).get("strain"),
            }
        )
    return out


@mcp.tool()
def log_event(kind: str, note: str, at: str | None = None) -> dict:
    """Store a logged event. kind must be one of: alcohol, caffeine, food, note, mood, other.

    `at` must be the user's LOCAL wall-clock time with NO timezone suffix
    (e.g. "2026-09-18T19:00:00" for 7pm). Omit it to mean right now. It is
    converted to UTC for storage; quote `at_display` back to him verbatim.
    """
    row = db.insert_event(kind, note, _to_utc_iso(at))
    stored = dict(row)
    stored["at_local"] = _to_local_iso(stored["at"])
    stored["at_display"] = _to_display(stored["at"])
    return stored


@mcp.tool()
def recall_events(kind: str | None = None, days: int = 30) -> list[dict]:
    """Past events, each annotated with the next morning's recovery/sleep performance if known."""
    events = db.get_events(kind=kind, days=days)
    out = []
    for e in events:
        at_local = _to_local_iso(e["at"])
        # The "next morning" is the morning after his local day, not the UTC
        # one — a 00:30 local snack is still the previous UTC day.
        event_date = datetime.fromisoformat(at_local).date()
        next_day = (event_date + timedelta(days=1)).isoformat()
        snap = db.get_snapshot(next_day)
        out.append(
            {
                "kind": e["kind"],
                "note": e["note"],
                "at": at_local,
                "at_display": _to_display(e["at"]),
                "next_morning_recovery": snap["recovery"] if snap else None,
                "next_morning_sleep_perf": snap["sleep_perf"] if snap else None,
            }
        )
    return out


@mcp.tool()
def get_trends(days: int = 14) -> list[dict]:
    """Daily recovery/HRV/sleep/strain rows for the last N days."""
    rows = db.get_snapshots(days)
    return [
        {
            "date": r["date"],
            "recovery": r["recovery"],
            "hrv": r["hrv"],
            "sleep_perf": r["sleep_perf"],
            "strain": r["strain"],
        }
        for r in rows
    ]


@mcp.tool()
def set_timezone(tz_name: str) -> dict:
    """Set or clear the manual timezone override.

    Call it when he says he is somewhere else ("I'm in Tokyo this week"): pass
    the IANA name for that place, e.g. "Asia/Tokyo" or "Europe/London". The
    override stays until he says he is back, then pass "auto" to go back to
    following his Slack profile. Nudge times, quiet hours, the check-in and
    every time he reads follow it immediately.
    """
    name = (tz_name or "").strip()
    if name.lower() in ("", "auto", "clear", "reset", "none"):
        db.clear_setting("tz_override")
        return {
            "ok": True,
            "following": "his Slack profile",
            "timezone": str(db.resolve_timezone()),
            "local_time_now": _to_display(datetime.now(timezone.utc).isoformat()),
        }
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return {
            "ok": False,
            "error": f"{name!r} is not a timezone name. Use an IANA name such as "
            "Asia/Tokyo, Europe/London or America/Toronto.",
        }
    db.set_setting("tz_override", name)
    return {
        "ok": True,
        "following": "a manual override, until he says he is back",
        "timezone": name,
        "local_time_now": _to_display(datetime.now(timezone.utc).isoformat()),
    }


def main() -> None:
    db.init_db()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
