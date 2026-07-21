from analyst_ledger.label_suggest import propose, suggest_labels


def test_topic_from_keyword():
    assert "topic:semiconductors" in suggest_labels("new GPU foundry ramp this quarter")


def test_topic_from_ticker_sector_map():
    assert "topic:semiconductors" in suggest_labels("watching NVDA closely")


def test_multiple_topics():
    got = set(suggest_labels("NVDA earnings guidance and the AI startup seed round"))
    assert "topic:semiconductors" in got  # NVDA + gpu/chip? here via ticker
    assert "topic:earnings" in got
    assert "topic:ai-startups" in got


def test_no_false_positive():
    assert suggest_labels("having lunch and walking the dog") == []


def test_propose_has_reasons_and_is_sorted():
    items = propose("NVDA earnings")
    labels = [i["label"] for i in items]
    assert labels == sorted(labels)
    assert all(i["reason"] for i in items)
