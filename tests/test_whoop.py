"""WHOOP token handling, with a fake WHOOP: no network, temp files only."""

import time

import pytest

from coach import whoop


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeWhoop:
    """Rotates tokens like the real service: each refresh retires the old
    refresh token AND the old access token."""

    def __init__(self):
        self.valid_access = None
        self.valid_refresh = "refresh-0"
        self.refresh_calls = 0
        self.refresh_tokens_seen = []
        self.data_calls = 0

    def post(self, url, data=None, **_):
        self.refresh_tokens_seen.append(data["refresh_token"])
        if data["refresh_token"] != self.valid_refresh:
            return FakeResp(400)
        self.refresh_calls += 1
        self.valid_refresh = f"refresh-{self.refresh_calls}"
        self.valid_access = f"access-{self.refresh_calls}"
        return FakeResp(
            200,
            {
                "access_token": self.valid_access,
                "refresh_token": self.valid_refresh,
                "expires_in": 3600,
            },
        )

    def get(self, url, headers=None, **_):
        self.data_calls += 1
        if headers["Authorization"] != f"Bearer {self.valid_access}":
            return FakeResp(401)
        return FakeResp(200, {"records": [{"ok": True}]})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("WHOOP_CLIENT_ID=id\nWHOOP_CLIENT_SECRET=secret\nWHOOP_REFRESH_TOKEN=refresh-0\n")
    monkeypatch.setattr(whoop, "ENV_PATH", env)
    monkeypatch.setattr(whoop, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(whoop, "ACCESS_CACHE_PATH", tmp_path / ".access.json")
    monkeypatch.setenv("WHOOP_CLIENT_ID", "id")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "secret")
    service = FakeWhoop()
    monkeypatch.setattr(whoop.httpx, "post", service.post)
    monkeypatch.setattr(whoop.httpx, "get", service.get)
    return service


def refresh_token_on_disk():
    return whoop.dotenv_values(whoop.ENV_PATH)["WHOOP_REFRESH_TOKEN"]


def test_a_refresh_saves_the_replacement_refresh_token(fake):
    whoop.WhoopClient()._get("/recovery")
    assert fake.refresh_calls == 1
    assert refresh_token_on_disk() == "refresh-1"


def test_a_new_client_each_tick_keeps_working(fake):
    """The original bug: every tick built a fresh client, each refreshing. The
    second one used a stale token and got a 400."""
    for _ in range(4):
        client = whoop.WhoopClient()
        client._expires_at = 0.0  # force a refresh path each time
        whoop._save_shared_access("expired", 0.0)
        client._get("/recovery")
    assert fake.refresh_calls == 4
    assert fake.refresh_tokens_seen == ["refresh-0", "refresh-1", "refresh-2", "refresh-3"]


def test_second_client_adopts_the_shared_access_token(fake):
    whoop.WhoopClient()._get("/recovery")
    whoop.WhoopClient()._get("/recovery")
    assert fake.refresh_calls == 1  # the second one reused the shared token


def test_401_from_another_process_refreshing_is_recovered_without_a_second_refresh(fake):
    """Another process refreshed, retiring our in-memory access token. We
    should adopt its fresh token from the shared file, not refresh again."""
    mine = whoop.WhoopClient()
    mine._get("/recovery")  # mine holds access-1
    assert fake.refresh_calls == 1

    other = whoop.WhoopClient()
    other._access_token, other._expires_at = None, 0.0
    whoop._save_shared_access("expired", 0.0)  # force `other` to refresh
    other._get("/recovery")  # retires access-1, now access-2 is shared
    assert fake.refresh_calls == 2

    assert mine._get("/recovery") == {"records": [{"ok": True}]}
    assert fake.refresh_calls == 2  # adopted access-2; did not refresh a third time


