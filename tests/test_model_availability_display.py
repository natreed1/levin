"""Test model availability display in chat dashboard."""

from io import BytesIO
import json
import pytest

from analyst_ledger import Ledger
from analyst_ledger.dashboard import make_app


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def test_chats_page_renders_without_models(ledger: Ledger):
    """Test that chats page renders successfully even without model registry."""
    app = make_app(ledger)
    
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/chats",
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    status = []
    body = b"".join(app(environ, lambda s, _h: status.append(s)))
    
    assert status[0].startswith("200")
    assert b"Chat with your friend" in body
    # Should render even without models
    assert b"chat-layout" in body


def test_model_availability_helper_handles_missing_registry():
    """Test that _get_model_availability handles missing model registry gracefully."""
    from analyst_ledger.dashboard import _get_model_availability
    
    # Should return empty list when registry is not available
    models = _get_model_availability("test_user")
    assert isinstance(models, list)
    assert len(models) == 0


def test_chats_page_includes_model_status_section(ledger: Ledger, monkeypatch):
    """Test that chats page includes model status when models are available."""
    # Mock the model availability function to return test data
    def mock_get_availability(user_id):
        return [
            {
                "label": "claude-sonnet-4",
                "provider": "Claude (Anthropic)",
                "is_local": False,
                "is_active": True,
            },
            {
                "label": "qwen3:8b",
                "provider": "Local open source",
                "is_local": True,
                "is_active": False,
            },
        ]
    
    import analyst_ledger.dashboard as dashboard_module
    monkeypatch.setattr(dashboard_module, "_get_model_availability", mock_get_availability)
    
    app = make_app(ledger)
    
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/chats",
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    status = []
    body = b"".join(app(environ, lambda s, _h: status.append(s)))
    
    assert status[0].startswith("200")
    body_str = body.decode("utf-8")
    
    # Check for model status section
    assert "model-status" in body_str
    assert "Available Models" in body_str
    assert "is in this Chat" in body_str or "is connected" in body_str


def test_model_status_shows_friendly_messages(ledger: Ledger, monkeypatch):
    """Test that model status shows user-friendly messages."""
    def mock_get_availability(user_id):
        return [
            {
                "label": "claude-sonnet-4",
                "provider": "Claude (Anthropic)",
                "is_local": False,
                "is_active": True,
            },
        ]
    
    import analyst_ledger.dashboard as dashboard_module
    monkeypatch.setattr(dashboard_module, "_get_model_availability", mock_get_availability)
    
    app = make_app(ledger)
    
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/chats",
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    status = []
    body = b"".join(app(environ, lambda s, _h: status.append(s)))
    
    body_str = body.decode("utf-8")
    
    # Should show friendly status message
    assert "Claude (Anthropic)" in body_str
    assert "model-item" in body_str
    assert "indicator" in body_str
