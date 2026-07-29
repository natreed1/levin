"""Token-usage capture in synthesize.py's low-level model-call functions."""

import json

import pytest

from analyst_ledger import synthesize


@pytest.fixture(autouse=True)
def _reset_last_usage():
    synthesize._LAST_USAGE.set(None)
    yield
    synthesize._LAST_USAGE.set(None)


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, text, input_tokens, output_tokens):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeMessagesAPI:
    def __init__(self, text, input_tokens, output_tokens):
        self._text = text
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def create(self, **kwargs):
        return _FakeMessage(self._text, self._input_tokens, self._output_tokens)


class _FakeAnthropicClient:
    def __init__(self, api_key=None):
        self.messages = _FakeMessagesAPI("hello from claude", 111, 222)


def test_call_anthropic_messages_captures_usage(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)

    result = synthesize._call_anthropic_messages(
        [{"role": "user", "content": "hi"}]
    )
    assert result == "hello from claude"
    usage = synthesize.last_usage()
    assert usage is not None
    assert usage["tokens_in"] == 111
    assert usage["tokens_out"] == 222
    assert usage["latency_ms"] >= 0


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, payload):
    def fake_urlopen(req, timeout=120):
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(synthesize.urllib.request, "urlopen", fake_urlopen)


def test_call_openai_compatible_captures_usage(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "choices": [{"message": {"content": "hi there"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 75},
        },
    )
    result = synthesize._call_openai_compatible_messages(
        [{"role": "user", "content": "hi"}]
    )
    assert result == "hi there"
    usage = synthesize.last_usage()
    assert usage is not None
    assert usage["tokens_in"] == 50
    assert usage["tokens_out"] == 75
    assert usage["latency_ms"] >= 0


def test_last_usage_resets_when_response_has_no_usage(monkeypatch):
    # Seed a usage value from a prior call.
    _patch_urlopen(
        monkeypatch,
        {
            "choices": [{"message": {"content": "first"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    synthesize._call_openai_compatible_messages([{"role": "user", "content": "hi"}])
    assert synthesize.last_usage() is not None

    # A response with no usage block must clear the stale value, not keep it.
    _patch_urlopen(
        monkeypatch,
        {"choices": [{"message": {"content": "second, no usage"}}]},
    )
    synthesize._call_openai_compatible_messages([{"role": "user", "content": "hi"}])
    assert synthesize.last_usage() is None


def test_last_usage_resets_before_a_failed_call(monkeypatch):
    import urllib.error

    # Seed a usage value from a prior successful call.
    _patch_urlopen(
        monkeypatch,
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 9},
        },
    )
    synthesize._call_openai_compatible_messages([{"role": "user", "content": "hi"}])
    assert synthesize.last_usage() is not None

    def raising_urlopen(req, timeout=120):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(synthesize.urllib.request, "urlopen", raising_urlopen)
    with pytest.raises(RuntimeError):
        synthesize._call_openai_compatible_messages([{"role": "user", "content": "hi"}])
    # A failed call must not leave a previous call's usage sitting around.
    assert synthesize.last_usage() is None
