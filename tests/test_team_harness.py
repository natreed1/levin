"""Hire → Teams → Harness Phase 1: config, draft-from-prompt, workflow posts."""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def _signup_and_login(client, *, email: str = "harness@example.com"):
    created = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "password12", "display_name": "Harness"},
    )
    assert created.status_code == 200, created.text
    token = created.json()["dev_verify_url"].split("token=")[-1]
    verified = client.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    logged = client.post(
        "/api/auth/login",
        json={"email": email, "password": "password12"},
    )
    assert logged.status_code == 200, logged.text


def test_harness_config_roles_orchestrator_and_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client)

    room = client.post("/api/rooms", json={"title": "Store UI", "name": "Harness"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]

    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen-bull"})
    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen-bear"})

    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Ship the storefront",
            "prompts": "Prefer primary sources",
            "skills": ["sec_filings_check", "note_digest"],
            "orchestrator": "workflow",
            "agents": ["qwen-bull", "qwen-bear"],
            "roles": {"qwen-bull": "SEO research", "qwen-bear": "UI critique"},
        },
    )
    assert patched.status_code == 200, patched.text
    cfg = patched.json()["config"]
    assert cfg["orchestrator"] == "workflow"
    assert cfg["roles"]["qwen-bull"] == "SEO research"
    assert cfg["skills"] == ["sec_filings_check", "note_digest"]
    assert set(cfg["agents"]) == {"qwen-bull", "qwen-bear"}

    loop = client.post(
        "/api/automations/from-chat",
        json={
            "name": "store_filings_loop",
            "steps": ["sec_filings_check", "note_digest"],
            "room_id": room_id,
            "schedule": "0 7 * * 1-5",
        },
    )
    assert loop.status_code == 200, loop.text
    assert loop.json()["ritual_id"] == "store_filings_loop"

    autos = client.get("/api/automations")
    assert autos.status_code == 200, autos.text
    bound = [a for a in autos.json()["automations"] if a.get("room_id") == room_id]
    assert any(a.get("ritual_id") == "store_filings_loop" for a in bound)


def test_draft_from_prompt_allowlist_and_out_of_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="draft@example.com")

    ok = client.post(
        "/api/registry/capabilities/draft-from-prompt",
        json={"description": "Scan SEC filings for NVDA material changes", "stub": True},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["in_scope"] is True
    assert body["capability"]["approved"] is False
    assert body["capability"]["executable"] is True
    assert "sec_filings_check" in (body.get("mapped_from") or [])

    oos = client.post(
        "/api/registry/capabilities/draft-from-prompt",
        json={
            "description": "Source products off Alibaba and track cargo shipments overseas",
            "stub": True,
        },
    )
    assert oos.status_code == 200, oos.text
    assert oos.json()["in_scope"] is False
    assert oos.json()["needs_script"] is True

    caps = client.get("/api/registry/capabilities")
    assert caps.status_code == 200, caps.text
    rows = caps.json()["capabilities"]
    by_id = {c["id"]: c for c in rows}
    assert by_id["sec_filings_check"]["executable"] is True
    assert by_id["web_research"]["executable"] is True
    # Draft we just created should be present and not yet approved
    drafts = [c for c in rows if c.get("proposed_by") == "hire_describe"]
    assert drafts
    assert drafts[0]["approved"] is False


def test_workflow_harness_posts_to_team_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="workflow@example.com")

    room = client.post("/api/rooms", json={"title": "Filings desk", "name": "WF"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]

    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Keep filings current",
            "skills": ["sec_filings_check"],
            "orchestrator": "workflow",
            "agents": ["qwen"],
        },
    )
    assert patched.status_code == 200, patched.text
    client.post(f"/api/rooms/{room_id}/agents", json={"agent_id": "qwen"})

    run = client.post(
        f"/api/rooms/{room_id}/harness-run",
        json={"stub": True},
    )
    assert run.status_code == 200, run.text
    assert run.json()["job"]["action"] == "workflow"

    # Wait briefly for background stub posts
    deadline = time.time() + 5
    bodies = []
    while time.time() < deadline:
        msgs = client.get(f"/api/messages?room_id={room_id}")
        assert msgs.status_code == 200, msgs.text
        bodies = [m.get("body") or "" for m in msgs.json().get("messages") or []]
        if any("Workflow" in b or "sec_filings_check" in b or "Harness" in b for b in bodies):
            break
        time.sleep(0.15)
    assert any(
        "Workflow" in b or "sec_filings_check" in b or "Harness" in b for b in bodies
    ), bodies

    auto = client.post(
        f"/api/rooms/{room_id}/autonomy",
        json={"enabled": True, "stub": True},
    )
    # May be busy from prior harness-run; either starts or reports busy/already
    assert auto.status_code in {200, 400}, auto.text


