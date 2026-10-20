"""WHOOP API v2 client with automatic access-token refresh.

All WHOOP-specific details (endpoints, field names, token refresh) live here
and in mcp_server.py, per the refactor-cost rule.
"""

import argparse
import fcntl
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values, load_dotenv

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
LOCK_PATH = ROOT / ".whoop_token.lock"
ACCESS_CACHE_PATH = ROOT / ".whoop_access.json"

TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
BASE_URL = "https://api.prod.whoop.com/developer/v2"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _update_env(key: str, value: str) -> None:
    """Rewrite one line of .env atomically (temp file + rename), so a process
    reading .env at the same moment never sees a half-written file."""
    lines = ENV_PATH.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    fd, tmp = tempfile.mkstemp(dir=ENV_PATH.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_shared_access() -> tuple[str | None, float]:
    try:
        data = json.loads(ACCESS_CACHE_PATH.read_text())
        return data["access_token"], float(data["expires_at"])
    except (OSError, ValueError, KeyError):
        return None, 0.0


def _save_shared_access(token: str, expires_at: float) -> None:
    _atomic_write(ACCESS_CACHE_PATH, json.dumps({"access_token": token, "expires_at": expires_at}))


class WhoopClient:
    """Thin wrapper around the WHOOP v2 API. Refreshes its own access token.

    Two facts about WHOOP shape this class:
      1. A refresh token is single-use: using it retires it and returns a new
         one. So it is never cached in memory or os.environ — every refresh
         re-reads it from .env under a file lock and writes the replacement
         straight back.
      2. Refreshing also invalidates the previous *access* token. The Slack
         bot, its MCP child and Claude Desktop's MCP server are separate
         processes, so each holding a private access token meant one process's
         refresh silently killed the others' (401s for up to an hour). The
         current access token therefore lives in a shared file too, and any
         401 triggers one "adopt the fresh shared token, else refresh" retry.
    """

    def __init__(self):
        load_dotenv(ENV_PATH)
        self.client_id = os.environ["WHOOP_CLIENT_ID"]
        self.client_secret = os.environ["WHOOP_CLIENT_SECRET"]
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def _refresh_locked(self) -> None:
        """Exchange the refresh token for new tokens. Caller holds the lock."""
        refresh_token = dotenv_values(ENV_PATH).get("WHOOP_REFRESH_TOKEN")
        if not refresh_token:
            raise RuntimeError("WHOOP_REFRESH_TOKEN missing from .env")
        resp = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            _update_env("WHOOP_REFRESH_TOKEN", new_refresh)
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60
        _save_shared_access(self._access_token, self._expires_at)
        log.info("WHOOP token refreshed (refresh token rotated)")

    def _access_token_value(self, rejected: str | None = None) -> str:
        """A usable access token. `rejected` is one WHOOP just answered 401 to:
        never hand it back."""
        if (
            self._access_token
            and time.time() < self._expires_at
            and self._access_token != rejected
        ):
            return self._access_token
        with open(LOCK_PATH, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                cached, expires_at = _load_shared_access()
                if cached and time.time() < expires_at and cached != rejected:
                    # Another process already refreshed; adopt its token rather
                    # than refreshing again and invalidating it.
                    self._access_token, self._expires_at = cached, expires_at
                else:
                    self._refresh_locked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return self._access_token

    def _get(self, path: str, params: dict | None = None) -> dict:
        token = self._access_token_value()
        resp = httpx.get(
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if resp.status_code == 401:
            log.warning("WHOOP answered 401; retrying once with a fresh access token")
            token = self._access_token_value(rejected=token)
            resp = httpx.get(
                f"{BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        resp.raise_for_status()
        return resp.json()

    def _get_collection(self, path: str, params: dict) -> list[dict]:
        # A single page (limit=25) comfortably covers one day or one week for
        # a single user, so we don't chase `next_token` — WHOOP returns one
        # even on a fully-satisfied `limit=1` request, and paginating anyway
        # doubles the calls for no benefit.
        data = self._get(path, params=params)
        return data.get("records", [])

    def latest_recovery(self) -> dict | None:
        records = self._get_collection("/recovery", {"limit": 1})
        return records[0] if records else None

    def latest_cycle(self) -> dict | None:
        records = self._get_collection("/cycle", {"limit": 1})
        return records[0] if records else None

    def latest_sleep(self) -> dict | None:
        records = self._get_collection("/activity/sleep", {"limit": 1})
        return records[0] if records else None

    def cycle_containing(self, instant: datetime) -> dict | None:
        """The cycle whose [start, end) contains `instant`, or None.

        A WHOOP cycle runs from one sleep onset to the next, so "the cycle a
        day belongs to" is the one containing that day's midday. Looking a
        cycle up by instant, rather than by a UTC date window, is correct in
        any timezone; a date window goes empty around sleep onset and can miss
        the morning's cycle entirely for zones far from UTC. A cycle that has
        not ended yet has end=None.
        """
        records = self._get_collection(
            "/cycle",
            {
                "start": _iso_z(instant - timedelta(days=4)),
                "end": _iso_z(instant + timedelta(days=1)),
                "limit": 25,
            },
        )
        for cycle in records:
            start = _parse(cycle["start"])
            end = _parse(cycle["end"]) if cycle.get("end") else None
            if start <= instant and (end is None or instant < end):
                return cycle
        return None

    def _for_cycle(self, cycle_id, what: str) -> dict | None:
        try:
            return self._get(f"/cycle/{cycle_id}/{what}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:   # not scored yet / none recorded
                return None
            raise

    def recovery_for_cycle(self, cycle_id) -> dict | None:
        return self._for_cycle(cycle_id, "recovery")

    def sleep_for_cycle(self, cycle_id) -> dict | None:
        return self._for_cycle(cycle_id, "sleep")

    def workouts_between(self, start: str, end: str) -> list[dict]:
        return self._get_collection(
            "/activity/workout", {"start": start, "end": end, "limit": 25}
        )

    def workouts(self, days: int = 7) -> list[dict]:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        return self._get_collection(
            "/activity/workout",
            {"start": _iso_z(start_dt), "end": _iso_z(end_dt), "limit": 25},
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="print the latest recovery and exit")
    args = parser.parse_args()

    if not args.smoke:
        parser.print_help()
        return 1

    client = WhoopClient()
    record = client.latest_recovery()
    if record is None:
        print("No recovery records found on this account yet.")
        return 1

    score = record.get("score", {})
    print(
        f"Recovery: {score.get('recovery_score')}% | "
        f"HRV: {score.get('hrv_rmssd_milli')} ms | "
        f"RHR: {score.get('resting_heart_rate')} bpm | "
        f"recorded: {record.get('created_at')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
