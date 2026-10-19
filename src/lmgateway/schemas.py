"""
Pydantic schemas for the three supported API styles:
  - OpenAI Chat Completions
  - OpenAI Responses
  - Anthropic Messages
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Shared ────────────────────────────────────────────────────────────────────

class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None


# ── OpenAI Chat Completions ───────────────────────────────────────────────────

class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class OpenAIChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    temperature: float | None = None
    user: str | None = None
    tools: list[dict[str, Any]] | None = None


# ── OpenAI Responses ──────────────────────────────────────────────────────────

class OpenAIResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    input: str | list[Any]
    instructions: str | None = None
    stream: bool = False


# ── Anthropic Messages ────────────────────────────────────────────────────────

class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"]
    content: str | list[ContentPart]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[AnthropicMessage]
    system: str | list[ContentPart] | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None


# ── Internal ──────────────────────────────────────────────────────────────────

class TranslatedRequest(BaseModel):
    """Result of adapting any incoming request format to a plain prompt + context list."""

    prompt: str
    additional_context: list[str] = Field(default_factory=list)