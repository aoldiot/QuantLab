"""Mock OpenAI-compatible chat/completions server for probing DeepSeek Harness.

Stands in for DeepSeek/OpenAI/any OpenAI-compatible endpoint so the DSH SDK
tool loop (LLM -> bash tool -> LLM) can be exercised without real credentials.

Serves SSE streaming + plain JSON, logs every request to a file (env
DSH_MOCK_LOG) so we can learn the exact request shape the DSH adapter sends
(path, headers, body: streaming? tool schema? roles?).

Usage:
    .venv/bin/python scripts/dsh_probe_mock_llm.py [port]
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
LOG_FILE = os.environ.get("DSH_MOCK_LOG", "/tmp/dsh-mock-llm.log")
MODEL = "mock-model"

_lock = threading.Lock()


def log_request(path: str, body: dict) -> None:
    with _lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "path": path, "body": body}, ensure_ascii=False) + "\n")


def decide(payload: dict) -> dict:
    messages = payload.get("messages", [])
    has_tool_msg = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)
    user_texts = [str(m.get("content", "")) for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    text = "\n".join(user_texts)

    if any("20000" in t for t in user_texts):
        return {"kind": "truncate"}
    if "Reply with exactly this line" in text:
        return {"kind": "text", "text": "DSH_PROBE_OK"}
    if has_tool_msg:
        if "probe.txt" in text:
            return {"kind": "text", "text": "The file was written. cat printed: DSH_FILE_OK"}
        if "DSH_MARKER" in text and "echo $" in text:
            return {"kind": "text", "text": "The printed value was: persist-9527"}
        if "DSH_BASH_OK" in text:
            return {"kind": "text", "text": "Output: /workspace DSH_BASH_OK=42"}
        if "ls" in text:
            return {"kind": "text", "text": "CONFIRM"}
        return {"kind": "text", "text": "done"}
    if "DSH_MARKER" in text and "export" in text:
        return {"kind": "tool", "name": "bash", "arguments": {"command": "export DSH_MARKER=persist-9527; echo exported", "description": "export a marker"}}
    if "DSH_MARKER" in text and "echo $" in text:
        return {"kind": "tool", "name": "bash", "arguments": {"command": "echo $DSH_MARKER", "description": "print marker"}}
    if "probe.txt" in text:
        return {"kind": "tool", "name": "bash", "arguments": {"command": "echo DSH_FILE_OK > probe.txt && cat probe.txt", "description": "write and read probe file"}}
    if "DSH_BASH_OK" in text:
        return {"kind": "tool", "name": "bash", "arguments": {"command": "pwd && echo DSH_BASH_OK=$((40+2))", "description": "print cwd and a value"}}
    if "ls" in text:
        return {"kind": "tool", "name": "bash", "arguments": {"command": "ls", "description": "list workspace"}}
    return {"kind": "text", "text": "I am a mock model; I cannot help with that."}


def chunk(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def sse(delta_events: list[dict], finish_reason: str) -> bytes:
    out = [
        chunk({"id": "mock-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
               "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    ]
    for delta in delta_events:
        out.append(chunk({"id": "mock-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                          "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
    out.append(chunk({"id": "mock-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                      "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}))
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def full_response(content: str | None, tool_call: dict | None, finish_reason: str) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_call:
        msg["tool_calls"] = [tool_call]
    return {
        "id": "mock-1", "object": "chat.completion", "created": int(time.time()), "model": MODEL,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.endswith("/models"):
            self._send(200, "application/json", json.dumps({"object": "list", "data": [{"id": MODEL}]}).encode())
        else:
            self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        log_request(f"{self.command} {path}", payload)
        if not path.endswith("/chat/completions"):
            self._send(404, "application/json", b'{"error":{"message":"mock only serves /v1/chat/completions"}}')
            return

        decision = decide(payload)
        stream = bool(payload.get("stream"))
        if decision["kind"] == "truncate":
            if stream:
                self._send(200, "text/event-stream", sse([{"content": "A"}], "length"))
            else:
                self._send(200, "application/json", json.dumps(full_response("A", None, "length")).encode())
            return
        if decision["kind"] == "tool":
            name, args = decision["name"], decision["arguments"]
            args_json = json.dumps(args, ensure_ascii=False)
            half = len(args_json) // 2
            tool_call = {"index": 0, "id": "call_mock", "type": "function",
                         "function": {"name": name, "arguments": ""}}
            if stream:
                self._send(200, "text/event-stream", sse([
                    {"role": "assistant", "tool_calls": [dict(tool_call)]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": args_json[:half]}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": args_json[half:]}}]},
                ], "tool_calls"))
            else:
                tool_call["function"]["arguments"] = args_json
                self._send(200, "application/json", json.dumps(full_response(None, tool_call, "tool_calls")).encode())
            return
        text = decision["text"]
        if stream:
            mid = len(text) // 2
            self._send(200, "text/event-stream", sse([{"content": text[:mid]}, {"content": text[mid:]}], "stop"))
        else:
            self._send(200, "application/json", json.dumps(full_response(text, None, "stop")).encode())

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    Path(LOG_FILE).write_text("", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock OpenAI-compatible server listening on http://127.0.0.1:{PORT}/v1/chat/completions")
    print(f"request log: {LOG_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
