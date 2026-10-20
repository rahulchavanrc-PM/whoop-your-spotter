"""Pins the logging rules in the AI's instructions. Whether the model *follows*
them is checked against the real model by hand (by hand, 2026-10-19):
this only stops the rule from being edited away unnoticed."""


def test_prompt_requires_moods_to_be_logged_as_their_own_events(monkeypatch):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_ID"):
        monkeypatch.setenv(key, "x")
    from coach import agent

    prompt = agent.SYSTEM_PROMPT
    assert "each as its own event" in prompt
    assert "kind mood" in prompt
    assert "never invent a mood" in prompt


def test_prompt_names_the_coach(monkeypatch):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_ID"):
        monkeypatch.setenv(key, "x")
    from coach import agent

    assert "You are Whoop-your-Spotter" in agent.SYSTEM_PROMPT


def test_prompt_refuses_to_conclude_from_one_or_two_nights(monkeypatch):
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_ID"):
        monkeypatch.setenv(key, "x")
    from coach import agent

    assert "isn't enough to tell yet" in agent.SYSTEM_PROMPT
    assert "several comparable nights" in agent.SYSTEM_PROMPT
