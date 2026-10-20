"""Measure the coach's real memory use under actual messages. Linux only (/proc).

Runs the REAL bot modules (everything `coach.app` imports) and the REAL MCP tool
child process, sends messages through the same agent loop a Slack DM uses, and
reports per-process memory plus the whole machine's peak. No Slack connection.

  python deploy/memcheck.py            real AI provider + real tools (needs .env).
                                       Default question only reads data.
  python deploy/fake_llm.py &          no secrets needed, and no WHOOP calls:
  python deploy/memcheck.py --fake     the AI endpoint is faked locally. Writes a
                                       test event, so it REFUSES to run if a
                                       coach.db exists, and removes what it made.

Why this exists: a small free-tier VM is tight on RAM, so "does it fit" was
measured (about 290 MB for the coach with a real message) rather than assumed. Re-run it after a few days of uptime to catch slow growth.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _kb(name: str) -> dict:
    out = {}
    for line in open(name):
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0]) if rest.split() and rest.split()[0].isdigit() else 0
    return out


class Sampler(threading.Thread):
    """Samples whole-machine memory once a second and remembers the peaks."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak_used = self.peak_swap = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            m = _kb("/proc/meminfo")
            self.peak_used = max(self.peak_used, (m["MemTotal"] - m["MemAvailable"]) // 1024)
            self.peak_swap = max(self.peak_swap, (m["SwapTotal"] - m["SwapFree"]) // 1024)
            time.sleep(1)

    def stop(self):
        self._stop.set()


def proc_mb(pid, key):
    return _kb(f"/proc/{pid}/status").get(key, 0) // 1024


def report(label):
    me = os.getpid()
    kids = subprocess.run(["pgrep", "-P", str(me)], capture_output=True, text=True).stdout.split()
    total = proc_mb(me, "VmRSS")
    parts = [f"bot {total}MB (peak {proc_mb(me, 'VmHWM')}MB)"]
    for kid in kids:
        try:
            rss = proc_mb(kid, "VmRSS")
            parts.append(f"tool-child {rss}MB (peak {proc_mb(kid, 'VmHWM')}MB)")
            total += rss
        except OSError:
            pass
    print(f"[{label}] " + " | ".join(parts) + f" | combined {total}MB", flush=True)


async def main(args):
    db_file = ROOT / "coach.db"
    if args.fake:
        if db_file.exists():
            sys.exit("Refusing --fake: coach.db exists and the fake AI would write a test event into it.")
        for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_ID"):
            os.environ.setdefault(key, "dummy")
        import litellm

        original = litellm.acompletion

        async def to_fake(**kwargs):
            return await original(api_base=f"http://127.0.0.1:{args.port}", api_key="x", **kwargs)

        litellm.acompletion = to_fake

    from coach import agent as agent_mod
    from coach import app as app_mod
    from coach import db

    if args.fake:
        agent_mod.MODEL = "openai/fake"

    sampler = Sampler()
    sampler.start()
    db.init_db()
    report("after importing the real bot modules")
    agent = app_mod.agent
    await agent.start()
    report("after starting the tool child")
    questions = args.messages or (
        ["memory check message %d" % i for i in (1, 2, 3)]
        if args.fake
        else ["What was my recovery today?"]
    )
    for i, text in enumerate(questions, 1):
        started = time.time()
        reply = await agent.respond(text, thread_ts="memcheck")
        report(f"after message {i} ({time.time() - started:.1f}s)")
        print(f"    reply: {reply[:120]!r}")
    report("final")
    await agent.aclose()
    sampler.stop()
    m = _kb("/proc/meminfo")
    print(
        f"\nmachine peak during the run: {sampler.peak_used} MB used of {m['MemTotal'] // 1024} MB, "
        f"swap used {sampler.peak_swap} MB"
    )
    conn = db.get_connection()
    conn.execute("DELETE FROM conversations WHERE thread_ts = 'memcheck'")
    conn.commit()
    conn.close()
    if args.fake:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_file) + suffix).unlink(missing_ok=True)
        print("removed the scratch coach.db this run created")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--fake", action="store_true", help="use the local fake AI endpoint (no secrets)")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--messages", nargs="*", help="override the questions sent")
    asyncio.run(main(parser.parse_args()))
