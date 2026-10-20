"""Central configuration: loads .env once and holds the model string.

The model string lives here alone so swapping Groq for Claude later is a
one-line change (a project hard constraint).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Verified against Groq's live model list on 2026-09-17: llama-3.3-70b-versatile
# is retired; this one returns text and emits tool_calls correctly.
MODEL = "groq/openai/gpt-oss-120b"

MAX_TOOL_ROUNDS = 8
HISTORY_TURNS = 20

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_USER_ID = os.environ["SLACK_USER_ID"]
