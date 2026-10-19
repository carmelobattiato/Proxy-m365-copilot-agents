"""
Upstream WebSocket client.

Connects to the enterprise LLM backend via a SignalR-over-WebSocket protocol,
sends user prompts, and streams back response deltas.

Protocol summary
----------------
1. Open WebSocket to the backend hub URL (credentials passed as query params).
2. Send the SignalR JSON v1 handshake: {"protocol":"json","version":1} + SEP
3. Wait for the handshake ACK (type 6 keep-alive frames are silently discarded).
4. Send the chat invocation frame (type 4, target "chat") + SEP.
5. Receive update frames (type 1, target "update") — delta text is in
   arguments[0].writeAtCursor; full fallback text is in arguments[0].messages.
6. A completion frame (type 3) signals end-of-response.

The record separator (SEP = 0x1E) is part of the SignalR JSON protocol.

Security notes
--------------
- The access token is passed as a URL query parameter (upstream requirement).
  It is never logged; error messages only reference "the access token" by name.
- Token claims (oid, tid) are extracted once at construction time and cached.
- WebSocket connection errors are wrapped in GatewayUpstreamError so callers
  never receive raw network exceptions with potentially sensitive URLs.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

import websockets

from .auth import decode_jwt_payload, is_valid_upstream_token
from .sessions import AnySession, IncognitoSession, PersistentSession, UpstreamTurn

_SEP = "\x1e"  # SignalR record separator
_HUB_BASE = "wss://substrate.office.com/m365Copilot/Chathub"

# ── Static upstream parameters ────────────────────────────────────────────────
# These feature flags mirror what the official web client sends.
_VARIANTS = (
    "EnableMcpServerWidgets,feature.EnableMcpServerWidgets,feature.EnableLuForChatCIQ,"
    "feature.enableChatCIQPlugin,EnableRequestPlugins,feature.EnableSensitivityLabels,"
    "EnableUnsupportedUrlDetector,feature.IsCustomEngineCopilotEnabled,feature.bizchatfluxv3,"
    "feature.enablechatpages,feature.enableCodeCanvas,feature.turnOnWorkTabRecommendation,"
    "feature.turnOnDARecommendation,feature.IsStreamingModeInChatRequestEnabled,"
    "IncludeSourceAttributionsConcise,SkipPublishEmptyMessage,"
    "feature.EnableDeduplicatingSourceAttributions,Enable3PActionProgressMessages,"
    "feature.enableClientWebRtc,feature.EnableMeetingRecapOfSeriesMeetingWithCiq,"
    "feature.EnableReferencesListCompleteSignal,feature.StorageMessageSplitDisabled,"
    "feature.EnableCuaTakeControlApi,SingletonEnvOn,feature.cwcallowedos,"
    "feature.EnableMergingPureDeltas,feature.disabledisallowedmsgs,"
    "feature.enableCitationsForSynthesisData,feature.EnableConversationShareApis,"
    "feature.enableGenerateGraphicArtOptionsSet,cdximagen,"
    "feature.EnableUpdatedUXForConfirmationDialog,"
    "feature.EnableContentApiandDocTypeHtmlInRichAnswers,"
    "cdxgrounding_api_v2_rich_web_answers_reference_bottom_force,"
    "cdxenablerenderforisocomp,feature.EnableClientFileURLSupportForOfficeWebPaidCopilot,"
    "feature.EnableDesignEditorImageGrounding,feature.EnableDesignerEditor,"
    "feature.EnableSkipRehydrationForSpeCIdImages,feature.EnableSkipEmittingMessageOnFlush,"
    "feature.EnableRemoveEmptySourceAttributions,feature.EnableRemoveStreamingMode,"
    "feature.OfficeWebToHelix,feature.OfficeDesktopToHelix,feature.M365TeamsHubToHelix,"
    "feature.OwaHubToHelix,feature.MonarchHubToHelix,feature.Win32OutlookHubToHelix,"
    "feature.MacOutlookHubToHelix,Agt_bizchat_enableGpt5ForHelix"
)

_OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "cwc_flux_image", "cwc_code_interpreter", "cwc_code_interpreter_amsfix",
    "cwcfluxgptv", "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "cwc_code_interpreter_citation_fix", "code_interpreter_interactive_charts",
    "cwc_code_interpreter_interactive_charts_inline_image",
    "code_interpreter_matplotlib_patching", "cwc_fileupload_odb",
    "update_memory_plugin", "add_custom_instructions", "cwc_flux_v3",
    "flux_v3_progress_messages", "enable_batch_token_processing", "enable_gg_gpt",
    "flux_v3_image_gen_enable_dimensions", "flux_v3_image_gen_enable_icon_dimensions",
    "flux_v3_image_gen_enable_system_text_with_params",
    "flux_v3_image_gen_enable_designer_dimensions_meta_prompting_in_system_prompts",
]

_ALLOWED_MESSAGE_TYPES = [
    "Chat", "Suggestion", "InternalSearchQuery", "Disengaged",
    "InternalLoaderMessage", "Progress", "GeneratedCode", "RenderCardRequest",
    "AdsQuery", "SemanticSerp", "GenerateContentQuery", "GenerateGraphicArt",
    "SearchQuery", "ConfirmationCard", "AuthError", "DeveloperLogs",
    "TriggerPlugin", "HintInvocation", "MemoryUpdate", "EndOfRequest",
    "TriggerConfirmation", "ResumeInvokeAction", "ResumeUserInputRequest",
    "TriggerUserInputRequest", "EscapeHatch", "TriggerPluginAuth",
    "ResumePluginAuth", "SideBySide", "ReferencesListComplete",
    "SwitchRespondingEndpoint",
]


# ── Exception ─────────────────────────────────────────────────────────────────

class GatewayUpstreamError(RuntimeError):
    """Raised when the upstream service returns an error or is unreachable."""


# ── Client ────────────────────────────────────────────────────────────────────

class UpstreamClient:
    """
    Stateless WebSocket client for the enterprise LLM backend.

    Each call to chat_stream() opens a fresh WebSocket connection, sends the
    prompt, streams back response deltas, and closes the connection.
    """

    def __init__(
        self,
        access_token: str,
        time_zone: str = "Europe/Rome",
        tone: str = "Claude_Sonnet",
        ws_timeout: int = 30,
    ) -> None:
        if not access_token:
            raise GatewayUpstreamError(
                "LMG_ACCESS_TOKEN is not set. Run `lm-gateway set-token` or "
                "start the server with `lm-gateway serve` to capture it automatically."
            )
        self._tone = tone
        self._time_zone = time_zone
        self._ws_timeout = ws_timeout

        # Validate token without logging its value
        try:
            claims = decode_jwt_payload(access_token)
        except ValueError as exc:
            raise GatewayUpstreamError(f"Cannot parse access token: {exc}") from exc

        if not is_valid_upstream_token(claims):
            raise GatewayUpstreamError(
                "The access token is not issued for the expected upstream service. "
                "Make sure you copied the correct WebSocket URL from the browser."
            )

        if time.time() > claims.get("exp", 0):
            raise GatewayUpstreamError(
                "The access token has expired. Refresh it with `lm-gateway serve` "
                "(auto-refresh) or `lm-gateway set-token` (manual)."
            )

        # Cache only non-sensitive claims needed to build the hub URL
        self._oid: str = claims["oid"]
        self._tid: str = claims["tid"]
        self._token = access_token  # kept private, never logged

    # ── Public API ────────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        session: AnySession = None,
    ) -> AsyncIterator[str]:
        text = _combine(prompt, additional_context)

        if session is None:
            async for chunk in self._stream_turn(
                text=text,
                conv_id=str(uuid.uuid4()),
                session_id=str(uuid.uuid4()),
                is_start=True,
            ):
                yield chunk
            return

        if isinstance(session, IncognitoSession):
            turn = session.reserve_turn()
            async for chunk in self._stream_turn(
                text=text,
                conv_id=turn.conversation_id,
                session_id=turn.client_session_id,
                is_start=turn.is_start_of_session,
            ):
                yield chunk
            return

        # PersistentSession: serialise turns with the async lock
        async with session.lock:
            turn = session.reserve_turn()
            async for chunk in self._stream_turn(
                text=text,
                conv_id=turn.conversation_id,
                session_id=turn.client_session_id,
                is_start=turn.is_start_of_session,
            ):
                yield chunk

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
        session: AnySession = None,
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.chat_stream(prompt, additional_context, session):
            chunks.append(chunk)
        return "".join(chunks)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _hub_url(self, conv_id: str, session_id: str, req_id: str) -> str:
        token_enc = quote(self._token, safe="")
        return (
            f"{_HUB_BASE}/{self._oid}@{self._tid}"
            f"?ClientRequestId={req_id}"
            f"&X-SessionId={session_id}"
            f"&ConversationId={conv_id}"
            f"&access_token={token_enc}"
            f"&variants={_VARIANTS}"
            f"&source=officeweb&product=Office&agentHost=Bizchat.FullScreen"
            f"&licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot"
        )

    def _invoke_frame(
        self,
        text: str,
        conv_id: str,
        session_id: str,
        req_id: str,
        is_start: bool,
    ) -> str:
        payload = {
            "arguments": [{
                "source": "officeweb",
                "clientCorrelationId": req_id,
                "sessionId": session_id,
                "optionsSets": _OPTIONS_SETS,
                "streamingMode": "ConciseWithPadding",
                "spokenTextMode": "None",
                "options": {},
                "extraExtensionParameters": {},
                "allowedMessageTypes": _ALLOWED_MESSAGE_TYPES,
                "sliceIds": [],
                "threadLevelGptId": {},
                "traceId": req_id,
                "isStartOfSession": is_start,
                "clientInfo": {
                    "clientPlatform": "mcmcopilot-web",
                    "clientAppName": "Office",
                    "clientEntrypoint": "mcmcopilot-officeweb",
                    "clientSessionId": session_id,
                    "clientAppType": "Web",
                    "deviceOS": "Windows",
                    "deviceType": "Desktop",
                },
                "message": {
                    "author": "user",
                    "inputMethod": "Keyboard",
                    "text": text,
                    "entityAnnotationTypes": [
                        "People", "File", "Event", "Email", "TeamsMessage",
                    ],
                    "requestId": req_id,
                    "locationInfo": {
                        "timeZoneOffset": 0,
                        "timeZone": self._time_zone,
                    },
                    "locale": "en-us",
                    "messageType": "Chat",
                    "experienceType": "Default",
                    "adaptiveCards": [],
                    "clientPreferences": {},
                },
                "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                "isSbsSupported": True,
                "tone": self._tone,
                "renderReferencesBehindEOS": True,
            }],
            "invocationId": "0",
            "target": "chat",
            "type": 4,
        }
        return json.dumps(payload, ensure_ascii=False) + _SEP

    async def _stream_turn(
        self,
        text: str,
        conv_id: str,
        session_id: str,
        is_start: bool,
    ) -> AsyncIterator[str]:
        req_id = str(uuid.uuid4())
        url = self._hub_url(conv_id, session_id, req_id)

        try:
            async with websockets.connect(
                url,
                additional_headers={"Origin": "https://m365.cloud.microsoft"},
                open_timeout=self._ws_timeout,
            ) as ws:
                # SignalR handshake
                await ws.send(json.dumps({"protocol": "json", "version": 1}) + _SEP)
                await ws.recv()

                # Send chat invocation
                await ws.send(self._invoke_frame(text, conv_id, session_id, req_id, is_start))

                fallback_text = ""
                yielded_any = False

                async for raw in ws:
                    for part in raw.split(_SEP):
                        part = part.strip()
                        if not part:
                            continue
                        try:
                            msg = json.loads(part)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type")

                        if msg_type == 6:
                            # Keep-alive ping — discard silently
                            continue

                        if msg_type == 1 and msg.get("target") == "update":
                            args = (msg.get("arguments") or [{}])[0]
                            delta = args.get("writeAtCursor")
                            if delta:
                                if not yielded_any and fallback_text:
                                    yield fallback_text
                                yielded_any = True
                                yield delta
                            msgs = args.get("messages")
                            if msgs:
                                entries = msgs if isinstance(msgs, list) else [msgs]
                                for entry in reversed(entries):
                                    if entry.get("author") != "user":
                                        fallback_text = entry.get("text", "")
                                        break

                        if msg_type == 2:
                            for entry in reversed(
                                (msg.get("item") or {}).get("messages") or []
                            ):
                                if entry.get("author") != "user":
                                    fallback_text = entry.get("text", "")
                                    break

                        if msg_type == 3:
                            if not yielded_any and fallback_text:
                                yield fallback_text
                            return

        except GatewayUpstreamError:
            raise
        except Exception as exc:
            # Wrap all network errors — never expose the raw URL (contains the token)
            raise GatewayUpstreamError(
                f"Upstream connection failed: {type(exc).__name__}: {exc}"
            ) from exc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _combine(prompt: str, context: list[str]) -> str:
    if not context:
        return prompt
    return "\n\n".join(context) + "\n\n---\n\n" + prompt