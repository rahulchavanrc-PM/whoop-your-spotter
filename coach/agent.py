"""Agent loop: MCP client (child process over stdio) + LiteLLM tool calling.

All LLM details stay behind LiteLLM here, and nothing in this module knows
about Slack — `respond()` returns text and the caller posts it. That keeps
the Slack seam to app.py's `send()` helper (the refactor-cost rule).
"""

import asyncio
import json
import logging
import sys

import litellm
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coach import db
from coach.config import HISTORY_TURNS, MAX_TOOL_ROUNDS, MODEL, ROOT

log = logging.getLogger(__name__)

# Groq's free tier rate-limits quick bursts. A short, growing wait usually
# clears it; after the last retry we raise LLMUnavailable so the caller can
# say so instead of failing silently.
RETRY_DELAYS = (3, 10, 25)
_RETRYABLE = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.APIConnectionError,
    litellm.Timeout,
)


class LLMUnavailable(Exception):
    """The AI provider stayed rate-limited or down through every retry."""

SYSTEM_EVENT_PREFIX = "[SYSTEM EVENT]"

SYSTEM_PROMPT = """You are Whoop-your-Spotter, a personal fitness coach living in a Slack DM. \
You have live access to the user's WHOOP data and to a memory of things he has logged.

How you work:
- Cite his actual numbers. Never invent, estimate, or recall data from memory — \
call a tool and use what it returns.
- If a tool fails, or returns found: false for a date, say so plainly — WHOOP has no \
distinct reading for that day, most likely because the band wasn't worn or synced \
that night. Don't paper over it.
- WHOOP's recovery and cycle dates run wake-to-wake, not calendar days, so a lookup \
for one date can hand back the very same record as another date. If the numbers for \
a date match ones you already gave for a different date in this conversation, say \
that plainly — WHOOP has no separate reading for the day he asked about. Never \
present another day's numbers as if they belong to the day asked about.
- Keep replies short and conversational — a few sentences. This is a DM, not a report.
- Write plain sentences. No markdown bold, headings, or bullet formatting — Slack \
shows the raw characters.
- When he tells you something he did, ate, drank, or felt ("double espresso at 10", \
"three beers last night", "feeling wrecked"), call log_event and confirm in one \
short line. No lecture, no unsolicited advice. \
- One message can hold several things, and you log each as its own event. What he \
did (kind food, caffeine, alcohol, ...) and, separately, how he says he feels or \
felt (kind mood) are two events: "black coffee at 5:15pm, I feel alert and dialed \
in" is two log_event calls, caffeine "black coffee" and mood "alert and dialed in", \
both at 5:15pm. Never drop the mood because the rest of the message is about \
something else, and never invent a mood he didn't state. A mood note keeps his own \
words, plus what it followed if he said so ("alert and dialed in, after black \
coffee"). Moods are what let you learn his patterns, so when he asks how he tends \
to feel after something, use recall_events for kind "mood" and for the related kind.
- Fetch only the data the question actually needs.
- Times he gives you are his local wall-clock time. Pass them to log_event exactly \
as he says them, with no timezone suffix ("7pm today" -> "{today}T19:00:00"); the \
tool converts to UTC itself. Never do timezone arithmetic yourself.
- When you tell him when something was logged, use the tool's `at_display` value \
exactly as given ("00:05 (BST) on September 18th, 2026"). Don't reformat it, \
shorten it, or restate it as a raw timestamp.
- Use recall_events and get_trends when connecting what he logs to how he recovers.
- When you connect something he logged to how he recovered, say how many nights the \
comparison rests on. With one or two nights it is not enough to tell: report what \
those nights showed, say plainly that it isn't enough to tell yet, and do not \
conclude that the thing does or doesn't affect him. Talk about a pattern only when \
several comparable nights back it.
- If he says he is somewhere else or his timezone differs ("I'm in Tokyo this week"), \
call set_timezone with the IANA name for that place (Asia/Tokyo, Europe/London, \
America/Toronto) and tell him the local time it returns. It stays until he says he is \
back; then call set_timezone with "auto". Only do this when he says where he is; never \
convert timezones yourself.
- Date lookups (get_recovery, get_sleep, get_strain) return a cycle_id. If two dates \
come back with the same cycle_id, they are the same record: say so plainly.
- A message starting with "[SYSTEM EVENT]" is an instruction from the scheduler, not \
something he said — he will never see that message himself. Write the reply as an \
unprompted message from you (a brief, a check-in, an alert), not as an answer to a \
question. Never say things like "you asked" or quote the [SYSTEM EVENT] text back.
- Never tell him WHOOP is down, disconnected, or needs re-authorizing unless a tool \
call in THIS reply actually failed, or the [SYSTEM EVENT] itself says access is lost. \
Earlier messages are not evidence: connections get fixed. For a brief, check-in or \
alert, always call the tools first, and use recall_events to see what he logged today, \
before you write anything.

Today is {today}."""


