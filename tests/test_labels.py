import pytest

from analyst_ledger.labels import (
    LabelError,
    is_valid_label,
    labels_by_axis,
    normalize_label,
    normalize_labels,
    slugify,
)


def test_controlled_axes_normalize():
    assert normalize_label("topic:AI Startups") == "topic:ai-startups"
    assert normalize_label("intent:Research") == "intent:research"
    assert normalize_label("state:OPEN") == "state:open"


def test_open_axes_slugified():
    assert normalize_label("entity:Acme AI, Inc.") == "entity:acme-ai-inc"
    assert normalize_label("project:Q2 Earnings") == "project:q2-earnings"


def test_unknown_controlled_values_rejected():
    with pytest.raises(LabelError):
        normalize_label("topic:not-a-real-theme")
    with pytest.raises(LabelError):
        normalize_label("intent:teleport")
    with pytest.raises(LabelError):
        normalize_label("state:sideways")


def test_malformed_labels_rejected():
    for bad in ["noaxis", "topic:", "unknownaxis:x", ""]:
        with pytest.raises(LabelError):
            normalize_label(bad)


def test_normalize_labels_dedupes_and_sorts():
    got = normalize_labels(["intent:research", "entity:Acme AI", "intent:research"])
    assert got == ["entity:acme-ai", "intent:research"]


def test_labels_by_axis_groups():
    grouped = labels_by_axis(
        ["topic:semiconductors", "entity:acme-ai", "intent:research"]
    )
    assert grouped["topic"] == ["semiconductors"]
    assert grouped["entity"] == ["acme-ai"]
    assert grouped["intent"] == ["research"]


def test_is_valid_label():
    assert is_valid_label("topic:earnings")
    assert not is_valid_label("topic:whatever-nonsense")
    assert not is_valid_label("garbage")


def test_slugify():
    assert slugify("  Acme AI, Inc. ") == "acme-ai-inc"
    assert slugify("NVDA") == "nvda"
