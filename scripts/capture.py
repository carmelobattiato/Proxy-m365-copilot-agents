"""
Standalone token capture script for macOS / Linux.

Connects to a running browser debug session (Chrome DevTools Protocol),
intercepts the upstream WebSocket URL, extracts the access token and
writes it to .env.

Usage
-----
  # Check if a fresh token is needed (exit 0 = yes, exit 1 = no)
  uv run python scripts/capture.py --needs-token

  # Capture a fresh token (requires browser already open with --remote-debugging-port)
  uv run python scripts/capture.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import websockets

# Add src/ to path so we can import lmgateway without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from lmgateway.auth import decode_jwt_payload, is_valid_upstream_token  # noqa: E402

# ── Configuration ─────────────────────────────────────────────────────────────
CDP_PORT = int(os.environ.get("LMG_CDP_PORT", "9222"))
TIMEOUT_SECONDS = int(os.environ.get("LMG_CAPTURE_TIMEOUT", "600"))
MIN_REMAINING_SECONDS = 600
ENV_PATH = Path(".env")
ENV_TOKEN_KEY = "LMG_ACCESS_TOKEN"
GATEWAY_URL_PREFIX = "https://m365.cloud.microsoft/"
UPSTREAM_HOST = "substrate.office.com"

# CDP: focus the chat input to trigger a WebSocket connection
_NUDGE_JS = """
(() => {
    const input = document.querySelector(
        '[aria-label="Message Copilot"], textarea, [contenteditable="true"], [role="textbox"]'
    );
    if (!input) return false;
    input.focus();
    return true;
})()
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_valid(token: str) -> bool:
    try:
        return is_valid_upstream_token(decode_jwt_payload(token))
    except Exception:
        return False


def _seconds_remaining(token: str) -> int:
    try:
        return int(decode_jwt_payload(token).get("exp", 0)) - int(time.time())
    except Exception:
        return 0


def _read_token() -> str | None:
    if not ENV_PATH.exists():
        return None
    m = re.search(rf"(?m)^{ENV_TOKEN_KEY}=(.*)$", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else None


def _write_token(token: str) -> None:
    pattern = rf"(?m)^{ENV_TOKEN_KEY}=.*$"
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    replacement = f"{ENV_TOKEN_KEY}={token}"
    if re.search(pattern, text):
        text = re.sub(pattern, replacement, text)
    else:
        text += f"\n{replacement}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def _find_page(tabs: list[dict]) -> dict | None:
    return next(
        (t for t in tabs if t.get("type") == "page" and t.get("url", "").startswith(GATEWAY_URL_PREFIX)),
        None,
    )


async def _capture_from_tab(tab: dict) -> str | None:
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": _NUDGE_JS}}))
        await ws.send(json.dumps({"id": 3, "method": "Input.insertText", "params": {"text": " "}}))
        for etype in ("keyDown", "keyUp"):
            await ws.send(json.dumps({"id": 4, "method": "Input.dispatchKeyEvent", "params": {
                "type": etype, "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
                "key": "Backspace", "code": "Backspace",
            }}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("method") != "Network.webSocketCreated":
                continue
            url = msg.get("params", {}).get("url", "")
            if UPSTREAM_HOST not in url:
                continue
            m = re.search(r"[?&]access_token=([^&]+)", url)
            if m and _is_valid(m.group(1)):
                return m.group(1)
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    if "--needs-token" in sys.argv:
        token = _read_token()
        if token and _is_valid(token) and _seconds_remaining(token) > MIN_REMAINING_SECONDS:
            remaining_min = _seconds_remaining(token) // 60
            print(f"Token is still valid ({remaining_min} min remaining). No login needed.")
            return 1  # does NOT need a token
        return 0  # needs a token

    print("Waiting for the browser debug session...")
    deadline = time.time() + TIMEOUT_SECONDS
    announced = False

    while time.time() < deadline:
        try:
            tabs = httpx.get(f"http://localhost:{CDP_PORT}/json", timeout=2).json()
        except Exception:
            time.sleep(1)
            continue

        tab = _find_page(tabs)
        if not tab:
            time.sleep(1)
            continue

        if not announced:
            print("Browser tab found. Sign in if prompted, then wait...")
            announced = True

        try:
            token = asyncio.run(_capture_from_tab(tab))
        except Exception:
            token = None

        if token:
            _write_token(token)
            remaining_min = _seconds_remaining(token) // 60
            print(f"Token saved to {ENV_PATH} (expires in {remaining_min} min).")
            return 0

        time.sleep(3)

    print("Timeout: no token captured. Did you sign in to the service in the browser window?")
    return 1


if __name__ == "__main__":
    sys.exit(main())