def _to_openai_tool(tool) -> dict:
    """Convert an MCP tool definition into the schema LiteLLM expects."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


class CoachAgent:
    """Owns the MCP child process and runs the LLM tool loop against it."""

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._tool_schemas: list[dict] = []
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._session_task: asyncio.Task | None = None
        self._startup_error: BaseException | None = None

    async def start(self) -> None:
        self._session_task = asyncio.create_task(self._run_session())
        await self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    async def aclose(self) -> None:
        self._stop.set()
        if self._session_task is not None:
            await self._session_task

    async def _run_session(self) -> None:
        """Hold the MCP session open for the process lifetime.

        Entering and exiting the stdio/session context managers must happen in
        the same task, so this one task owns both ends and other tasks only
        ever issue requests against the live session.
        """
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "coach.mcp_server"],
            cwd=str(ROOT),
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tool_schemas = [_to_openai_tool(t) for t in listed.tools]
                    self._session = session
                    log.info(
                        "MCP server ready with %d tools: %s",
                        len(self._tool_schemas),
                        ", ".join(t.name for t in listed.tools),
                    )
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as exc:
            self._startup_error = exc
            raise
        finally:
            self._session = None
            self._ready.set()

    async def _run_tool(self, name: str, raw_arguments: str) -> str:
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return f"ERROR: arguments for {name} were not valid JSON"

        log.info("tool call: %s(%s)", name, arguments)
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:
            log.exception("tool %s raised", name)
            return f"ERROR: tool {name} failed: {exc}"

        text = "\n".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        if result.is_error:
            return f"ERROR: {text or 'tool reported a failure'}"
        return text or "(no data returned)"

    async def _complete(self, messages: list[dict]):
        for delay in (*RETRY_DELAYS, None):
            try:
                return await litellm.acompletion(
                    model=MODEL, messages=messages, tools=self._tool_schemas
                )
            except _RETRYABLE as exc:
                if delay is None:
                    raise LLMUnavailable(type(exc).__name__) from exc
                log.warning("AI provider %s; retrying in %ss", type(exc).__name__, delay)
                await asyncio.sleep(delay)

    async def respond(self, user_text: str, thread_ts: str) -> str:
        """Run the tool loop until the model answers in words. Returns that answer."""
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(today=db.local_today())}
        ]
        # A scheduler-started message starts fresh. Replaying the shared
        # "proactive" thread let an old "WHOOP is down" notice from a real
        # outage steer a healthy check-in into repeating it (2026-10-19). What
        # the coach knows comes from its tools, not from that old text.
        if not user_text.startswith(SYSTEM_EVENT_PREFIX):
            for row in db.get_conversation(thread_ts, limit=HISTORY_TURNS):
                messages.append({"role": row["role"], "content": row["content"]})
        messages.append({"role": "user", "content": user_text})

        answer = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._complete(messages)
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            if not tool_calls:
                answer = (message.content or "").strip()
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": await self._run_tool(
                            call.function.name, call.function.arguments
                        ),
                    }
                )
        else:
            answer = "I kept going in circles looking that up. Mind asking again?"

        if not answer:
            answer = "I came back empty on that one — try rephrasing?"

        db.add_conversation(thread_ts, "user", user_text)
        db.add_conversation(thread_ts, "assistant", answer)
        return answer
