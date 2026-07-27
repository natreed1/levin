"""Flyleaf Tracking labels / People-room capture / classify sweep wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from messenger.tenancy import user_context


def _client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_INVITE_TOKEN", "server-secret")
    monkeypatch.setenv("MESSENGER_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("MESSENGER_DB_PATH", str(tmp_path / "messages.sqlite3"))
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("MESSENGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESSENGER_SCHEDULER", "0")
    monkeypatch.setenv("MESSENGER_CLASSIFY_SWEEP", "0")
    monkeypatch.setenv("MESSENGER_EMAIL_DEV_EXPOSE", "1")
    monkeypatch.delenv("MESSENGER_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("MESSENGER_RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MESSENGER_SMTP_HOST", raising=False)
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import importlib
    import messenger.app as app_module

    importlib.reload(app_module)
    app = app_module.create_app()
    return starlette_testclient.TestClient(app)


def _signup_and_login(client, *, email: str, password: str = "password12", name: str = "Nat"):
    created = client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "display_name": name},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    token = body["dev_verify_url"].split("token=")[-1]
    verified = client.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    logged = client.post("/api/auth/login", json={"email": email, "password": password})
    assert logged.status_code == 200, logged.text
    return logged.json()


def test_labels_vocab_and_correct(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    me = _signup_and_login(client, email="labels@example.com")
    uid = me["user_id"]

    with user_context(uid) as ledger:
        thread = ledger.get_or_create_chat_thread(master=True)
        msg = ledger.append_chat_message(
            thread.session_id, role="user", content="look into Acme AI"
        )
        ledger.record_ask_labels(
            thread.session_id,
            ["kind:research", "entity:acme-ai"],
            source="chat_classify",
            meta={"target_event_id": msg.event_id},
        )
        target = msg.event_id
        session_id = thread.session_id

    vocab = client.get("/api/tracking/labels/vocab")
    assert vocab.status_code == 200
    kinds = vocab.json()["kinds"]
    assert "research" in kinds and "build" in kinds

    events = client.get("/api/tracking/events?limit=50")
    assert events.status_code == 200
    rows = events.json()["events"]
    assert all(r.get("sensitivity") != "restricted" for r in rows)
    labeled = [r for r in rows if r.get("type") == "label"]
    assert labeled
    assert any(r.get("resolved_kind") == "research" for r in labeled)

    fixed = client.post(
        "/api/tracking/labels/correct",
        json={
            "session_id": session_id,
            "event_id": target,
            "kind": "build",
            "auto_kind": "research",
        },
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["kind"] == "build"

    with user_context(uid) as ledger:
        assert ledger.latest_kind_for(target, session_id) == "build"
        feedback = [
            e
            for e in ledger.list_events(session_id=session_id, limit=50)
            if e.get("type") == "label_feedback"
        ]
        assert feedback


def test_restricted_events_hidden(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    me = _signup_and_login(client, email="secret@example.com")
    uid = me["user_id"]

    with user_context(uid) as ledger:
        session = ledger.start_session(
            title="Secret",
            surface="notes",
            sensitivity="restricted",
        )
        ledger.add_note("top secret note", session_id=session.session_id)
        ledger.end_session(session_id=session.session_id, tags=["neutral"])
        open_sess = ledger.start_session(
            title="Open", surface="notes", sensitivity="internal"
        )
        ledger.add_note("visible note", session_id=open_sess.session_id)

    sessions = client.get("/api/tracking/sessions?limit=20").json()["sessions"]
    titles = {s["title"] for s in sessions}
    assert "Open" in titles
    assert "Secret" not in titles

    events = client.get("/api/tracking/events?limit=50").json()["events"]
    assert all(e.get("sensitivity") != "restricted" for e in events)
    assert any(
        (e.get("payload") or {}).get("text") == "visible note"
        or "visible" in str((e.get("payload") or {}))
        for e in events
    ) or any(e.get("type") == "note" for e in events)


def test_people_room_capture_lands_in_tracking(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    me = _signup_and_login(client, email="rooms@example.com", name="Nat")
    uid = me["user_id"]

    created = client.post("/api/rooms", json={"title": "Tag Room", "name": "Nat"})
    assert created.status_code == 200, created.text
    room_id = created.json()["room_id"]

    selected = client.post("/api/rooms/select", json={"room_id": room_id})
    assert selected.status_code == 200, selected.text

    sent = client.post(
        "/api/messages",
        json={"body": "we should look into Acme AI"},
    )
    assert sent.status_code == 200, sent.text

    with user_context(uid) as ledger:
        threads = ledger.list_chat_threads()
        messenger_threads = [
            t
            for t in threads
            if str(t.get("desk_tag") or "").startswith("chat:messenger:")
        ]
        assert messenger_threads, f"expected messenger capture thread, got {threads}"
        labels = [e for e in ledger.list_events(limit=100) if e.get("type") == "label"]
        assert labels, "expected kind label from people-room capture"
        assert any(
            any(
                str(lbl).startswith("kind:")
                for lbl in (e.get("payload") or {}).get("labels") or []
            )
            for e in labels
        ), labels


def test_agent_messages_include_resolved_kind(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    me = _signup_and_login(client, email="agentkind@example.com")
    uid = me["user_id"]

    with user_context(uid) as ledger:
        thread = ledger.get_or_create_chat_thread(master=True)
        msg = ledger.append_chat_message(
            thread.session_id, role="user", content="how do we build the tagging UI"
        )
        ledger.record_ask_labels(
            thread.session_id,
            ["kind:question"],
            source="chat_classify",
            meta={"target_event_id": msg.event_id},
        )
        thread_id = thread.session_id

    resp = client.get(f"/api/agent-chats/messages?thread_id={thread_id}")
    assert resp.status_code == 200, resp.text
    messages = resp.json()["messages"]
    assert any(m.get("resolved_kind") == "question" for m in messages)


def test_classify_pending_endpoint(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    me = _signup_and_login(client, email="sweep@example.com")
    uid = me["user_id"]

    with user_context(uid) as ledger:
        thread = ledger.get_or_create_chat_thread(master=True)
        ledger.append_chat_message(
            thread.session_id,
            role="user",
            content="we should look into Tesla earnings",
        )

    # Deterministic path should classify without Qwen.
    resp = client.post("/api/tracking/classify-pending", json={"limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("classified", 0) >= 1


def test_classify_sweep_tick(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MESSENGER_USERS_DIR", str(tmp_path / "users"))
    from messenger.scheduler import ClassifySweep

    with user_context("sweep-user") as ledger:
        thread = ledger.get_or_create_chat_thread(master=True)
        ledger.append_chat_message(
            thread.session_id,
            role="user",
            content="we should look into Acme AI",
        )

    sweep = ClassifySweep(list_user_ids=lambda: ["sweep-user"], interval_seconds=60)
    results = sweep.tick()
    assert results
    assert results[0].get("classified", 0) >= 1
