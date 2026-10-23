"""The review client must say enough to tell one outage from another.

A bare `HTTPError 503` sent an operator to check REVIEW_API_KEY when the model
endpoint was the problem; the server's own error body and the model name are the
two facts that tell an overload from a retired model from a bad key. These tests
pin that, pin that the key never reaches the log, and keep the streaming path
(the one that survives a proxy dropping an idle connection) covered.
"""
from __future__ import annotations

import email.message
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "ops" / "agent-loop" / "review_llm.py"
KEY = "sk-test-do-not-leak-0123456789"
MODEL = "glm-5.3-flash"


def load():
    spec = importlib.util.spec_from_file_location("review_llm_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse(io.BytesIO):
    """Enough of an HTTP response for both the JSON and the SSE path."""

    def __init__(self, payload: bytes, content_type: str = "application/json"):
        super().__init__(payload)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture()
def client(monkeypatch):
    module = load()
    monkeypatch.setenv("REVIEW_API_KEY", KEY)
    monkeypatch.setenv("REVIEW_MODEL", MODEL)
    monkeypatch.setenv("REVIEW_API_BASE", "https://example.invalid")
    # The backoff schedule is not what is under test; sleeping would only slow it.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


def http_error(body: str, code: int = 503) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "Service Unavailable",
                                  {}, io.BytesIO(body.encode()))


def run(client, monkeypatch, error, stdin="review me"):
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(client.sys, "stdin", io.StringIO(stdin))
    return client.main()


def test_an_http_error_reports_the_server_body_and_the_model(client, monkeypatch, capsys):
    body = json.dumps({"error": {"message": "model is overloaded", "type": "server_error"}})
    assert run(client, monkeypatch, http_error(body)) == 1
    err = capsys.readouterr().err
    assert "HTTPError 503" in err
    assert "model=" + MODEL in err, "the log must name the model that failed"
    assert "overloaded" in err, "the server's own message must survive to the log"
    assert KEY not in err, "the API key must never be logged"


def test_a_retired_model_is_distinguishable_from_an_overload(client, monkeypatch, capsys):
    body = json.dumps({"error": {"message": "model not found", "type": "invalid_request_error"}})
    assert run(client, monkeypatch, http_error(body, 404)) == 1
    err = capsys.readouterr().err
    assert "HTTPError 404" in err and "model not found" in err


def test_a_connection_failure_still_explains_itself(client, monkeypatch, capsys):
    assert run(client, monkeypatch, urllib.error.URLError("name resolution failed")) == 1
    err = capsys.readouterr().err
    assert "URLError" in err and "name resolution failed" in err


def test_a_huge_server_body_is_truncated(client, monkeypatch, capsys):
    assert run(client, monkeypatch, http_error("x" * 5000)) == 1
    err = capsys.readouterr().err
    assert "..." in err
    assert len(err) < client.MAX_DETAIL * 3


def test_a_json_reply_is_printed_and_exits_zero(client, monkeypatch, capsys):
    payload = {"choices": [{"message": {"content": "VERDICT: APPROVE"}}]}
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda *_a, **_k: FakeResponse(json.dumps(payload).encode()))
    monkeypatch.setattr(client.sys, "stdin", io.StringIO("review me"))
    assert client.main() == 0
    assert "VERDICT: APPROVE" in capsys.readouterr().out


def test_a_streamed_reply_is_assembled_and_printed(client, monkeypatch, capsys):
    events = [{"choices": [{"delta": {"content": "VERDICT: "}}]},
              {"choices": [{"delta": {"content": "REQUEST_CHANGES"}}]}]
    stream = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda *_a, **_k: FakeResponse(stream.encode(), "text/event-stream"))
    monkeypatch.setattr(client.sys, "stdin", io.StringIO("review me"))
    assert client.main() == 0
    assert "VERDICT: REQUEST_CHANGES" in capsys.readouterr().out


def test_an_empty_reply_is_a_failure_not_a_silent_pass(client, monkeypatch, capsys):
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda *_a, **_k: FakeResponse(b"", "text/event-stream"))
    monkeypatch.setattr(client.sys, "stdin", io.StringIO("review me"))
    assert client.main() == 1
    assert "empty reply" in capsys.readouterr().err


def test_missing_configuration_fails_loudly_rather_than_reviewing_with_a_default(monkeypatch, capsys):
    module = load()
    monkeypatch.setenv("REVIEW_MODEL", "")
    monkeypatch.setenv("REVIEW_API_KEY", KEY)
    assert module.main() == 2
    assert "REVIEW_MODEL" in capsys.readouterr().err

def test_a_transient_outage_is_ridden_out_instead_of_failing_the_check(client, monkeypatch, capsys):
    """Three attempts with 15/30 s of backoff gave up after ~46 s.

    A required check that dies on a one-minute outage blocks every PR, so the
    budget must let a recovery land instead of counting attempts.
    """
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 5:
            raise http_error("temporarily unavailable")
        return FakeResponse(json.dumps({"choices": [{"message": {"content": "VERDICT: APPROVE"}}]}).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(client.sys, "stdin", io.StringIO("review me"))
    assert client.main() == 0
    assert "VERDICT: APPROVE" in capsys.readouterr().out
    assert calls["n"] == 5, "four consecutive failures must not exhaust the retries"


def test_the_retry_budget_bounds_the_wait_and_says_so(client, monkeypatch, capsys):
    """A persistent outage must end near the budget, not after every sleep."""
    clock = {"now": 0.0}
    monkeypatch.setattr(client.time, "monotonic", lambda: clock["now"])

    def slow_failure(*_a, **_k):
        clock["now"] += client.RETRY_BUDGET  # each attempt eats the whole budget
        raise http_error("still down")

    monkeypatch.setattr(client.urllib.request, "urlopen", slow_failure)
    monkeypatch.setattr(client.sys, "stdin", io.StringIO("review me"))
    assert client.main() == 1
    err = capsys.readouterr().err
    assert "retry budget" in err, "an exhausted budget must be reported"
