"""review_llm.py against a local stand-in for the chat API."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("review_llm.py")


def serve(reply_status=200):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers["Authorization"]
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(reply_status)
            self.end_headers()
            if reply_status == 200:
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "好\nVERDICT: APPROVE"}}]}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def run(env):
    return subprocess.run([sys.executable, str(SCRIPT)], input="评审这个 PR", capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", **env}, timeout=120)


def test_sends_prompt_and_prints_reply():
    srv, seen = serve()
    p = run({"REVIEW_API_KEY": "k-test", "REVIEW_MODEL": "m-test",
             "REVIEW_API_BASE": f"http://127.0.0.1:{srv.server_port}"})
    srv.shutdown()
    assert p.returncode == 0 and p.stdout.strip().endswith("VERDICT: APPROVE")
    assert seen["path"] == "/chat/completions" and seen["auth"] == "Bearer k-test"
    assert seen["body"]["model"] == "m-test" and seen["body"]["messages"][1]["content"] == "评审这个 PR"


def test_missing_model_fails_without_calling():
    assert run({"REVIEW_API_KEY": "k"}).returncode == 2


def test_api_error_fails_and_does_not_print_the_key(monkeypatch):
    srv, _ = serve(reply_status=500)
    p = subprocess.run([sys.executable, "-c",
                        "import time,runpy,sys; time.sleep=lambda s: None; sys.argv=['x']; "
                        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')"],
                       input="x", capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "REVIEW_API_KEY": "k-secret", "REVIEW_MODEL": "m",
                            "REVIEW_API_BASE": f"http://127.0.0.1:{srv.server_port}"})
    srv.shutdown()
    assert p.returncode != 0 and "k-secret" not in p.stdout + p.stderr