def test_graph_config_layers_and_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="graph@example.com")
    room = client.post("/api/rooms", json={"title": "Graph Desk", "name": "Graph"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]
    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Research then draft",
            "orchestrator": "workflow",
            "agents": ["qwen-bull", "qwen-bear"],
            "graph": {
                "layers": [
                    {
                        "id": "L1",
                        "title": "Research",
                        "prompt": "Gather filings",
                        "goal": "Sources listed",
                        "members": [{"agent_id": "qwen-bull", "instructions": "Prefer 10-Ks"}],
                    },
                    {
                        "id": "L2",
                        "title": "Draft",
                        "prompt": "Write memo",
                        "goal": "Memo ready",
                        "members": ["qwen-bear"],
                    },
                ],
                "guards": [
                    {
                        "id": "G1",
                        "from_layer_id": "L1",
                        "to_layer_id": "L2",
                        "prompt": "Is coverage deep enough to draft?",
                    }
                ],
            },
        },
    )
    assert patched.status_code == 200, patched.text
    graph = patched.json()["config"]["graph"]
    assert len(graph["layers"]) == 2
    assert graph["layers"][0]["members"][0]["instructions"] == "Prefer 10-Ks"
    assert graph["guards"][0]["prompt"] == "Is coverage deep enough to draft?"


def test_graph_run_walks_layers_into_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="graphrun@example.com")
    room = client.post("/api/rooms", json={"title": "Graph Run", "name": "GR"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]
    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Scope then research",
            "orchestrator": "workflow",
            "agents": ["qwen-bull"],
            "graph": {
                "layers": [
                    {
                        "id": "L1",
                        "title": "Define scope",
                        "prompt": "Write a rules document",
                        "goal": "Scope defined",
                        "members": [{"agent_id": "qwen-bull", "instructions": "Be brief"}],
                    },
                    {
                        "id": "L2",
                        "title": "Collect sources",
                        "prompt": "List sources",
                        "goal": "Sources ready",
                        "members": ["qwen-bull"],
                    },
                ],
                "guards": [
                    {
                        "id": "G1",
                        "from_layer_id": "L1",
                        "to_layer_id": "L2",
                        "prompt": "Has a scope document been created?",
                    }
                ],
            },
        },
    )
    assert patched.status_code == 200, patched.text

    run = client.post(
        f"/api/rooms/{room_id}/harness-run",
        json={"stub": True},
    )
    assert run.status_code == 200, run.text
    assert run.json()["job"]["action"] == "graph"

    deadline = time.time() + 6
    bodies = []
    while time.time() < deadline:
        msgs = client.get(f"/api/messages?room_id={room_id}")
        assert msgs.status_code == 200, msgs.text
        bodies = [m.get("body") or "" for m in msgs.json().get("messages") or []]
        if any("Graph finished" in b for b in bodies):
            break
        time.sleep(0.15)
    joined = "\n".join(bodies)
    assert "Graph started" in joined or "Layer 1" in joined, bodies
    assert "Define scope" in joined or "Layer 1" in joined, bodies
    assert "Guard" in joined or "Guard passed" in joined or "Graph finished" in joined, bodies
    assert any("Graph finished" in b for b in bodies), bodies


def test_graph_run_accepts_focus_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client(tmp_path, monkeypatch)
    _signup_and_login(client, email="graphfocus@example.com")
    room = client.post("/api/rooms", json={"title": "Focus Run", "name": "FR"})
    assert room.status_code == 200, room.text
    room_id = room.json()["room_id"]
    patched = client.patch(
        f"/api/rooms/{room_id}/config",
        json={
            "objective": "Generic research Graph",
            "orchestrator": "workflow",
            "agents": ["qwen-bull"],
            "graph": {
                "layers": [
                    {
                        "id": "L1",
                        "title": "Research",
                        "prompt": "Investigate the topic",
                        "goal": "Notes ready",
                        "members": ["qwen-bull"],
                    }
                ],
                "guards": [],
            },
        },
    )
    assert patched.status_code == 200, patched.text

    run = client.post(
        f"/api/rooms/{room_id}/harness-run",
        json={"stub": True, "focus": "NVDA data-center demand"},
    )
    assert run.status_code == 200, run.text
    assert run.json()["job"]["topic"] == "NVDA data-center demand"

    deadline = time.time() + 6
    bodies = []
    while time.time() < deadline:
        msgs = client.get(f"/api/messages?room_id={room_id}")
        assert msgs.status_code == 200, msgs.text
        bodies = [m.get("body") or "" for m in msgs.json().get("messages") or []]
        if any("Graph finished" in b for b in bodies):
            break
        time.sleep(0.15)
    joined = "\n".join(bodies)
    assert "Focus: NVDA data-center demand" in joined, bodies
    assert any("Graph finished" in b for b in bodies), bodies


