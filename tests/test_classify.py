import analyst_ledger.classify as classify_mod
from analyst_ledger.classify import classify_message


def test_deterministic_research():
    r = classify_message("we should look into Acme AI", allow_qwen=False)
    assert r["kind"] == "research"
    assert r["source"] == "deterministic"
    assert "kind:research" in r["labels"]
    assert r["entity"] == "acme-ai"


def test_deterministic_build():
    r = classify_message(
        "the sync is throwing an error, we should refactor the classifier",
        allow_qwen=False,
    )
    assert r["kind"] == "build"
    assert "kind:build" in r["labels"]


def test_deterministic_observation():
    r = classify_message("NVDA just broke out, up 5% today", allow_qwen=False)
    assert r["kind"] == "observation"


def test_deterministic_question():
    r = classify_message("how does the tagging thing work", allow_qwen=False)
    assert r["kind"] == "question"


def test_smalltalk_gets_no_kind():
    r = classify_message("haha yeah that was fun last night", allow_qwen=False)
    assert r["kind"] is None
    assert r["labels"] == []


def test_qwen_fallback_used(monkeypatch):
    monkeypatch.setenv("ANALYST_CLASSIFY_QWEN", "on")
    monkeypatch.setattr(classify_mod, "_qwen_kind", lambda text, examples=None: "idea")
    r = classify_message("we might spin something up around this space", allow_qwen=True)
    assert r["kind"] == "idea"
    assert r["source"] == "qwen"


def test_qwen_not_called_when_deterministic(monkeypatch):
    def _boom(text, examples=None):
        raise AssertionError("Qwen should not be called when rules already matched")

    monkeypatch.setattr(classify_mod, "_qwen_kind", _boom)
    r = classify_message("look into Tesla earnings", allow_qwen=True)
    assert r["kind"] == "research"
    assert r["source"] == "deterministic"


def test_qwen_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ANALYST_CLASSIFY_QWEN", "off")
    monkeypatch.setattr(
        classify_mod, "_qwen_kind", lambda text, examples=None: "idea"
    )  # would be used if enabled
    r = classify_message("just some ambiguous chatter here", allow_qwen=True)
    assert r["kind"] is None  # env off -> no Qwen fallback


def test_classify_forwards_examples(monkeypatch):
    monkeypatch.setenv("ANALYST_CLASSIFY_QWEN", "on")
    seen = {}

    def fake_qwen(text, examples=None):
        seen["examples"] = examples
        return "idea"

    monkeypatch.setattr(classify_mod, "_qwen_kind", fake_qwen)
    ex = [{"text": "the sync broke", "kind": "build"}]
    r = classify_message("some ambiguous thing here", allow_qwen=True, examples=ex)
    assert r["kind"] == "idea"
    assert seen["examples"] == ex


def test_qwen_fewshot_prompt(monkeypatch):
    captured = {}

    def fake_call(messages, **kw):
        captured["messages"] = messages
        return "research"

    monkeypatch.setattr(
        "analyst_ledger.synthesize._call_openai_compatible_messages", fake_call
    )
    kind = classify_mod._qwen_kind(
        "what about this new thing",
        examples=[{"text": "the sync broke", "kind": "build"}],
    )
    assert kind == "research"
    contents = [m["content"] for m in captured["messages"]]
    assert "the sync broke" in contents
    assert "build" in contents
    assert contents[-1] == "what about this new thing"