def test_401_with_no_fresher_token_forces_a_refresh(fake):
    client = whoop.WhoopClient()
    client._get("/recovery")
    fake.valid_access = "revoked-elsewhere"  # our token is dead, nobody shared a new one
    fake.valid_refresh = whoop.dotenv_values(whoop.ENV_PATH)["WHOOP_REFRESH_TOKEN"]
    assert client._get("/recovery") == {"records": [{"ok": True}]}
    assert fake.refresh_calls == 2


def test_a_second_401_is_reported_not_looped(fake, monkeypatch):
    client = whoop.WhoopClient()
    client._get("/recovery")
    fake.valid_access = "nothing-will-match"
    monkeypatch.setattr(
        whoop.httpx,
        "post",
        lambda *a, **k: FakeResp(
            200, {"access_token": "still-wrong", "refresh_token": "refresh-9", "expires_in": 3600}
        ),
    )
    with pytest.raises(RuntimeError):
        client._get("/recovery")


def test_an_unexpired_shared_token_is_used_without_any_refresh(fake):
    fake.valid_access = "shared"
    whoop._save_shared_access("shared", time.time() + 1000)
    assert whoop.WhoopClient()._get("/recovery") == {"records": [{"ok": True}]}
    assert fake.refresh_calls == 0


# --- cycle lookups ---------------------------------------------------------------


def _client_with(monkeypatch, records):
    client = whoop.WhoopClient.__new__(whoop.WhoopClient)
    monkeypatch.setattr(client, "_get_collection", lambda path, params: records, raising=False)
    return client


def _t(stamp):
    return whoop._parse(stamp)


def test_cycle_containing_picks_the_cycle_that_holds_the_instant(monkeypatch):
    records = [
        {"id": 2, "start": "2026-10-19T03:44:00.000Z", "end": None},
        {"id": 1, "start": "2026-10-18T05:08:00.000Z", "end": "2026-10-19T03:44:00.000Z"},
    ]
    client = _client_with(monkeypatch, records)
    assert client.cycle_containing(_t("2026-10-18T16:00:00Z"))["id"] == 1
    assert client.cycle_containing(_t("2026-10-19T16:00:00Z"))["id"] == 2


def test_a_cycles_start_belongs_to_it_and_its_end_to_the_next(monkeypatch):
    records = [
        {"id": 2, "start": "2026-10-19T03:44:00.000Z", "end": None},
        {"id": 1, "start": "2026-10-18T05:08:00.000Z", "end": "2026-10-19T03:44:00.000Z"},
    ]
    client = _client_with(monkeypatch, records)
    assert client.cycle_containing(_t("2026-10-19T03:44:00Z"))["id"] == 2
    assert client.cycle_containing(_t("2026-10-18T05:08:00Z"))["id"] == 1


def test_no_cycle_holds_an_instant_in_a_gap(monkeypatch):
    records = [{"id": 1, "start": "2026-10-18T05:08:00.000Z", "end": "2026-10-18T20:00:00.000Z"}]
    assert _client_with(monkeypatch, records).cycle_containing(_t("2026-10-18T22:00:00Z")) is None


def test_a_404_for_a_cycles_recovery_means_not_scored_yet(monkeypatch):
    import httpx

    client = whoop.WhoopClient.__new__(whoop.WhoopClient)

    def missing(path, params=None):
        raise httpx.HTTPStatusError("nope", request=httpx.Request("GET", "http://x"), response=httpx.Response(404))

    monkeypatch.setattr(client, "_get", missing, raising=False)
    assert client.recovery_for_cycle(7) is None


def test_other_errors_for_a_cycle_lookup_are_not_swallowed(monkeypatch):
    import httpx

    client = whoop.WhoopClient.__new__(whoop.WhoopClient)

    def boom(path, params=None):
        raise httpx.HTTPStatusError("bad", request=httpx.Request("GET", "http://x"), response=httpx.Response(500))

    monkeypatch.setattr(client, "_get", boom, raising=False)
    with pytest.raises(httpx.HTTPStatusError):
        client.sleep_for_cycle(7)
