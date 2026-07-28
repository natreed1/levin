"""Flyleaf Teams stage: model control + hierarchy polish (static UI)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "messenger" / "static"


def test_room_model_controls_in_index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="room-model-select"' in html
    assert 'id="room-model-wrap"' in html
    assert 'id="start-local-model-btn"' in html
    assert 'id="room-overflow"' in html
    assert 'id="teams-empty"' in html
    assert 'id="teams-empty-create"' in html
    assert '<textarea id="body"' in html
    assert "mobile-tabbar" in html


def test_model_status_js_and_css():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "refreshModelStatus" in js
    assert "fillRoomModelSelect" in js
    assert "setTeamsEmptyVisible" in js
    assert "--space-1" in css
    assert ".overflow-menu" in css
    assert ".teams-empty" in css
    assert ".mobile-tabbar" in css
    assert ".system-chip" in css
