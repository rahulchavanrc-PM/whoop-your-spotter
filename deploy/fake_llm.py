"""A stand-in for the AI provider, so memory can be measured without any secret.

Speaks the OpenAI chat-completions format on 127.0.0.1:8099. First request in a
conversation -> a `log_event` tool call; once a tool result is present -> a
short text answer. Used only by `deploy/memcheck.py --fake`; stdlib only.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if any(m.get("role") == "tool" for m in body["messages"]):
            message, finish = {"role": "assistant", "content": "Logged that."}, "stop"
        else:
            call = {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "log_event",
                    "arguments": json.dumps({"kind": "note", "note": "synthetic memory test"}),
                },
            }
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        out = json.dumps(
            {
                "id": "x", "object": "chat.completion", "created": 0, "model": "fake",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