def test_graph_guard_rejection_continues_layer(monkeypatch: pytest.MonkeyPatch):
    """Rejected guards re-run the same step instead of pausing the Graph."""
    from messenger import team_harness as th
    from messenger.specialist_room import SpecialistJob

    posts: list[str] = []

    def fake_post(store, hub, room_id, name, body, loop=None, **_kwargs):
        posts.append(str(body))

    calls = {"n": 0}

    def fake_guard(prompt, *, prior, layer_goal, stub):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "NO — coverage is thin."
        return True, "YES — enough to advance."

    monkeypatch.setattr(th, "_post", fake_post)
    monkeypatch.setattr(th, "_guard_allows", fake_guard)
    monkeypatch.setattr(
        th,
        "_recent_work_brief",
        lambda *_a, **_k: "",
        raising=False,
    )

    # Patch imports used inside _run_graph_layers
    import messenger.specialist_room as sr

    monkeypatch.setattr(sr, "_recent_work_brief", lambda *_a, **_k: "")
    monkeypatch.setattr(sr, "_room_guidance", lambda *_a, **_k: "")
    monkeypatch.setattr(
        sr,
        "_speak",
        lambda personality, **_k: f"{personality.name} stub turn",
    )

    class FakePersonality:
        def __init__(self, pid: str, name: str):
            self.id = pid
            self.name = name

    monkeypatch.setattr(
        "analyst_ledger.friend_personalities.resolve_specialists",
        lambda ids: [FakePersonality(i, i) for i in ids],
    )

    class FakeEndpointCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "analyst_ledger.synthesize.use_llm_endpoint",
        lambda *_a, **_k: FakeEndpointCtx(),
    )
    monkeypatch.setattr(
        "messenger.tenancy.user_context",
        lambda *_a, **_k: FakeEndpointCtx(),
    )

    job = SpecialistJob(
        job_id="job_test",
        room_id="r1",
        action="graph",
        topic="focus topic",
    )
    room = {
        "room_id": "r1",
        "config": {
            "objective": "Generic research",
            "graph": {
                "layers": [
                    {
                        "id": "L1",
                        "title": "Research",
                        "prompt": "Dig in",
                        "goal": "Notes",
                        "members": [{"agent_id": "qwen-bull"}],
                    },
                    {
                        "id": "L2",
                        "title": "Draft",
                        "prompt": "Write",
                        "goal": "Memo",
                        "members": [{"agent_id": "qwen-bull"}],
                    },
                ],
                "guards": [
                    {
                        "id": "G1",
                        "from_layer_id": "L1",
                        "to_layer_id": "L2",
                        "prompt": "Deep enough?",
                    }
                ],
            },
        },
    }

    th._run_graph_layers(
        store=None,
        hub=None,
        room=room,
        room_id="r1",
        config=room["config"],
        job=job,
        stub=True,
        loop=None,
        owner_user_id="u1",
        run_focus="NVDA AI demand",
    )

    joined = "\n".join(posts)
    assert "Focus: NVDA AI demand" in joined
    assert "Guard not satisfied — continuing" in joined
    assert "continue · try 2" in joined
    assert "Graph paused" not in joined
    assert "Graph finished" in joined
    assert job.status == "completed"
    assert calls["n"] == 2


