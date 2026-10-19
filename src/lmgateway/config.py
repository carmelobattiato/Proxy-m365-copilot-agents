"""
Configuration — all settings are read from environment variables or a .env file.

Prefix: LMG_
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Authentication ────────────────────────────────────────────────────────
    access_token: str = Field(default="", alias="LMG_ACCESS_TOKEN")

    # ── Upstream behaviour ────────────────────────────────────────────────────
    time_zone: str = Field(default="Europe/Rome", alias="LMG_TIME_ZONE")
    # Internal model selector: "Claude_Sonnet" | "Claude_Sonnet_5" | "Magic"
    tone: str = Field(default="Claude_Sonnet", alias="LMG_TONE")
    # Timeout in seconds for each upstream WebSocket turn
    ws_timeout: int = Field(default=30, alias="LMG_WS_TIMEOUT_SECONDS")

    # ── API surface ───────────────────────────────────────────────────────────
    # Name returned by /v1/models and echoed in responses
    model_alias: str = Field(default="lm-gateway", alias="LMG_MODEL_ALIAS")

    # ── Features ──────────────────────────────────────────────────────────────
    # Emulate OpenAI tool_calls via prompt injection (default: on)
    tool_emulation: bool = Field(default=True, alias="LMG_TOOL_EMULATION")
    # Incognito mode: reuse a fixed conversation ID for stateless requests so
    # they do not pollute the upstream chat history (default: on)
    incognito: bool = Field(default=True, alias="LMG_INCOGNITO")