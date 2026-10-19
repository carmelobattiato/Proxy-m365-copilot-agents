"""
Request adapter — translates OpenAI and Anthropic request formats into the
internal (prompt, additional_context) representation consumed by the upstream client.

All three supported API styles are handled here:
  - OpenAI Chat Completions  → translate_openai_request()
  - OpenAI Responses         → translate_responses_request()
  - Anthropic Messages       → translate_anthropic_request()

Design note: system prompts and conversation history are converted to plain
text context blocks because the upstream does not expose a structured API for
them. Tool instruction text, if present, is injected at the top of the user
prompt (not in context) because the upstream tends to ignore injected context
blocks for instruction-following purposes.
"""

from __future__ import annotations

from typing import Iterable

from .schemas import (
    AnthropicMessagesRequest,
    ContentPart,
    OpenAIChatRequest,
    OpenAIMessage,
    OpenAIResponsesRequest,
    TranslatedRequest,
)
from .tools import render_tool_calls


# ── Content helpers ───────────────────────────────────────────────────────────

def flatten_content(content: str | list[ContentPart] | None) -> str:
    """Reduce any content shape to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(part.text or "" for part in content if part.type == "text")


def _join(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line).strip()


# ── OpenAI Chat Completions ───────────────────────────────────────────────────

def _openai_message_text(message: OpenAIMessage) -> str:
    text = flatten_content(message.content).strip()
    if message.tool_calls:
        text = _join([text, render_tool_calls(message.tool_calls)])
    if message.role == "tool":
        tid = message.tool_call_id or "unknown"
        return f"Tool result ({tid}): {text}" if text else ""
    return text


def translate_openai_request(
    request: OpenAIChatRequest,
    tool_instruction_text: str | None = None,
) -> TranslatedRequest:
    system_lines: list[str] = []
    transcript_lines: list[str] = []
    prompt_lines: list[str] = []
    messages = request.messages

    # Trailing tool-result messages form the new user prompt in an agentic loop
    tail_start = len(messages)
    while tail_start > 0 and messages[tail_start - 1].role == "tool":
        tail_start -= 1

    for idx, message in enumerate(messages):
        text = _openai_message_text(message)
        if not text:
            continue
        if message.role in {"system", "developer"}:
            system_lines.append(text)
            continue
        if idx >= tail_start:
            prompt_lines.append(text)
            continue
        if idx == len(messages) - 1:
            if message.role != "user":
                raise ValueError("The final message must have role 'user'.")
            prompt_lines.append(text)
            continue
        if message.role == "tool":
            transcript_lines.append(text)
        else:
            transcript_lines.append(f"{message.role.capitalize()}: {text}")

    prompt = _join(prompt_lines)
    if not prompt:
        raise ValueError("A final user message is required.")

    # Tool instructions go in the prompt, not context, for reliable instruction-following
    if tool_instruction_text:
        prompt = f"{tool_instruction_text}\n\n{prompt}"

    additional_context: list[str] = []
    system_text = _join(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")

    return TranslatedRequest(prompt=prompt, additional_context=additional_context)


# ── OpenAI Responses ──────────────────────────────────────────────────────────

def translate_responses_request(request: OpenAIResponsesRequest) -> TranslatedRequest:
    instructions = request.instructions or ""

    if isinstance(request.input, str):
        ctx = [f"System instructions:\n{instructions}"] if instructions else []
        return TranslatedRequest(prompt=request.input, additional_context=ctx)

    system_lines: list[str] = [instructions] if instructions else []
    transcript_lines: list[str] = []
    prompt = ""
    items = request.input

    for idx, item in enumerate(items):
        role = item.get("role", "") if isinstance(item, dict) else ""
        content = item.get("content", "") if isinstance(item, dict) else str(item)
        if isinstance(content, list):
            content = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            )
        text = content.strip()
        if not text:
            continue
        is_last = idx == len(items) - 1
        if role in {"system", "developer"}:
            system_lines.append(text)
            continue
        if is_last:
            if role != "user":
                raise ValueError("The final Responses input message must be a user message.")
            prompt = text
            continue
        transcript_lines.append(f"{role.capitalize()}: {text}")

    if not prompt:
        raise ValueError("No user message found in input.")

    additional_context: list[str] = []
    system_text = _join(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")

    return TranslatedRequest(prompt=prompt, additional_context=additional_context)


# ── Anthropic Messages ────────────────────────────────────────────────────────

def translate_anthropic_request(request: AnthropicMessagesRequest) -> TranslatedRequest:
    system_text = flatten_content(request.system).strip()
    transcript_lines: list[str] = []
    prompt = ""

    for idx, message in enumerate(request.messages):
        text = flatten_content(message.content).strip()
        if not text:
            continue
        is_last = idx == len(request.messages) - 1
        if is_last:
            if message.role != "user":
                raise ValueError("The final Anthropic message must have role 'user'.")
            prompt = text
            continue
        transcript_lines.append(f"{message.role.capitalize()}: {text}")

    if not prompt:
        raise ValueError("A final user message is required.")

    additional_context: list[str] = []
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")

    return TranslatedRequest(prompt=prompt, additional_context=additional_context)