_SEC_TRENDS = {
    "symbol": "NVDA",
    "entity": "NVIDIA CORP",
    "revenue": {
        "latest": {
            "val": 81_615_000_000,
            "start": "2026-01-26",
            "end": "2026-04-26",
            "filed": "2026-05-20",
            "accn": "0001045810-26-000123",
        },
        "prior": {"val": 44_062_000_000, "end": "2025-04-27"},
        "growth_pct": 85.24,
    },
    "eps": {
        "latest": {"val": 2.39, "end": "2026-04-26", "filed": "2026-05-20"},
        "prior": {"val": 0.76},
        "growth_pct": 214.47,
    },
    "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json",
}
_SEC_FILINGS = [
    {
        "form": "8-K",
        "filed": "2026-06-01",
        "accession": "0001045810-26-000200",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1045810/"
            "000104581026000200/nvda-8k.htm"
        ),
    },
    {
        "form": "10-Q",
        "filed": "2026-05-20",
        "accession": "0001045810-26-000123",
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1045810/"
            "000104581026000123/nvda-20260426.htm"
        ),
    },
]


def _patch_edgar(monkeypatch: pytest.MonkeyPatch, *, filings=None):
    """Offline EDGAR + Bing. Source modules are patched (imports are function-local)."""
    import analyst_ledger.finance_research as fr
    import analyst_ledger.web_search as ws

    monkeypatch.setattr(fr, "fetch_sec_trends", lambda symbol: dict(_SEC_TRENDS))
    monkeypatch.setattr(
        fr,
        "fetch_recent_filings",
        lambda symbol, limit=5: list(
            _SEC_FILINGS if filings is None else filings
        ),
    )
    monkeypatch.setattr(ws, "bing_search", lambda *_a, **_k: [])
    monkeypatch.setattr(ws, "enrich_trusted_hits", lambda hits, **_k: list(hits))


def test_edgar_sources_builds_facts_and_ordered_hits(monkeypatch: pytest.MonkeyPatch):
    from messenger.team_harness import _edgar_sources

    _patch_edgar(monkeypatch)
    block, hits = _edgar_sources("NVDA 10-Q gross margin")

    assert "SEC EDGAR facts for NVDA (NVIDIA CORP)" in block
    assert "$81.615B" in block
    assert "$44.062B" in block
    assert "+85.2%" in block
    assert "$2.39" in block
    assert "data.sec.gov" in block

    assert [h["title"] for h in hits] == [
        "NVDA 10-Q filed 2026-05-20",
        "NVDA 8-K filed 2026-06-01",
    ]
    assert all("sec.gov" in h["url"] for h in hits)


