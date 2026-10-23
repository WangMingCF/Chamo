"""review_llm.py against a local stand-in for the chat API."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("review_llm.py")
# Runs the script with time.sleep disabled, so retry backoff does not slow the tests.
RUNNER = ("import time, runpy, sys; time.sleep = lambda s: None; sys.argv = ['review_llm']; "
          f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')")


def sse(*texts: str) -> bytes:
    chunks = [{"choices": [{"delta": {"content": t}}]} for t in texts]
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def serve(plan):
    """plan: list of actions per request: "sse", "drop" (close without answering) or an HTTP status."""
    seen = {"requests": 0}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = seen["requests"]
            seen["requests"] += 1
            seen["auth"] = self.headers["Authorization"]
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            action = plan[min(n, len(plan) - 1)]
            if action == "drop":
                self.close_connection = True
                self.connection.shutdown(2)
                return
            if action == "json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "好\nVERDICT: APPROVE"}}]}).encode())
                return
            if isinstance(action, tuple) and action[0] == "error":
                # An error status with a body: the only thing that distinguishes an
                # overloaded endpoint from a retired model from a bad key.
                body = action[2].encode()
                self.send_response(action[1])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if action == "sse":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(sse("没有阻塞问题。\n", "VERDICT: ", "APPROVE"))
                return
            self.send_response(action)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def run(env):
    return subprocess.run([sys.executable, "-c", RUNNER], input="评审这个 PR", capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", **env}, timeout=120)


def env_for(srv, key="k-test"):
    return {"REVIEW_API_KEY": key, "REVIEW_MODEL": "m-test",
            "REVIEW_API_BASE": f"http://127.0.0.1:{srv.server_port}"}


def test_streams_the_reply_and_sends_the_prompt():
    srv, seen = serve(["sse"])
    p = run(env_for(srv))
    srv.shutdown()
    assert p.returncode == 0 and p.stdout.strip().endswith("VERDICT: APPROVE"), p.stderr
    assert seen["path"] == "/chat/completions" and seen["auth"] == "Bearer k-test"
    assert seen["body"]["stream"] is True and seen["body"]["model"] == "m-test"
    assert seen["body"]["messages"][1]["content"] == "评审这个 PR"


def test_a_dropped_connection_is_retried():
    srv, seen = serve(["drop", "sse"])
    p = run(env_for(srv))
    srv.shutdown()
    assert p.returncode == 0 and "VERDICT: APPROVE" in p.stdout, p.stderr
    assert seen["requests"] == 2 and "attempt 1/6 failed" in p.stderr


def test_a_proxy_that_ignores_streaming_still_works():
    srv, _ = serve(["json"])
    p = run(env_for(srv))
    srv.shutdown()
    assert p.returncode == 0 and p.stdout.strip().endswith("VERDICT: APPROVE"), p.stderr


def test_an_error_body_and_the_model_reach_the_log():
    """A bare "HTTPError 503" sent an operator to check the key; say what broke."""
    body = json.dumps({"error": {"message": "model is overloaded", "type": "server_error"}})
    srv, _ = serve([("error", 503, body)])
    p = run(env_for(srv))
    srv.shutdown()
    assert p.returncode == 1
    assert "HTTPError 503" in p.stderr and "model=m-test" in p.stderr
    assert "overloaded" in p.stderr, p.stderr
    assert "k-test" not in p.stderr


def test_missing_model_fails_without_calling():
    assert run({"REVIEW_API_KEY": "k"}).returncode == 2


def test_persistent_errors_fail_and_never_print_the_key():
    srv, seen = serve([500])
    p = run(env_for(srv, key="k-secret"))
    srv.shutdown()
    assert p.returncode == 1 and seen["requests"] == 6
    assert "k-secret" not in p.stdout + p.stderr
