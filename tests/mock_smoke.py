#!/usr/bin/env python3
"""Mock smoke test for ple-harness (no real model key, no Docker, no artifacts).

Runs my-harness/serve.py against:
  * a fake OpenAI-compatible model endpoint (scripted SSE responses), and
  * a fake evaluator (WebSocket client speaking the serve protocol, with an
    in-memory fake task filesystem).

Exercises the full planner -> worker -> verifier -> fix-round flow:
  planner writes a plan, worker writes /app/hello.txt, verifier finds a gap,
  worker fixes it via edit_file, verifier passes.

Asserts the final message, the fake filesystem state, the phase order, and
that NO artifacts/ folder was created in my-harness (artifacts come only from
eval/run.py with a real model).

Usage (from ~/workspace/kaggle-ple):
  uv venv .venv-test --python 3.12
  uv pip install --python .venv-test/bin/python openai==3.26.1 websockets==15.0.1
  .venv-test/bin/python tests/mock_smoke.py

Vendored copy in the entry repo (tests/ at the repo root) runs the same way
from the repo root; see .github/workflows/validate.yml.
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
PLE = os.path.dirname(HERE)
# Two layouts: (A) local dev — tests/ sits next to my-harness/
# (~/workspace/kaggle-ple); (B) vendored in the entry repo — tests/ is at
# the repo root and serve.py sits next to it.
if os.path.exists(os.path.join(PLE, "my-harness", "serve.py")):
    HARNESS = os.path.join(PLE, "my-harness")
else:
    HARNESS = PLE
WS_PORT = 18765

PLAN_TEXT = (
    "Plan:\n1. Deliverable: /app/hello.txt (plain text) containing the word "
    "'hello'.\n2. Method: list /app, then write the file.\n3. Check: read the "
    "file back."
)
GAP_TEXT = "1. /app/hello.txt should say 'hello world', not 'hello'."
SUMMARY_1 = "Wrote /app/hello.txt: hello"
SUMMARY_2 = "Wrote /app/hello.txt: hello world"

# Scripted model replies, keyed by phase (detected from the system prompt)
# and by the 1-based request count within that phase.
SCRIPT = {
    "planner": [("text", PLAN_TEXT)],
    "worker": [
        ("tool", "bash", {"command": "ls -la /app"}),
        ("tool", "write_file", {"path": "/app/hello.txt", "content": "hello"}),
        ("text", SUMMARY_1),
        ("tool", "edit_file", {
            "path": "/app/hello.txt", "old_text": "hello",
            "new_text": "hello world"}),
        ("text", SUMMARY_2),
    ],
    "verifier": [("text", GAP_TEXT), ("text", "PASS")],
}

requests_seen = []  # every model request body, in order
counts = {"planner": 0, "worker": 0, "verifier": 0, "other": 0}


def phase_of(body):
    system = ""
    for m in body.get("messages", []):
        if m.get("role") == "system":
            system = m.get("content", "")
            break
    if "You plan an engineering task" in system:
        return "planner"
    if "You check an engineer" in system:
        return "verifier"
    if "You are an expert engineer working on a task" in system:
        return "worker"
    return "other"


def sse_chunk(delta, finish=None, usage=None):
    choice = {"index": 0, "delta": delta}
    if finish is not None:
        choice["finish_reason"] = finish
    chunk = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-model",
        "choices": [choice],
    }
    if usage is not None:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk)}\n\n"


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == "/v1/chat/completions", self.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        requests_seen.append(body)
        phase = phase_of(body)
        counts[phase] += 1
        script = SCRIPT.get(phase, [])
        step = script[counts[phase] - 1] if counts[phase] <= len(script) else ("text", "done")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def send(text):
            self.wfile.write(text.encode())

        send(sse_chunk({"role": "assistant", "content": ""}))
        if step[0] == "text":
            send(sse_chunk({"content": step[1]}))
            send(sse_chunk(
                {}, finish="stop",
                usage={"prompt_tokens": 100, "completion_tokens": 20,
                       "total_tokens": 120}))
        else:
            _, name, args = step
            send(sse_chunk({"tool_calls": [{
                "index": 0, "id": f"call_{phase}_{counts[phase]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]}))
            send(sse_chunk(
                {}, finish="tool_calls",
                usage={"prompt_tokens": 100, "completion_tokens": 20,
                       "total_tokens": 120}))
        send("data: [DONE]\n\n")


class FakeEvaluator:
    """WebSocket client playing the evaluator; in-memory fake /app."""

    def __init__(self):
        self.fs = {}
        self.requests = []
        self.final_message = None

    async def run(self):
        async with websockets.connect(
            f"ws://127.0.0.1:{WS_PORT}", max_size=16 * 2**20
        ) as ws:
            await ws.send(json.dumps({
                "type": "task", "protocol": 1, "session": "mock-smoke",
                "instruction": (
                    "Write a hello file: create /app/hello.txt containing "
                    "the word 'hello'."),
                "workdir": "/app",
                "limits": {"agent_timeout_sec": 3600, "exec_timeout_sec": 600,
                           "max_output_bytes": 100000,
                           "max_write_bytes": 10000000},
                "mcp_servers": [],
            }))
            async for frame in ws:
                msg = json.loads(frame)
                mtype = msg.get("type")
                if mtype in ("log", "usage"):
                    continue
                if mtype == "done":
                    self.final_message = msg.get("final_message", "")
                    return
                if mtype == "error":
                    raise AssertionError(f"harness sent error: {msg}")
                self.requests.append(msg)
                rid = msg["id"]
                if mtype == "exec":
                    cmd = msg["command"]
                    if cmd.startswith("ls"):
                        out = "\n".join(sorted(self.fs)) or "(empty dir)"
                    else:
                        out = "(mock exec ok)"
                    reply = {"type": "result", "id": rid, "exit_code": 0,
                             "output": out, "truncated": False,
                             "timed_out": False}
                elif mtype == "read_file":
                    data = self.fs.get(msg["path"])
                    if data is None:
                        reply = {"type": "result", "id": rid, "ok": False,
                                 "error": "no such file"}
                    else:
                        reply = {"type": "result", "id": rid, "ok": True,
                                 "content_b64": base64.b64encode(data).decode(),
                                 "size": len(data), "truncated": False}
                elif mtype == "write_file":
                    data = base64.b64decode(msg["content_b64"])
                    if msg.get("append"):
                        data = self.fs.get(msg["path"], b"") + data
                    self.fs[msg["path"]] = data
                    reply = {"type": "result", "id": rid, "ok": True,
                             "size": len(data)}
                else:
                    reply = {"type": "result", "id": rid, "ok": False,
                             "error": f"unexpected request {mtype}"}
                await ws.send(json.dumps(reply))


def main():
    # 1. Fake model endpoint on an ephemeral port.
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 2. Start serve.py (from my-harness so `import taskbridge` resolves).
    env = dict(os.environ)
    env.update({
        "MODEL_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "MODEL_API_KEY": "mock-key",
        "MODEL_NAME": "mock-model",
        "PORT": str(WS_PORT),
    })
    proc = subprocess.Popen(
        [sys.executable, "serve.py"], cwd=HARNESS, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    evaluator = FakeEvaluator()
    try:
        # Wait for serve.py to listen before connecting.
        import socket
        import time as _time
        start = _time.monotonic()
        while True:
            if proc.poll() is not None:
                out, _ = proc.communicate()
                raise AssertionError(
                    f"serve.py exited early (rc={proc.returncode}):\n{out}")
            try:
                with socket.create_connection(("127.0.0.1", WS_PORT),
                                              timeout=1):
                    break
            except OSError:
                if _time.monotonic() - start > 30:
                    raise AssertionError("serve.py did not listen in 30 s")
                _time.sleep(0.2)
        asyncio.run(asyncio.wait_for(evaluator.run(), timeout=120))
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    server.shutdown()

    # 3. Assertions.
    failures = []
    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    check(evaluator.final_message is not None, "harness sent done")
    check(evaluator.final_message == SUMMARY_2,
          f"final_message == {SUMMARY_2!r} (got {evaluator.final_message!r})")
    check(evaluator.fs.get("/app/hello.txt") == b"hello world",
          "fake FS /app/hello.txt == b'hello world'")
    check(counts == {"planner": 1, "worker": 5, "verifier": 2, "other": 0},
          f"model request counts planner=1 worker=5 verifier=2 (got {counts})")
    worker_first = next(
        (b for b in requests_seen if phase_of(b) == "worker"), None)
    check(worker_first is not None and "## Plan" in
          worker_first["messages"][1]["content"]
          and PLAN_TEXT in worker_first["messages"][1]["content"],
          "worker received the planner's plan in its user message")
    tool_names = [r.get("command") or r.get("path")
                  for r in evaluator.requests]
    check(any(r.get("type") == "exec" for r in evaluator.requests),
          "worker ran bash (ls)")
    check(any(r.get("type") == "write_file" for r in evaluator.requests),
          "worker wrote /app/hello.txt")
    check(any(r.get("type") == "read_file" and r.get("path") == "/app/hello.txt"
              for r in evaluator.requests),
          "edit_file read /app/hello.txt back (fix round ran)")
    check(not os.path.exists(os.path.join(HARNESS, "artifacts")),
          "no artifacts/ folder was created in my-harness")
    if failures:
        print("\n--- serve.py output ---\n" + out)
        print(f"\nMOCK SMOKE: {len(failures)} FAILURE(S)")
        return 1
    print("\nMOCK SMOKE: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
