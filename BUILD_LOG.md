# Build log: what broke, and what it taught

A curated log of the incidents and decisions that shaped Whoop-your-Spotter. It was built with Claude Code over several weeks, one phase at a time (setup, WHOOP data and MCP server, agent loop and Slack, rules engine, deployment, verification), with a human owning the design and every call that cost money or touched an account. The interesting part is not that it worked first time; it did not. These are the failures worth reading.

The constraint behind everything: **$0 per month, one small app, nothing listening on a port.**

* * *

## 1. The login that kept expiring (twice)

**Symptom.** The coach went silent. Later, it told its owner "I've lost access to your WHOOP data" while the band was fine.

**Cause, part one.** WHOOP's refresh token is single-use: the moment it is used, WHOOP retires it and hands back a new one. The bot read the token once at startup and kept it in memory. The first poll used it and WHOOP retired it, so every later poll failed with `400 Bad Request`: 45 failures in a row, with no message to the user.

**Cause, part two (found two days later).** Refreshing also retires the previous *access* token. The bot, its MCP child process and Claude Desktop's own MCP server are separate processes, each keeping a private access token, so one process's hourly refresh silently killed the others' (`401 Unauthorized` for up to an hour).

**Fix.** Never cache the refresh token: re-read it from `.env` under a file lock on every refresh and write the replacement back atomically. Share the current access token between processes in a git-ignored file under the same lock, and retry once on a 401 (adopt the fresher shared token, otherwise refresh). One `WhoopClient` per process so refreshes happen hourly, not every 5 minutes.

**Lesson.** A single startup check proves nothing; this bug only appeared on the *second* poll. The verification plan now requires a multi-tick soak that spans a real token rotation. (It passed: nine healthy ticks and one natural rotation on the production machine.)

## 2. "Today" depends on whose clock you ask

**Symptom.** Morning briefs never fired for a user in the Americas, and lookups for a given day sometimes came back "not found".

**Cause.** WHOOP organizes data in *cycles* that run from one sleep onset to the next (wake to wake), not calendar days. The first version asked WHOOP for "today" as a UTC day. In the evening the UTC date has already rolled over while the cycle has not, so the window went empty; for zones far east of UTC it could miss the morning cycle entirely. The same cycle can also span two calendar dates.

**Fix.** The 5-minute poll now fetches the *latest* recovery, sleep and cycle with no date window and files each under the local date its own record belongs to. The agent's date tools resolve a local date to the cycle containing that day's midday and use WHOOP's per-cycle endpoints. Briefs are gated on the recovery's own `cycle_id`, never on a date, so a cycle that spans two dates cannot double-brief; a recovery older than 24 hours is never announced as "new". A first-thought fix ("just pass the local date") was rejected because it fails the same way for far-east zones.

**Lesson.** Do not model someone else's data with your own calendar. Read how the source defines a day.

## 3. The model that did time-zone arithmetic

**Symptom.** An event logged as "a latte at 11:30" was stored as 11:30 UTC, an hour off.

**Cause.** The LLM was asked to turn local times into UTC and treated local time as UTC. Left alone this silently corrupts the feature that joins "what you did" to "the next morning's recovery": an after-midnight local event belongs to the previous UTC day and would be matched to the wrong morning.

**Fix.** Deterministic conversion in Python, at the edges. The model passes and receives local wall-clock time only; the MCP server converts in and out with a real IANA zone (so daylight saving is right), and returns a ready-to-quote display string. Tested at the midnight boundary.

**Lesson.** If a rule can be code, make it code. Do not ask the model to do arithmetic you can do exactly.

## 4. A message that is true on the first run and false on the third

Two bugs with one theme: proactive messages are the product, and they failed silently.

- **A failed message counted as sent.** The scheduler recorded "brief sent" even when delivery failed (the AI provider was rate-limited), so a morning brief could be lost for good. Now a proactive message is marked sent only if it was delivered; a failed one stays due and is retried on the next tick, and the log shows `decision=brief-FAILED`.
- **A stale outage notice repeated itself.** The coach once told its owner that WHOOP was down, while the poll in the same minute had succeeded and the model had made no tool call at all. All scheduled messages shared one conversation thread whose last 20 turns still contained a real outage notice from weeks earlier, and the model copied it. Fix: scheduled messages start with no replayed history, a prompt rule forbids claiming WHOOP is down unless a tool failed in that very reply, and the check-in and brief prompts say to fetch data first. Saved memory is untouched, because it reaches the model through tools, not through history.

**Lesson.** Earlier output is not evidence about the present. And any failure that stops the product doing its job must reach the user, not just the log.

## 5. The stale bot, and the process that was silent for four weeks

- **Two bots.** A `pkill -f` pattern matched nothing (the Python interpreter binary is spelled with a capital P on macOS), so a stale bot ran for two days beside the new one and a "still alive?" warning was wrongly dismissed. Rule now: stop a bot by exact PID and verify the count is zero.
- **Four weeks of silence.** During development the bot ran on a laptop that went to sleep, and nothing noticed: no rule *inside* a process can report that the process is gone. That is why it now runs under systemd on an always-on VM, and why an external liveness check remains on the list.

**Lesson.** A blank result is not evidence of "none". Run a control that must return something before believing an empty answer.

## 6. Finding a free home

The plan was a larger free machine type in a cloud provider's always-free tier. In the chosen region it never had capacity: 658 launch attempts over about 64 hours, none succeeded, and the retry loop itself died because it depended on a laptop staying awake. A read-only capacity-report API turned out to say "available" or "out of capacity" per machine type without creating anything, so the next decision was made on facts: use the smaller always-free machine type with a swap file, after measuring the real footprint (about 320 MB for the coach with a real message). A popular free hosting alternative was rejected for a concrete reason: its disk is wiped on restart, which would revert WHOOP's rotating refresh token to a retired one and lock the coach out until a human re-authorizes.

**Lesson.** Measure before you decide, and write down why alternatives were rejected so they are not re-litigated.

## 7. Moods that were not logged

**Symptom.** "Black coffee at 5:15pm, I feel alert and dialed in" stored the coffee and dropped the feeling.

**Cause.** The prompt said to log what the user did and drank, and the model treated the message as one event.

**Fix.** A prompt rule: one message can hold several things, each logged as its own event, and a stated feeling is a separate `mood` event in the user's own words (never invented). The rule was tested against the real model: the old wording logged the mood in 0 of 4 trials, the new wording in every completed trial; a unit test pins the wording so it cannot be edited away unnoticed.

**Lesson.** Moods are the data the coach learns patterns from. Test prompt rules against the real model, not only mocks, and pin them.

## 8. Dependency drift

A fresh install on the server differed from the development laptop in 75 of about 77 packages, in a project already broken once by a library rename. Fix: freeze every package from the production machine into `requirements.lock`, install only from it, and document an upgrade procedure (test in a clean environment, regenerate the lock, stop the app before installing on a small machine).

* * *

## What I would do differently

- Write the soak test (several polls across a token rotation) before the first deploy, not after the first outage.
- Put the "is it alive?" signal outside the process from day one.
- Treat the data source's own definitions (cycles, token lifetimes) as the first thing to read, not the last.

## Process notes

- Every phase ended with a written status: what was verified, what is open, what is next, and the time it took.
- The assistant stopped and asked at every step that cost money, touched an account, or could not be undone, and a standing rule kept the free-tier constraint honest.
- The nightly backup uses a write-only, single-object link, so no powerful cloud credential ever sits on the server; restore was tested end to end (byte-for-byte checksum match) before it was trusted.