def test_retrieve_web_sources_returns_edgar_facts_without_bing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression: zero Bing hits used to yield an empty source pack."""
    from messenger.team_harness import _retrieve_web_sources

    _patch_edgar(monkeypatch)
    pack = _retrieve_web_sources("balanced NVDA research 10-Q gross margin")

    assert "$81.615B" in pack
    assert "www.sec.gov/Archives" in pack
    # Facts lead so they survive the prompt truncation downstream.
    assert pack.startswith("SEC EDGAR facts for NVDA")


def test_retrieve_web_sources_keeps_facts_when_no_hits_at_all(
    monkeypatch: pytest.MonkeyPatch,
):
    from messenger.team_harness import _retrieve_web_sources

    _patch_edgar(monkeypatch, filings=[])
    pack = _retrieve_web_sources("NVDA quarterly revenue")

    assert "$81.615B" in pack
    assert "+85.2%" in pack


def test_retrieve_web_sources_detects_filings_from_step_context(
    monkeypatch: pytest.MonkeyPatch,
):
    """The ticker may live in the step title/prompt, not the run focus."""
    from messenger.team_harness import _retrieve_web_sources

    _patch_edgar(monkeypatch, filings=[])
    assert _retrieve_web_sources("latest quarterly results") == ""
    with_context = _retrieve_web_sources(
        "latest quarterly results",
        context="Pull the latest NVDA 10-Q",
    )
    assert "SEC EDGAR facts for NVDA" in with_context


def test_retrieve_web_sources_excludes_edgar_from_page_fetch(
    monkeypatch: pytest.MonkeyPatch,
):
    """EDGAR primary docs are iXBRL tag soup — keep them as list lines but
    spend the page-fetch budget on prose sources."""
    import analyst_ledger.web_search as ws

    from messenger.team_harness import _retrieve_web_sources

    _patch_edgar(monkeypatch)
    monkeypatch.setattr(
        ws,
        "bing_search",
        lambda *_a, **_k: [
            {
                "title": "NVDA earnings coverage",
                "url": "https://www.reuters.com/nvda-earnings",
                "snippet": "Data-center revenue grew again.",
            }
        ],
    )

    fetched: list = []

    def record_enrich(hits, **_k):
        fetched.extend(str(h.get("url") or "") for h in hits)
        return list(hits)

    monkeypatch.setattr(ws, "enrich_trusted_hits", record_enrich)

    pack = _retrieve_web_sources("balanced NVDA research 10-Q gross margin")

    assert "www.sec.gov/Archives" in pack
    assert fetched
    assert all("sec.gov" not in url for url in fetched)


def test_retrieve_web_sources_stub_skips_network(monkeypatch: pytest.MonkeyPatch):
    from messenger.team_harness import _retrieve_web_sources

    def boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("stub must not touch the network")

    import analyst_ledger.finance_research as fr
    import analyst_ledger.web_search as ws

    monkeypatch.setattr(fr, "fetch_sec_trends", boom)
    monkeypatch.setattr(ws, "bing_search", boom)
    assert "(stub) Sources for" in _retrieve_web_sources("NVDA 10-Q", stub=True)


def test_graph_guard_exhaustion_flags_evidence_gap(monkeypatch: pytest.MonkeyPatch):
    """Guard exhaustion flags the run instead of silently passing it."""
    from messenger import team_harness as th
    from messenger.specialist_room import SpecialistJob

    posts: list[str] = []

    def fake_post(store, hub, room_id, name, body, loop=None, **_kwargs):
        posts.append(str(body))

    calls = {"n": 0}

    def fake_guard(prompt, *, prior, layer_goal, stub):
        calls["n"] += 1
        return False, "NO — no evidence was retrieved."

    monkeypatch.setattr(th, "_post", fake_post)
    monkeypatch.setattr(th, "_guard_allows", fake_guard)

    import messenger.specialist_room as sr

    monkeypatch.setattr(sr, "_recent_work_brief", lambda *_a, **_k: "")
    monkeypatch.setattr(sr, "_room_guidance", lambda *_a, **_k: "")
    monkeypatch.setattr(
        sr,
        "_speak",
        lambda personality, **_k: f"{personality.name} stub turn",
    )

    class FakePersonality:
        def __init__(self, pid: str, name: str):
            self.id = pid
            self.name = name

    monkeypatch.setattr(
        "analyst_ledger.friend_personalities.resolve_specialists",
        lambda ids: [FakePersonality(i, i) for i in ids],
    )

    class FakeEndpointCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "analyst_ledger.synthesize.use_llm_endpoint",
        lambda *_a, **_k: FakeEndpointCtx(),
    )
    monkeypatch.setattr(
        "messenger.tenancy.user_context",
        lambda *_a, **_k: FakeEndpointCtx(),
    )

    job = SpecialistJob(
        job_id="job_gap",
        room_id="r1",
        action="graph",
        topic="focus topic",
    )
    room = {
        "room_id": "r1",
        "config": {
            "objective": "Generic research",
            "graph": {
                "layers": [
                    {
                        "id": "L1",
                        "title": "Gather",
                        "prompt": "Dig in",
                        "goal": "Notes",
                        "members": [{"agent_id": "qwen-bull"}],
                    },
                    {
                        "id": "L2",
                        "title": "Synthesize",
                        "prompt": "Write",
                        "goal": "Memo",
                        "members": [{"agent_id": "qwen-bull"}],
                    },
                ],
                "guards": [
                    {
                        "id": "G1",
                        "from_layer_id": "L1",
                        "to_layer_id": "L2",
                        "prompt": "Any real evidence?",
                    }
                ],
            },
        },
    }

    th._run_graph_layers(
        store=None,
        hub=None,
        room=room,
        room_id="r1",
        config=room["config"],
        job=job,
        stub=True,
        loop=None,
        owner_user_id="u1",
        run_focus="NVDA AI demand",
    )

    joined = "\n".join(posts)
    assert "**Evidence gap**" in joined
    assert "Gather: guard unsatisfied after 3 tries" in joined
    assert "flagging this step as an evidence gap" in joined
    assert "Graph finished" in joined
    assert job.status == "completed"
    assert calls["n"] == th._GRAPH_LAYER_MAX_ATTEMPTS


def test_web_search_query_strips_make_imperative():
    from messenger.team_harness import _web_search_query_from_focus

    assert (
        _web_search_query_from_focus("make a ww2 history essay")
        == "ww2 history essay"
    )
    assert (
        _web_search_query_from_focus("write an essay about the New Deal")
        == "essay about the New Deal"
    )
    assert _web_search_query_from_focus("NVDA AI demand") == "NVDA AI demand"
