import pytest

from analyst_ledger.actionable import detect_actionable


def test_detects_explicit_research_ask():
    d = detect_actionable("we should look into Acme AI, could be big")
    assert d.matched
    assert d.entity == "acme-ai"
    assert set(d.labels) == {"entity:acme-ai", "intent:research", "state:open"}


def test_research_verb_form():
    d = detect_actionable("can you research Foobar Labs for us")
    assert d.matched
    assert d.entity == "foobar-labs"


def test_called_pattern():
    d = detect_actionable("look into a startup called Nimbus Data")
    assert d.matched
    assert d.entity == "nimbus-data"


def test_trailing_filler_trimmed():
    d = detect_actionable("look into Acme AI startup")
    assert d.entity == "acme-ai"


def test_no_entity_pronoun_only():
    assert not detect_actionable("look into it when you can").matched


def test_research_as_noun_not_actionable():
    assert not detect_actionable("I did some research on the sector").matched


def test_non_trigger_falls_through():
    assert not detect_actionable("where are the TSLA reports?").matched
    assert not detect_actionable("any new NVDA filings today?").matched


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("ANALYST_CHAT_ACTIONABLE", "off")
    assert not detect_actionable("look into Acme AI").matched
