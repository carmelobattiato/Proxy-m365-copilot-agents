"""Tests for the gateway HTTP server endpoints."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from lmgateway.server import create_app
from lmgateway.cli import (
    _find_gateway_page,
    _is_valid_token,
    _needs_token,
    _read_token,
    _seconds_remaining,
    _write_token,
)
from lmgateway.config import Settings
from lmgateway.sessions import PersistentSession, SessionStore
from lmgateway.upstream import GatewayUpstreamError, UpstreamClient


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeUpstreamClient:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self.sessions: list[object] = []

    async def chat(self, prompt: str, ctx: list[str], session=None) -> str:
        self.calls.append((prompt, ctx))
        self.sessions.append(session)
        return "gateway reply"

    async def chat_stream(self, prompt: str, ctx: list[str], session=None) -> AsyncIterator[str]:
        self.calls.append((prompt, ctx))
        self.sessions.append(session)
        yield "hello"
        yield " world"


class FailingStreamClient(FakeUpstreamClient):
    async def chat_stream(self, prompt: str, ctx: list[str], session=None) -> AsyncIterator[str]:
        self.calls.append((prompt, ctx))
        self.sessions.append(session)
        raise GatewayUpstreamError("upstream broke")
        yield ""  # noqa: unreachable


def _build(fake: FakeUpstreamClient, **kwargs) -> TestClient:
    settings = Settings(LMG_ACCESS_TOKEN="fake-token", **kwargs)
    app = create_app(settings=settings, client_factory=lambda: fake)
    return TestClient(app)


def _jwt(exp: int, aud: str = "https://substrate.office.com/sydney") -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(d, separators=(",", ":")).encode()
        ).decode().rstrip("=")
    return f"{enc({'alg':'none'})}.{enc({'aud': aud, 'exp': exp, 'oid': 'o', 'tid': 't'})}.sig"


# ── Meta endpoints ────────────────────────────────────────────────────────────

def test_models_endpoint():
    client = _build(FakeUpstreamClient())
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "lm-gateway"


def test_app_starts_without_token():
    app = create_app(settings=Settings(LMG_ACCESS_TOKEN=""))
    r = TestClient(app).get("/v1/token/status")
    assert r.status_code == 200
    assert r.json()["valid"] is False


def test_token_status_reports_expiry():
    settings = Settings(LMG_ACCESS_TOKEN=_jwt(int(time.time()) + 3600))
    app = create_app(settings=settings, client_factory=lambda: FakeUpstreamClient())
    r = TestClient(app).get("/v1/token/status")
    body = r.json()
    assert body["valid"] is True
    assert body["seconds_remaining"] > 0


def test_healthz_includes_token_status():
    settings = Settings(LMG_ACCESS_TOKEN=_jwt(int(time.time()) + 3600))
    app = create_app(settings=settings, client_factory=lambda: FakeUpstreamClient())
    r = TestClient(app).get("/healthz")
    body = r.json()
    assert body["status"] == "ok"
    assert body["token"]["valid"] is True


def test_token_status_rejects_wrong_audience():
    settings = Settings(LMG_ACCESS_TOKEN=_jwt(int(time.time()) + 3600, aud="wrong-aud"))
    app = create_app(settings=settings, client_factory=lambda: FakeUpstreamClient())
    r = TestClient(app).get("/v1/token/status")
    assert r.json()["valid"] is False


# ── Token hot-reload ──────────────────────────────────────────────────────────

def test_default_factory_hot_reloads_token(tmp_path, monkeypatch):
    first = _jwt(int(time.time()) + 3600)
    second = _jwt(int(time.time()) + 7200)
    env = tmp_path / ".env"
    env.write_text(f"LMG_ACCESS_TOKEN={first}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    seen: list[str] = []

    class RecordingClient(FakeUpstreamClient):
        def __init__(self, access_token: str, **_):
            super().__init__()
            seen.append(access_token)

    monkeypatch.setattr("lmgateway.server.UpstreamClient", RecordingClient)
    app = create_app(settings=Settings(LMG_ACCESS_TOKEN=first))
    client = TestClient(app)

    time.sleep(0.01)
    env.write_text(f"LMG_ACCESS_TOKEN={second}\n", encoding="utf-8")
    r = client.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert seen == [second]


# ── CLI helpers ───────────────────────────────────────────────────────────────

def test_cli_reads_token_from_env(tmp_path, monkeypatch):
    token = _jwt(int(time.time()) + 3600)
    (tmp_path / ".env").write_text(f"LMG_ACCESS_TOKEN='{token}'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _read_token() == token


def test_cli_write_token_skips_commented_line(tmp_path, monkeypatch):
    token = _jwt(int(time.time()) + 3600)
    env = tmp_path / ".env"
    env.write_text("# LMG_ACCESS_TOKEN=old\nOTHER=value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _write_token(token)
    assert _read_token() == token
    assert env.read_text(encoding="utf-8").count("LMG_ACCESS_TOKEN=") == 2


def test_cli_seconds_remaining():
    assert 0 < _seconds_remaining(_jwt(int(time.time()) + 3600)) <= 3600


def test_cli_accepts_only_valid_tokens():
    assert _is_valid_token(_jwt(int(time.time()) + 3600))
    assert not _is_valid_token(_jwt(int(time.time()) + 3600, aud="wrong"))


def test_cli_needs_token_logic():
    assert _needs_token(None)
    assert _needs_token(_jwt(int(time.time()) + 3600, aud="wrong"))
    assert _needs_token(_jwt(int(time.time()) - 1))
    assert not _needs_token(_jwt(int(time.time()) + 3600))


def test_cli_finds_correct_page():
    tabs = [
        {"type": "page", "url": "devtools://devtools?remoteBase=https://m365.cloud.microsoft/chat"},
        {"type": "page", "url": "https://m365.cloud.microsoft/chat"},
    ]
    assert _find_gateway_page(tabs) == tabs[1]


# ── Chat Completions ──────────────────────────────────────────────────────────

def test_chat_completion_translates_history():
    fake = FakeUpstreamClient()
    client = _build(fake)
    r = client.post("/v1/chat/completions", json={
        "model": "lm-gateway",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ],
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "gateway reply"
    assert fake.calls == [(
        "Q2",
        [
            "System instructions:\nBe concise.",
            "Prior conversation transcript:\nUser: Q1\nAssistant: A1",
        ],
    )]


def test_persistent_session_header_reuses_session():
    fake = FakeUpstreamClient()
    client = _build(fake, LMG_INCOGNITO="false")
    body = {"model": "lm-gateway", "messages": [{"role": "user", "content": "hi"}]}
    client.post("/v1/chat/completions", headers={"X-LMG-Session-Id": "ws1"}, json=body)
    client.post("/v1/chat/completions", headers={"X-LMG-Session-Id": "ws1"}, json=body)
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not None


def test_persistent_model_suffix_uses_user_as_key():
    fake = FakeUpstreamClient()
    client = _build(fake, LMG_INCOGNITO="false")
    for user in ("alice", "alice", "bob"):
        client.post("/v1/chat/completions", json={
            "model": "lm-gateway:persist",
            "user": user,
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not fake.sessions[2]


def test_incognito_mode_uses_fixed_session(monkeypatch):
    """With LMG_INCOGNITO=true (default) stateless calls share a ghost session."""
    from lmgateway.sessions import IncognitoSession
    fake = FakeUpstreamClient()
    client = _build(fake)  # incognito=True by default
    body = {"model": "lm-gateway", "messages": [{"role": "user", "content": "hi"}]}
    client.post("/v1/chat/completions", json=body)
    client.post("/v1/chat/completions", json=body)
    assert all(isinstance(s, IncognitoSession) for s in fake.sessions)


def test_fresh_suffix_bypasses_incognito():
    """lm-gateway:fresh forces a new UUID even when incognito is on."""
    fake = FakeUpstreamClient()
    client = _build(fake)  # incognito=True by default
    body = {"model": "lm-gateway:fresh", "messages": [{"role": "user", "content": "hi"}]}
    client.post("/v1/chat/completions", json=body)
    assert fake.sessions[0] is None


def test_persistent_session_turn_order():
    store = SessionStore()
    session = store.get("k")
    t1 = session.reserve_turn()
    t2 = session.reserve_turn()
    assert t1.conversation_id == t2.conversation_id
    assert t1.is_start_of_session is True
    assert t2.is_start_of_session is False


def test_openai_streaming_sse():
    fake = FakeUpstreamClient()
    client = _build(fake)
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "lm-gateway", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as r:
        payload = "".join(r.iter_text())
    assert r.status_code == 200
    assert '"role": "assistant"' in payload
    assert '"content": "hello"' in payload
    assert "data: [DONE]" in payload


def test_streaming_error_event_on_upstream_failure():
    client = _build(FailingStreamClient())
    with client.stream("POST", "/v1/chat/completions", json={
        "model": "lm-gateway", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as r:
        payload = "".join(r.iter_text())
    assert '"type": "upstream_error"' in payload
    assert "data: [DONE]" in payload


# ── Responses API ─────────────────────────────────────────────────────────────

def test_responses_requires_final_user_message():
    client = _build(FakeUpstreamClient())
    r = client.post("/v1/responses", json={
        "model": "lm-gateway",
        "input": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "there"},
        ],
    })
    assert r.status_code == 400


def test_responses_streaming_error_event():
    client = _build(FailingStreamClient())
    with client.stream("POST", "/v1/responses", json={
        "model": "lm-gateway", "stream": True, "input": "hi",
    }) as r:
        payload = "".join(r.iter_text())
    assert '"type": "error"' in payload


# ── Anthropic Messages ────────────────────────────────────────────────────────

def test_anthropic_messages_endpoint():
    fake = FakeUpstreamClient()
    client = _build(fake)
    r = client.post("/v1/messages", json={
        "model": "lm-gateway",
        "system": "Be concise.",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "gateway reply"


def test_anthropic_streaming_error_event():
    client = _build(FailingStreamClient())
    with client.stream("POST", "/v1/messages", json={
        "model": "lm-gateway", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as r:
        payload = "".join(r.iter_text())
    assert "event: error" in payload