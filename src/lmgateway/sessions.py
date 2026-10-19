"""
Session management for the gateway.

Three session modes:
  - None            : stateless — a fresh conversation UUID per request (original behaviour)
  - IncognitoSession: stateless content, but a fixed conversation ID so upstream does not
                      accumulate thousands of orphaned chat threads in the user's history
  - PersistentSession: multi-turn — the same conversation ID is reused across requests,
                       giving the upstream model genuine memory of previous turns
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field

# ── Incognito constants ───────────────────────────────────────────────────────
# RFC-4122 compliant UUIDs reserved for incognito (ghost) sessions.
# They are stable across restarts so the upstream always maps them to the
# same throwaway thread instead of creating a new one each time.
_GHOST_CONVERSATION_ID: str = "cafecafe-0000-4000-a000-000000000001"
_GHOST_SESSION_ID: str = "cafecafe-0000-4000-a000-000000000002"


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UpstreamTurn:
    """Immutable snapshot of the IDs to use for a single upstream WebSocket turn."""

    conversation_id: str
    client_session_id: str
    is_start_of_session: bool


# ── Session types ─────────────────────────────────────────────────────────────

class IncognitoSession:
    """
    Ghost session: every request is content-independent (is_start_of_session=True)
    but shares a fixed conversation_id so the upstream does not spam the user's
    chat history with thousands of orphaned threads.

    No lock needed — concurrent requests are fine because each turn is independent.
    """

    def reserve_turn(self) -> UpstreamTurn:
        return UpstreamTurn(
            conversation_id=_GHOST_CONVERSATION_ID,
            client_session_id=_GHOST_SESSION_ID,
            is_start_of_session=True,
        )


@dataclass
class PersistentSession:
    """
    Stateful session: the same conversation_id is reused across turns, giving
    the upstream model genuine memory of previous exchanges.

    Access is serialised with an asyncio.Lock so concurrent requests on the
    same session do not interleave their turns.
    """

    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    turn_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reserve_turn(self) -> UpstreamTurn:
        turn = UpstreamTurn(
            conversation_id=self.conversation_id,
            client_session_id=self.client_session_id,
            is_start_of_session=self.turn_count == 0,
        )
        self.turn_count += 1
        return turn


# Type alias used throughout the codebase
AnySession = PersistentSession | IncognitoSession | None


# ── Store ─────────────────────────────────────────────────────────────────────

class SessionStore:
    """Thread-safe registry of named PersistentSession objects."""

    def __init__(self) -> None:
        self._sessions: dict[str, PersistentSession] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> PersistentSession:
        with self._lock:
            if key not in self._sessions:
                self._sessions[key] = PersistentSession()
            return self._sessions[key]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)