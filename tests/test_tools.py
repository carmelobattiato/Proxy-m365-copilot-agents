"""Tests for tool-call emulation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from lmgateway.server import create_app
from lmgateway.config import Settings
from lmgateway.schemas import OpenAIChatRequest
from lmgateway.tools import parse_tool_calls, render_tool_calls, tool_instructions, tool_names
from lmgateway.adapter import translate_openai_request
from lmgateway.upstream import GatewayUpstreamError

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the filesystem",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}]

_TOOL_REPLY = 'Reading now.\n<tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>'


class ScriptedClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    async def chat(self, prompt: str, ctx: list[str], session=None) -> str:
        self.prompts.append(prompt)
        return self.reply

    async def chat_stream(self, prompt: str, ctx: list[str], session=None) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        half = len(self.reply) // 2
        yield self.reply[:half]
        yield self.reply[half:]


def _build(reply: str, **kwargs) -> tuple[TestClient, ScriptedClient]:
    fake = ScriptedClient(reply)
    settings = Settings(LMG_ACCESS_TOKEN="fake", **kwargs)
    app = create_app(settings=settings, client_factory=lambda: fake)
    return TestClient(app), fake


def _body(**extra) -> dict:
    body: dict = {"model": "lm-gateway", "messages": [{"role": "user", "content": "read a.txt"}]}
    body.update(extra)
    return body


# ── Unit tests ────────────────────────────────────────────────────────────────

def test_tool_emulation_on_by_default():
    assert Settings(LMG_ACCESS_TOKEN="x").tool_emulation is True


def test_tool_emulation_disabled_via_env():
    assert Settings(LMG_ACCESS_TOKEN="x", LMG_TOOL_EMULATION="false").tool_emulation is False


def test_tool_names_extraction():
    assert tool_names(TOOLS) == {"read_file"}


def test_parse_tool_calls_keeps_known_and_filters_unknown():
    text = (
        'Ok.\n<tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>\n'
        '<tool_call>{"name": "rm_everything", "arguments": {}}</tool_call>'
    )
    remaining, calls = parse_tool_calls(text, {"read_file"})
    assert remaining == "Ok."
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}


def test_parse_tool_calls_stray_angle_bracket():
    _, calls = parse_tool_calls('<<tool_call>{"name": "read_file", "arguments": {}}</tool_call>', {"read_file"})
    assert len(calls) == 1


def test_parse_tool_calls_invalid_json():
    remaining, calls = parse_tool_calls("<tool_call>{not json}</tool_call>", {"read_file"})
    assert calls == []
    assert remaining == ""


def test_tool_instructions_content():
    instructions = tool_instructions(TOOLS)
    assert "read_file" in instructions
    assert "<tool_call>" in instructions
    assert "path: string" in instructions


def test_translator_tool_loop():
    req = OpenAIChatRequest.model_validate(_body(messages=[
        {"role": "user", "content": "read a.txt"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
    ]))
    translated = translate_openai_request(req, tool_instructions(TOOLS))
    assert translated.prompt.startswith("You are the reasoning engine")
    assert "Tool result (c1): file contents" in translated.prompt
    transcript = next(c for c in translated.additional_context if "transcript" in c)
    assert "<tool_call>" in transcript


# ── Integration tests ─────────────────────────────────────────────────────────

def test_chat_returns_tool_calls():
    client, _ = _build(_TOOL_REPLY)
    r = client.post("/v1/chat/completions", json=_body(tools=TOOLS))
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Reading now."
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "read_file"


def test_chat_injects_tool_instructions():
    client, fake = _build(_TOOL_REPLY)
    client.post("/v1/chat/completions", json=_body(tools=TOOLS))
    assert fake.prompts[0].startswith("You are the reasoning engine")
    assert "read_file(path: string)" in fake.prompts[0]


def test_chat_without_tools_unchanged():
    client, fake = _build(_TOOL_REPLY)
    r = client.post("/v1/chat/completions", json=_body())
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == _TOOL_REPLY
    assert "reasoning engine" not in fake.prompts[0]


def test_tool_emulation_disabled_ignores_tools():
    client, fake = _build(_TOOL_REPLY, LMG_TOOL_EMULATION="false")
    r = client.post("/v1/chat/completions", json=_body(tools=TOOLS))
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "reasoning engine" not in fake.prompts[0]


def test_streaming_emits_tool_calls_chunk():
    client, _ = _build(_TOOL_REPLY)
    r = client.post("/v1/chat/completions", json=_body(tools=TOOLS, stream=True))
    events = [
        json.loads(line[len("data: "):])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    tool_deltas = [
        e["choices"][0]["delta"]["tool_calls"]
        for e in events
        if "tool_calls" in e["choices"][0]["delta"]
    ]
    assert r.text.rstrip().endswith("data: [DONE]")
    assert tool_deltas[0][0]["function"]["name"] == "read_file"
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"