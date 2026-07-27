"""Flyleaf chat stage: model availability / active indicator (static UI)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "messenger" / "static"


def test_model_status_markup_in_index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="model-status"' in html
    assert 'id="model-status-list"' in html
    assert "Available Models" in html


def test_model_status_js_and_css():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "refreshModelStatus" in js
    assert "is active" in js
    assert ".model-status" in css
    assert ".model-item .indicator" in css
