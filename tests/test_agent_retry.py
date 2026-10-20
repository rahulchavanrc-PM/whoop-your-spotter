"""The AI provider is rate-limited in bursts on Groq's free tier: retry with
backoff, then say so plainly. A fake provider; no network."""

import asyncio

import litellm
import pytest

from coach import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent_test.db")
    db.init_db()


def make_agent(monkeypatch, outcomes, delays=(0, 0, 0)):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_ID"):
        monkeypatch.setenv(key, "x")
    from coach import agent as agent_mod

    if delays is not None:
        monkeypatch.setattr(agent_mod, "RETRY_DELAYS", delays)
    calls = {"n": 0}

    class Message:
        tool_calls = None
        content = "hi there"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    async def fake(**kwargs):
        outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return Response()

    monkeypatch.setattr(agent_mod.litellm, "acompletion", fake)
    return agent_mod, calls


def rate_limit():
    return litellm.RateLimitError(message="slow down", llm_provider="groq", model="m")


def test_a_rate_limit_is_retried_and_then_succeeds(monkeypatch):
    agent_mod, calls = make_agent(monkeypatch, [rate_limit(), rate_limit(), "ok"])
    answer = asyncio.run(agent_mod.CoachAgent().respond("hello", "t1"))
    assert answer == "hi there" and calls["n"] == 3


def test_it_gives_up_after_the_last_retry_with_a_clear_error(monkeypatch):
    agent_mod, calls = make_agent(monkeypatch, [rate_limit()])
    with pytest.raises(agent_mod.LLMUnavailable):
        asyncio.run(agent_mod.CoachAgent().respond("hello", "t1"))
    assert calls["n"] == 4  # the first try plus three retries


def test_a_failed_attempt_leaves_no_half_saved_conversation(monkeypatch):
    agent_mod, _ = make_agent(monkeypatch, [rate_limit()])
    with pytest.raises(agent_mod.LLMUnavailable):
        asyncio.run(agent_mod.CoachAgent().respond("hello", "t1"))
    assert db.get_conversation("t1") == []


def test_a_real_bug_is_not_retried_or_hidden(monkeypatch):
    agent_mod, calls = make_agent(monkeypatch, [ValueError("bad request")])
    with pytest.raises(ValueError):
        asyncio.run(agent_mod.CoachAgent().respond("hello", "t1"))
    assert calls["n"] == 1


def test_the_waits_grow_between_retries(monkeypatch):
    agent_mod, _ = make_agent(monkeypatch, [rate_limit(), rate_limit(), rate_limit(), "ok"], delays=None)
    waits = []

    async def record(seconds):
        waits.append(seconds)

    monkeypatch.setattr(agent_mod.asyncio, "sleep", record)
    asyncio.run(agent_mod.CoachAgent().respond("hello", "t1"))
    assert waits == [3, 10, 25]
