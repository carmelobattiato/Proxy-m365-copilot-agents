"""
Token management: decode, validate, monitor expiry, hot-reload from .env.

The upstream API uses short-lived JWTs (~1 hour). This module:
  - Decodes the JWT payload without a signing-key check (we only need the claims)
  - Validates the audience claim to ensure it is the correct upstream service
  - Monitors the .env file mtime and hot-reloads the token when it changes
  - Never logs or exposes raw token values
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Audience prefix that identifies a valid upstream access token.
_EXPECTED_AUDIENCE_PREFIX: str = "https://substrate.office.com/"


# ── JWT helpers ───────────────────────────────────────────────────────────────

def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the payload section of a JWT without signature verification."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding if needed
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as exc:
        raise ValueError(f"Malformed JWT: {exc}") from exc


def is_valid_upstream_token(claims: dict[str, Any]) -> bool:
    """Return True if the JWT audience identifies the expected upstream service."""
    return str(claims.get("aud", "")).startswith(_EXPECTED_AUDIENCE_PREFIX)


# ── Token store ───────────────────────────────────────────────────────────────

class TokenStore:
    """
    Thread-safe holder for the upstream access token.

    Hot-reloads from .env whenever the file modification time changes so the
    server can pick up a refreshed token without restarting.
    """

    def __init__(self, token: str, env_path: Path | str = ".env") -> None:
        self._token = token
        self._env_path = Path(env_path)
        self._mtime_ns: int | None = self._read_mtime()
        self._lock = threading.RLock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self) -> str:
        with self._lock:
            self._reload_if_changed()
            return self._token

    def status(self) -> dict[str, Any]:
        """Return a safe, non-sensitive status dict for health endpoints."""
        token = self.get()
        now = time.time()
        try:
            claims = decode_jwt_payload(token)
        except ValueError as exc:
            return {
                "valid": False,
                "error": str(exc),
                "expires_at": None,
                "seconds_remaining": 0,
            }

        if not is_valid_upstream_token(claims):
            return {
                "valid": False,
                "error": "Token audience does not match the expected upstream service.",
                "expires_at": None,
                "seconds_remaining": 0,
            }

        exp = int(claims.get("exp", 0))
        seconds_remaining = max(0, exp - int(now))
        return {
            "valid": seconds_remaining > 0,
            "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
            "seconds_remaining": seconds_remaining,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _reload_if_changed(self) -> None:
        mtime_ns = self._read_mtime()
        if mtime_ns is None or mtime_ns == self._mtime_ns:
            return
        fresh = _read_env_token(self._env_path)
        if fresh:
            self._token = fresh
            self._mtime_ns = mtime_ns

    def _read_mtime(self) -> int | None:
        try:
            return self._env_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None


# ── .env helpers ──────────────────────────────────────────────────────────────

def _read_env_token(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if sep and key.strip() == "LMG_ACCESS_TOKEN":
            return _strip_quotes(value)
    return None


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value