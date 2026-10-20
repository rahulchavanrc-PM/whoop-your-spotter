"""Entry point: Slack Bolt over Socket Mode, wired to the agent loop.

Every Slack detail lives in this module and goes out through one `send()`
helper, so swapping Slack for something else touches nothing else
(the refactor-cost rule). Socket Mode means an outbound websocket —
nothing listens on a port.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from coach import db, rules
from coach.agent import CoachAgent, LLMUnavailable
from coach.config import SLACK_APP_TOKEN, SLACK_BOT_TOKEN, SLACK_USER_ID

log = logging.getLogger(__name__)

app = AsyncApp(token=SLACK_BOT_TOKEN)
agent = CoachAgent()
scheduler = AsyncIOScheduler()

# Proactive messages (briefs, alerts, nudges, check-ins) share one
# conversation history so the agent knows what it already told him today,
# but are posted as new top-level DMs — there's no user message to reply
# in-thread to. A user reply lands as its own new top-level message
# (Slack's default unless he explicitly threads it), which is fine: it
# just starts its own conversation, same as any other DM.
PROACTIVE_THREAD = "proactive"


async def send(channel: str, text: str, thread_ts: str | None = None) -> None:
    """The single place a Slack message gets posted."""
    await app.client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)


@app.event("message")
async def handle_dm(event: dict) -> None:
    # Ignore our own posts, edits/joins/etc., and anything outside a DM.
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return

    text = (event.get("text") or "").strip()
    if not text:
        return

    channel = event["channel"]
    # A top-level DM starts its own thread; replies inside a thread keep that
    # thread's history, which is what scopes the agent's memory.
    thread_ts = event.get("thread_ts") or event["ts"]

    try:
        answer = await agent.respond(text, thread_ts)
    except LLMUnavailable:
        log.warning("AI provider unavailable after retries; told the owner")
        await send(channel, "My AI provider is rate-limiting me or down right now. "
                   "Give it a minute and send that again.", thread_ts)
        return
    except Exception:
        log.exception("agent failed handling message")
        await send(channel, "Something broke on my end there. Try me again?", thread_ts)
        return

    await send(channel, answer, thread_ts)


async def refresh_slack_tz() -> None:
    """Cache the owner's Slack profile timezone into settings.slack_tz.

    db.resolve_timezone() reads this (falling back to it only when no
    manual tz_override is set) — this is the one place that talks to
    Slack's users.info, so rules.py and mcp_server.py never need a Slack
    client of their own. Refreshed every tick, since he can
    change it by updating his Slack profile while traveling.
    """
    try:
        info = await app.client.users_info(user=SLACK_USER_ID)
        tz = info["user"].get("tz")
        if tz:
            db.set_setting("slack_tz", tz)
    except Exception:
        log.exception("could not refresh Slack tz; keeping the last known value")


async def scheduled_tick() -> None:
    await refresh_slack_tz()
    await rules.tick(proactive_wake)


async def proactive_wake(prompt: str) -> None:
    """rules.tick's injected wake() — runs the same agent loop as a real
    message, then DMs the result. `channel=SLACK_USER_ID` opens/uses his DM
    directly; no separate lookup needed."""
    # Failures propagate: rules.tick must not mark a brief/alert/nudge as sent
    # when it was not, or the owner silently never gets it.
    answer = await agent.respond(prompt, thread_ts=PROACTIVE_THREAD)
    await send(SLACK_USER_ID, answer)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_db()
    await agent.start()

    # An immediate check at startup, then every 5 minutes thereafter
    # (the rules spec) — not scheduled as the job's first run,
    # so a job misfire/coalesce setting can't accidentally skip it.
    asyncio.create_task(scheduled_tick())
    scheduler.add_job(scheduled_tick, "interval", minutes=5, id="whoop_tick")
    scheduler.start()

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    log.info("coach is listening on Socket Mode")
    try:
        await handler.start_async()
    finally:
        scheduler.shutdown(wait=False)
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
