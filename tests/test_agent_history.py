"""A scheduler-started message must not replay the shared 'proactive' thread.

A stale "WHOOP is down" notice from a real outage was once copied into a
healthy check-in. Fake provider; no network."""

import asyncio

import pytest

from coach import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history_test.db")
    db.init_db()


def run(monkeypatch, user_text, thread):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_ID"):
        monkeypatch.setenv(key, "x")
    from coach import agent as agent_mod

    seen = {}

    class Message:
        tool_calls = None
        content = "ok"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    async def fake(**kwargs):
        seen["messages"] = kwargs["messages"]
        return Response()

    monkeypatch.setattr(agent_mod.litellm, "acompletion", fake)
    asyncio.run(agent_mod.CoachAgent().respond(user_text, thread))
    return agent_mod, seen["messages"]


def test_a_scheduler_event_does_not_replay_old_proactive_messages(monkeypatch):
    db.add_conversation("proactive", "assistant", "I've lost access to your WHOOP data")
    _, messages = run(monkeypatch, "[SYSTEM EVENT] Evening check-in.", "proactive")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert not any("lost access" in m["content"] for m in messages)


def test_a_real_dm_still_gets_its_own_thread_history(monkeypatch):
    db.add_conversation("t1", "user", "earlier question")
    _, messages = run(monkeypatch, "follow-up", "t1")
    assert [m["content"] for m in messages[1:]] == ["earlier question", "follow-up"]


def test_the_scheduler_turn_is_still_saved_for_the_record(monkeypatch):
    run(monkeypatch, "[SYSTEM EVENT] Nudge him.", "proactive")
    assert [r["role"] for r in db.get_conversation("proactive")] == ["user", "assistant"]


def test_prompt_forbids_claiming_whoop_is_down_without_a_failed_tool(monkeypatch):
    agent_mod, _ = run(monkeypatch, "hi", "t2")
    prompt = agent_mod.SYSTEM_PROMPT
    assert "unless a tool call in THIS reply actually failed" in prompt
    assert "always call the tools first" in prompt
