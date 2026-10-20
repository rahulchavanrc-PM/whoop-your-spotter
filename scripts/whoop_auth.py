"""One-time WHOOP OAuth handshake.

Run this once to authorize the app against your WHOOP account. It opens your
browser to WHOOP's consent screen, catches the redirect on a local HTTP
server, exchanges the returned code for tokens, and writes
WHOOP_REFRESH_TOKEN into .env. Requires WHOOP_CLIENT_ID and
WHOOP_CLIENT_SECRET to already be set in .env.
"""

import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES = (
    "read:recovery read:cycles read:sleep read:workout "
    "read:profile read:body_measurement offline"
)


class CallbackHandler(BaseHTTPRequestHandler):
    """Catches the single OAuth redirect, then lets the server stop."""

    result: dict = {}

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if "code" in CallbackHandler.result:
            body = "<h1>Authorized.</h1>You can close this tab and go back to the terminal."
        else:
            body = "<h1>Something went wrong.</h1>No authorization code was returned."
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # keep stdout clean


def update_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines(keepends=False)
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def main() -> int:
    load_dotenv(ENV_PATH)
    client_id = os.environ.get("WHOOP_CLIENT_ID")
    client_secret = os.environ.get("WHOOP_CLIENT_SECRET")
    # "..." is the placeholder shipped in .env.example; without this check a
    # fresh clone would open the browser with a bogus client and wait forever.
    if not client_id or not client_secret or "..." in (client_id, client_secret):
        print("WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET must be set in .env first.")
        return 1

    state = secrets.token_urlsafe(16)
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    url = f"{AUTH_URL}?{urlencode(auth_params)}"

    print("Opening your browser to authorize with WHOOP...")
    print("If it doesn't open automatically, visit this URL:")
    print(url)
    webbrowser.open(url)

    server = HTTPServer(("localhost", 8765), CallbackHandler)
    print("Waiting for the redirect on http://localhost:8765/callback ...")
    server.handle_request()  # blocks until one request arrives
    server.server_close()

    result = CallbackHandler.result
    if result.get("state") != state:
        print("State mismatch — aborting for safety. Please re-run this script.")
        return 1
    code = result.get("code")
    if not code:
        print(f"No code returned. WHOOP said: {result}")
        return 1

    token_resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if token_resp.status_code != 200:
        print(f"Token exchange failed ({token_resp.status_code}): {token_resp.text}")
        return 1

    tokens = token_resp.json()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("No refresh_token in response — check that 'offline' scope was granted.")
        return 1

    update_env("WHOOP_REFRESH_TOKEN", refresh_token)
    print("Success. WHOOP_REFRESH_TOKEN written to .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
