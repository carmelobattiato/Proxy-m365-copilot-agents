"""
FastAPI application factory.

Exposes an OpenAI-compatible (and Anthropic-compatible) HTTP API that
proxies requests to the upstream enterprise LLM service.

Endpoints
---------
GET  /healthz                 — liveness + token status
GET  /v1/token/status         — token validity and expiry details
GET  /v1/models               — list available model names
POST /v1/chat/completions     — OpenAI Chat Completions (streaming supported)
POST /v1/responses            — OpenAI Responses API (streaming supported)
POST /v1/messages             — Anthropic Messages API (streaming supported)

Session modes (per request)
---------------------------
Header X-LMG-Session-Id: <key>   → named persistent session (multi-turn memory)
Model suffix :persist             → persistent session keyed by user field
Model suffix :fresh               → force new conversation (bypass incognito)
LMG_INCOGNITO=true (default)     → incognito ghost session (no history spam)
LMG_INCOGNITO=false              → stateless (new UUID per request, original behaviour)
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .adapter import translate_anthropic_request, translate_openai_request, translate_responses_request
from .auth import TokenStore
from .config import Settings
from .model_opts import ModelOptions, model_names, parse_model_options
from .schemas import AnthropicMessagesRequest, OpenAIChatRequest, OpenAIResponsesRequest
from .sessions import AnySession, IncognitoSession, SessionStore
from .tools import parse_tool_calls, tool_instructions, tool_names
from .upstream import GatewayUpstreamError, UpstreamClient

_SESSION_HEADER = "x-lmg-session-id"


# ── Application factory ───────────────────────────────────────────────────────

def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[], UpstreamClient] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="LM Gateway",
        description=(
            "OpenAI-compatible local gateway for enterprise LLM subscriptions. "
            "Supports Chat Completions, Responses, and Anthropic Messages APIs."
        ),
        version="1.0.0",
    )

    cfg = settings or Settings()
    app.state.settings = cfg
    app.state.token_store = TokenStore(cfg.access_token)
    app.state.session_store = SessionStore()
    app.state.client_factory = client_factory or (
        lambda: UpstreamClient(
            access_token=app.state.token_store.get(),
            time_zone=cfg.time_zone,
            tone=cfg.tone,
            ws_timeout=cfg.ws_timeout,
        )
    )

    # ── Dependency helpers ────────────────────────────────────────────────────

    def get_settings() -> Settings:
        return app.state.settings

    def get_client() -> UpstreamClient:
        return app.state.client_factory()

    # ── Health & meta endpoints ───────────────────────────────────────────────

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"status": "ok", "token": app.state.token_store.status()}

    @app.get("/v1/token/status", tags=["meta"])
    async def token_status() -> dict:
        return app.state.token_store.status()

    @app.get("/v1/models", tags=["models"])
    async def list_models(cfg: Settings = Depends(get_settings)) -> dict:
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "lm-gateway"}
                for name in model_names(cfg.model_alias)
            ],
        }

    # ── OpenAI Chat Completions ───────────────────────────────────────────────

    @app.post("/v1/chat/completions", tags=["chat"])
    async def chat_completions(
        raw: Request,
        request: OpenAIChatRequest,
        cfg: Settings = Depends(get_settings),
        client: UpstreamClient = Depends(get_client),
    ):
        emulate = cfg.tool_emulation and bool(request.tools)
        known = tool_names(request.tools) if emulate else None
        opts = parse_model_options(request.model)
        upstream = _client_for_tone(app, client, opts.tone, cfg)

        try:
            translated = translate_openai_request(
                request,
                tool_instructions(request.tools) if emulate else None,
            )
            session = _resolve_session(app, raw, request.model, request.user, opts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _stream_openai(cfg.model_alias, upstream, translated.prompt,
                               translated.additional_context, session, known),
                media_type="text/event-stream",
            )

        try:
            text = await upstream.chat(translated.prompt, translated.additional_context, session)
        except GatewayUpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        message: dict = {"role": "assistant", "content": text}
        finish_reason = "stop"
        if known is not None:
            content, calls = parse_tool_calls(text, known)
            if calls:
                message = {"role": "assistant", "content": content or None, "tool_calls": calls}
                finish_reason = "tool_calls"

        return JSONResponse({
            "id": f"chatcmpl_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": cfg.model_alias,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        })

    # ── OpenAI Responses ──────────────────────────────────────────────────────

    @app.post("/v1/responses", tags=["chat"])
    async def openai_responses(
        raw: Request,
        cfg: Settings = Depends(get_settings),
        client: UpstreamClient = Depends(get_client),
    ):
        body = await raw.json()
        try:
            request = OpenAIResponsesRequest.model_validate(body)
            translated = translate_responses_request(request)
            opts = parse_model_options(request.model)
            session = _resolve_session(app, raw, request.model, options=opts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _stream_responses(cfg.model_alias, client, translated.prompt,
                                  translated.additional_context, session),
                media_type="text/event-stream",
            )

        try:
            text = await client.chat(translated.prompt, translated.additional_context, session)
        except GatewayUpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse({
            "id": f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "created_at": int(time.time()),
            "model": cfg.model_alias,
            "output": [{
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        })

    # ── Anthropic Messages ────────────────────────────────────────────────────

    @app.post("/v1/messages", tags=["chat"])
    async def anthropic_messages(
        raw: Request,
        request: AnthropicMessagesRequest,
        cfg: Settings = Depends(get_settings),
        client: UpstreamClient = Depends(get_client),
    ):
        try:
            translated = translate_anthropic_request(request)
            opts = parse_model_options(request.model)
            session = _resolve_session(app, raw, request.model, options=opts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _stream_anthropic(cfg.model_alias, client, translated.prompt,
                                  translated.additional_context, session),
                media_type="text/event-stream",
            )

        try:
            text = await client.chat(translated.prompt, translated.additional_context, session)
        except GatewayUpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse({
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": cfg.model_alias,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    return app


# ── Session resolution ────────────────────────────────────────────────────────

def _resolve_session(
    app: FastAPI,
    raw: Request,
    model: str,
    fallback_key: str | None = None,
    options: ModelOptions | None = None,
) -> AnySession:
    """
    Determine which session type to use for this request.

    Priority (highest first)
    ------------------------
    1. X-LMG-Session-Id header  → named PersistentSession
    2. :persist model suffix     → PersistentSession keyed by user/default
    3. :fresh model suffix       → None (new UUID, bypasses incognito)
    4. LMG_INCOGNITO=true        → IncognitoSession (ghost conversation ID)
    5. LMG_INCOGNITO=false       → None (stateless, original behaviour)
    """
    if options is None:
        options = parse_model_options(model)

    header_key = (raw.headers.get(_SESSION_HEADER) or "").strip()
    if header_key:
        return app.state.session_store.get(f"header:{header_key}")

    if options.persist:
        return app.state.session_store.get(f"model:{fallback_key or 'default'}")

    if options.fresh:
        return None

    if app.state.settings.incognito:
        return IncognitoSession()

    return None


# ── Tone override ─────────────────────────────────────────────────────────────

def _client_for_tone(
    app: FastAPI,
    default_client: UpstreamClient,
    tone: str | None,
    cfg: Settings,
) -> UpstreamClient:
    if tone is None or tone == cfg.tone:
        return default_client
    return UpstreamClient(
        access_token=app.state.token_store.get(),
        time_zone=cfg.time_zone,
        tone=tone,
        ws_timeout=cfg.ws_timeout,
    )


# ── SSE chunk helpers ─────────────────────────────────────────────────────────

def _openai_chunk(cid: str, created: int, alias: str, delta: dict, finish: str | None) -> str:
    return f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': alias, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n"


# ── Streaming generators ──────────────────────────────────────────────────────

async def _stream_openai(
    alias: str,
    client: UpstreamClient,
    prompt: str,
    context: list[str],
    session: AnySession,
    known_tools: set[str] | None,
) -> AsyncIterator[str]:
    cid = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': alias, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"

    if known_tools is not None:
        # Buffer full response to detect tool_calls before streaming
        full = ""
        try:
            async for delta in client.chat_stream(prompt, context, session):
                full += delta
        except GatewayUpstreamError as exc:
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
            yield "data: [DONE]\n\n"
            return
        content, calls = parse_tool_calls(full, known_tools)
        if content:
            yield _openai_chunk(cid, created, alias, {"content": content}, None)
        if calls:
            yield _openai_chunk(cid, created, alias, {"tool_calls": [{"index": i, **c} for i, c in enumerate(calls)]}, None)
        yield _openai_chunk(cid, created, alias, {}, "tool_calls" if calls else "stop")
        yield "data: [DONE]\n\n"
        return

    try:
        async for delta in client.chat_stream(prompt, context, session):
            yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': alias, 'choices': [{'index': 0, 'delta': {'content': delta}, 'finish_reason': None}]})}\n\n"
    except GatewayUpstreamError as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    yield _openai_chunk(cid, created, alias, {}, "stop")
    yield "data: [DONE]\n\n"


async def _stream_responses(
    alias: str,
    client: UpstreamClient,
    prompt: str,
    context: list[str],
    session: AnySession,
) -> AsyncIterator[str]:
    rid = f"resp_{uuid.uuid4().hex}"
    iid = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())
    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': rid, 'object': 'response', 'created_at': created, 'model': alias, 'status': 'in_progress', 'output': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': iid, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': iid, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"
    full = ""
    try:
        async for delta in client.chat_stream(prompt, context, session):
            full += delta
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': iid, 'output_index': 0, 'content_index': 0, 'delta': delta})}\n\n"
    except GatewayUpstreamError as exc:
        yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        return
    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': iid, 'output_index': 0, 'content_index': 0, 'text': full})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': rid, 'object': 'response', 'created_at': created, 'model': alias, 'status': 'completed', 'output': [{'id': iid, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full}]}], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"


async def _stream_anthropic(
    alias: str,
    client: UpstreamClient,
    prompt: str,
    context: list[str],
    session: AnySession,
) -> AsyncIterator[str]:
    mid = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("message_start", {"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant", "content": [], "model": alias, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    yield sse("ping", {"type": "ping"})
    try:
        async for delta in client.chat_stream(prompt, context, session):
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}})
    except GatewayUpstreamError as exc:
        yield sse("error", {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}})
        return
    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}})
    yield sse("message_stop", {"type": "message_stop"})