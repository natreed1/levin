from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from messenger import room_files
from messenger.db import MessageStore


def test_grade_and_reviewer_notes_are_injected_as_context(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path))
    item = room_files.save_report(
        "room-a",
        title="NIO trade",
        body="The bull case depends on vehicle deliveries.",
        source="graph",
    )

    updated = room_files.update_file(
        "room-a",
        item["id"],
        {"grade": 2, "grade_notes": "Validate margins and include the bear case."},
        grader="Nat",
    )

    assert updated is not None
    assert updated["grade"] == 2
    assert updated["graded_by"] == "Nat"
    context = room_files.context_for_room("room-a")
    assert "Human grade: 2/5" in context
    assert "Validate margins and include the bear case." in context
    assert "vehicle deliveries" in context


def test_items_removed_from_context_do_not_reach_agents(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path))
    item = room_files.add_file(
        "room-a",
        name="private-note.md",
        content=b"Do not inject this.",
        mime="text/markdown",
        use_for_context=False,
    )
    room_files.update_file(
        "room-a",
        item["id"],
        {"grade": 1, "grade_notes": "Still should not be injected."},
        grader="Nat",
    )

    assert room_files.context_for_room("room-a") == ""


def _client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_INVITE_TOKEN", "server-secret")
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("MESSENGER_DB_PATH", str(tmp_path / "messages.sqlite3"))
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSENGER_SCHEDULER", "0")
    monkeypatch.setenv("MESSENGER_EMAIL_DEV_EXPOSE", "1")
    monkeypatch.delenv("MESSENGER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import importlib
    import messenger.app as app_module

    importlib.reload(app_module)
    return starlette_testclient.TestClient(app_module.create_app())


def _signup(client, email: str, name: str):
    response = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "password12", "display_name": name},
    )
    token = response.json()["dev_verify_url"].split("token=")[-1]
    client.post("/api/auth/verify-email", json={"token": token})
    client.post("/api/auth/login", json={"email": email, "password": "password12"})
    return response.json()["user_id"]


def test_editor_can_upload_and_grade_output(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _signup(client, "owner-files@example.com", "Owner")
    created = client.post("/api/rooms", json={"name": "Owner", "title": "Research"})
    room_id = created.json()["room_id"]

    uploaded = client.post(
        f"/api/rooms/{room_id}/files",
        data={"title": "Trade report", "kind": "report", "use_for_context": "1"},
        files={"upload": ("trade.md", b"Long NIO with a tight stop.", "text/markdown")},
    )
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["file"]["id"]

    graded = client.patch(
        f"/api/rooms/{room_id}/files/{file_id}",
        json={"grade": 4, "grade_notes": "Add valuation sensitivity next time."},
    )
    assert graded.status_code == 200, graded.text
    assert graded.json()["file"]["grade"] == 4

    listing = client.get(f"/api/rooms/{room_id}/files")
    assert listing.status_code == 200
    assert listing.json()["files"][0]["grade_notes"] == "Add valuation sensitivity next time."


def test_viewer_can_read_but_cannot_grade(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_id = _signup(client, "owner-viewer@example.com", "Owner")
    store = MessageStore(db_path=tmp_path / "messages.sqlite3")
    store.create_room(
        "viewer-room",
        "Viewer room",
        hashlib.sha256(b"invite").hexdigest(),
        owner_user_id=owner_id,
    )
    store.add_room_member("viewer-room", owner_id, role="owner")
    item = room_files.save_report(
        "viewer-room", title="Output", body="Report body", source="graph"
    )

    client.cookies.clear()
    viewer_id = _signup(client, "viewer-files@example.com", "Viewer")
    store.add_room_member("viewer-room", viewer_id, role="viewer")

    assert client.get("/api/rooms/viewer-room/files").status_code == 200
    denied = client.patch(
        f"/api/rooms/viewer-room/files/{item['id']}",
        json={"grade": 1, "grade_notes": "No"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "editor_required"
