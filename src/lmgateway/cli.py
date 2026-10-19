"""
Command-line interface for lm-gateway.

Commands
--------
serve          Start the HTTP gateway server (default entry-point)
set-token      Paste a fresh token URL manually
capture-token  Capture a token from a running debug browser (CDP)
launch-browser Open the dedicated debug browser profile

The serve command also handles:
  - Launching a dedicated browser profile with remote-debugging enabled
  - Automatic token capture on first start
  - Background token auto-refresh loop
  - Interactive [q]uit / [r]efresh keystrokes (Windows) or SIGTERM (Unix)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import websockets

from .auth import decode_jwt_payload, is_valid_upstream_token
from .server import create_app

# Suppress the uvicorn "CTRL+C" noise on Windows
class _SuppressCtrlC(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "CTRL+C" not in record.getMessage()

logging.getLogger("uvicorn.error").addFilter(_SuppressCtrlC())

# ── Constants ─────────────────────────────────────────────────────────────────

_ENV_PATH = Path(".env")
_ENV_TOKEN_KEY = "LMG_ACCESS_TOKEN"
_BROWSER_PROFILE_DIR = Path.home() / ".lm-gateway" / "browser-profile"
_M365_URL = "https://m365.cloud.microsoft/chat"
_M365_PREFIX = "https://m365.cloud.microsoft/"
_UPSTREAM_HOST = "substrate.office.com"
_MIN_REMAINING_SECONDS = 300  # refresh when less than this much time remains

# CDP JavaScript: extract candidate tokens from browser storage / resource timing
_CDP_EXTRACT_JS = """
(() => {
    const candidates = [];
    for (const store of [sessionStorage, localStorage]) {
        for (const key of Object.keys(store)) {
            if (key.includes('accesstoken')) {
                try {
                    const v = JSON.parse(store.getItem(key));
                    if (v && v.secret && v.secret.startsWith('eyJ') &&
                        ((v.target && v.target.includes('substrate')) || key.includes('substrate'))) {
                        candidates.push(v.secret);
                    }
                } catch {}
            }
        }
    }
    for (const entry of performance.getEntriesByType('resource')) {
        if (!entry.name.includes('substrate.office.com') || !entry.name.includes('access_token=')) continue;
        const m = entry.name.match(/[?&]access_token=([^&]+)/);
        if (m) candidates.push(decodeURIComponent(m[1]));
    }
    return candidates;
})()
"""

# CDP JavaScript: focus the chat input box to trigger a WebSocket connection
_CDP_NUDGE_JS = """
(() => {
    const input = document.querySelector(
        '[aria-label="Message Copilot"], textarea, [contenteditable="true"], [role="textbox"]'
    );
    if (!input) return false;
    input.focus();
    return true;
})()
"""


# ── Token helpers ─────────────────────────────────────────────────────────────

def _is_valid_token(token: str) -> bool:
    try:
        return is_valid_upstream_token(decode_jwt_payload(token))
    except Exception:
        return False


def _seconds_remaining(token: str) -> int:
    try:
        return int(decode_jwt_payload(token).get("exp", 0)) - int(time.time())
    except Exception:
        return 0


def _needs_token(token: str | None) -> bool:
    if not token or not _is_valid_token(token):
        return True
    return _seconds_remaining(token) <= 0


def _read_token() -> str | None:
    if not _ENV_PATH.exists():
        return None
    text = _ENV_PATH.read_text(encoding="utf-8")
    m = re.search(rf"(?m)^{_ENV_TOKEN_KEY}=(.*)$", text)
    return m.group(1).strip().strip("\"'") if m else None


def _write_token(token: str) -> None:
    pattern = rf"(?m)^{_ENV_TOKEN_KEY}=.*$"
    text = _ENV_PATH.read_text(encoding="utf-8") if _ENV_PATH.exists() else ""
    replacement = f"{_ENV_TOKEN_KEY}={token}"
    if re.search(pattern, text):
        text = re.sub(pattern, replacement, text)
    else:
        text += f"\n{replacement}\n"
    _ENV_PATH.write_text(text, encoding="utf-8")


# ── CDP helpers ───────────────────────────────────────────────────────────────

def _find_gateway_page(tabs: list[dict]) -> dict | None:
    return next(
        (t for t in tabs if t.get("type") == "page" and t.get("url", "").startswith(_M365_PREFIX)),
        None,
    )


async def _cdp_extract_token(port: int, *, nudge: bool = True) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=2) as hc:
            tabs = (await hc.get(f"http://localhost:{port}/json")).json()
    except Exception:
        return None

    tab = _find_gateway_page(tabs)
    if not tab:
        return None

    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": _CDP_EXTRACT_JS}}))
            result = json.loads(await ws.recv())
            candidates = result.get("result", {}).get("result", {}).get("value") or []
            for t in candidates:
                if _is_valid_token(t):
                    return t
            if not nudge:
                return None
            return await _cdp_nudge_and_capture(ws)
    except Exception:
        return None


async def _cdp_nudge_and_capture(ws) -> str | None:
    await ws.send(json.dumps({"id": 2, "method": "Network.enable"}))
    await ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {"expression": _CDP_NUDGE_JS}}))
    await ws.send(json.dumps({"id": 4, "method": "Input.insertText", "params": {"text": " "}}))
    for etype in ("keyDown", "keyUp"):
        await ws.send(json.dumps({"id": 5, "method": "Input.dispatchKeyEvent", "params": {
            "type": etype, "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
            "key": "Backspace", "code": "Backspace",
        }}))
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg.get("method") != "Network.webSocketCreated":
            continue
        url = msg.get("params", {}).get("url", "")
        if _UPSTREAM_HOST not in url:
            continue
        m = re.search(r"[?&]access_token=([^&]+)", url)
        if m and _is_valid_token(m.group(1)):
            return m.group(1)
    return None


async def _cdp_capture_ws_token(port: int, timeout: int) -> str | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3) as hc:
                tabs = (await hc.get(f"http://localhost:{port}/json")).json()
        except Exception:
            await asyncio.sleep(1)
            continue
        tab = _find_gateway_page(tabs)
        if not tab:
            await asyncio.sleep(1)
            continue
        try:
            async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
                token = await _wait_ws_token(ws, deadline)
                if token:
                    return token
        except Exception:
            await asyncio.sleep(1)
    return None


async def _wait_ws_token(ws, deadline: float) -> str | None:
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg.get("method") != "Network.webSocketCreated":
            continue
        url = msg.get("params", {}).get("url", "")
        if _UPSTREAM_HOST not in url:
            continue
        m = re.search(r"[?&]access_token=([^&]+)", url)
        if m and _is_valid_token(m.group(1)):
            return m.group(1)
    return None


def _wait_for_page(port: int, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=1) as hc:
                tabs = hc.get(f"http://localhost:{port}/json").json()
            if _find_gateway_page(tabs):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _try_auto_refresh(port: int, *, nudge: bool = True) -> bool:
    token = asyncio.run(_cdp_extract_token(port, nudge=nudge))
    if not token:
        return False
    _write_token(token)
    print("Token refreshed automatically.")
    return True


def _capture_token_to_env(port: int, timeout: int) -> bool:
    token = asyncio.run(_cdp_capture_ws_token(port, timeout))
    if not token:
        return False
    _write_token(token)
    return True


def _startup_capture_loop(port: int, timeout: int) -> None:
    print("Waiting for the browser gateway tab...")
    _wait_for_page(port, min(timeout, 30))
    print("Attempting automatic token refresh from browser...")
    if _try_auto_refresh(port):
        return
    print("Waiting for a fresh token from the browser tab...")
    print("Tip: click the chat input box and type one character (no need to send).")
    if _capture_token_to_env(port, timeout):
        print(".env updated with fresh token.")
    else:
        print("Startup token capture timed out. Use `lm-gateway set-token` to set it manually.")


def _auto_refresh_loop(port: int, refresh_before: int, retry: int, stop: threading.Event) -> None:
    while not stop.is_set():
        token = _read_token()
        if not token:
            stop.wait(retry)
            continue
        remaining = _seconds_remaining(token)
        if remaining > refresh_before:
            stop.wait(min(remaining - refresh_before, 300))
            continue
        print(f"Token expires in {max(remaining, 0)}s — refreshing from browser...")
        if not _try_auto_refresh(port):
            print("Auto-refresh failed; will retry later.")
        stop.wait(retry)


# ── Browser launcher ──────────────────────────────────────────────────────────

def _launch_browser(port: int) -> None:
    _BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    candidates: list[list[str]] = []

    if sys.platform == "win32":
        candidates = [[
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={_BROWSER_PROFILE_DIR}",
            "--no-first-run", _M365_URL,
        ]]
    else:
        for browser in [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]:
            if Path(browser).exists():
                candidates = [[
                    browser,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={_BROWSER_PROFILE_DIR}",
                    "--no-first-run", _M365_URL,
                ]]
                break

    if not candidates:
        print("Warning: no supported browser found. Launch it manually with remote debugging.")
        return

    subprocess.Popen(candidates[0])
    print(f"Browser launched (debug port {port}). Profile: {_BROWSER_PROFILE_DIR}")
    print("Sign in to the enterprise LLM service in that window once.")


# ── CLI commands ──────────────────────────────────────────────────────────────

def cmd_set_token(_args) -> None:
    print("Paste the full WebSocket URL (or just the raw token value), then press Enter:")
    raw = input().strip()
    m = re.search(r"access_token=([^&\s]+)", raw)
    token = m.group(1) if m else raw
    if not token.startswith("eyJ"):
        print("Error: could not find a valid token. Copy the full WebSocket URL from DevTools.")
        return
    if not _is_valid_token(token):
        print("Error: token audience does not match the expected upstream service.")
        return
    _write_token(token)
    print(".env updated.")


def cmd_capture_token(args: argparse.Namespace) -> None:
    print("Listening for a token from the browser debug tab...")
    print("In the browser, click the chat input and type one character (no need to send).")
    token = asyncio.run(_cdp_capture_ws_token(args.cdp_port, args.timeout_seconds))
    if not token:
        print("Error: no valid token captured before timeout.")
        return
    _write_token(token)
    print(".env updated.")


def cmd_launch_browser(args: argparse.Namespace) -> None:
    _launch_browser(args.cdp_port)


def cmd_serve(args: argparse.Namespace) -> None:
    port: int = args.cdp_port

    # Windows-only interactive keypress loop; on Unix we rely on SIGTERM
    _use_keypress = sys.platform == "win32"
    if _use_keypress:
        import msvcrt  # noqa: PLC0415

    while True:
        app = create_app()
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        stop_refresh = threading.Event()

        if not args.no_launch_browser:
            _launch_browser(port)

        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        if not args.no_capture_on_start and _needs_token(_read_token()):
            threading.Thread(
                target=_startup_capture_loop,
                args=(port, args.capture_timeout),
                daemon=True,
            ).start()

        if not args.no_auto_refresh:
            threading.Thread(
                target=_auto_refresh_loop,
                args=(port, args.refresh_before, args.refresh_retry, stop_refresh),
                daemon=True,
            ).start()

        while not server.started and server_thread.is_alive():
            time.sleep(0.05)

        ar_label = "off" if args.no_auto_refresh else "on"
        sc_label = "off" if args.no_capture_on_start else "on"
        if _use_keypress:
            print(f"\n  [q] quit    [r] refresh token    auto-refresh: {ar_label}    startup-capture: {sc_label}\n")
        else:
            print(f"\n  Gateway running on http://{args.host}:{args.port}    auto-refresh: {ar_label}    startup-capture: {sc_label}")
            print("  Send SIGTERM to stop.\n")

        action = None
        while server_thread.is_alive():
            if _use_keypress and msvcrt.kbhit():
                key = msvcrt.getwch().lower()
                if key == "q":
                    action = "quit"
                    server.should_exit = True
                    break
                elif key == "r":
                    action = "refresh"
                    server.should_exit = True
                    break
            time.sleep(0.05)

        stop_refresh.set()
        server_thread.join()

        if action == "refresh":
            print("Refreshing token...")
            if not _try_auto_refresh(port):
                print("Auto-refresh failed. Falling back to manual mode.")
                cmd_set_token(None)
            print("Restarting server...")
        else:
            break


# ── Entry-point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lm-gateway",
        description="OpenAI-compatible local gateway for enterprise LLM subscriptions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # set-token
    sub.add_parser("set-token", help="Set the access token manually.").set_defaults(func=cmd_set_token)

    # capture-token
    cp = sub.add_parser("capture-token", help="Capture token from a running debug browser.")
    cp.add_argument("--cdp-port", type=int, default=9222)
    cp.add_argument("--timeout-seconds", type=int, default=60)
    cp.set_defaults(func=cmd_capture_token)

    # launch-browser
    lb = sub.add_parser("launch-browser", help="Open the dedicated debug browser profile.")
    lb.add_argument("--cdp-port", type=int, default=9222)
    lb.set_defaults(func=cmd_launch_browser)

    # serve
    sv = sub.add_parser("serve", help="Start the gateway server.")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--cdp-port", type=int, default=9222)
    sv.add_argument("--no-auto-refresh", action="store_true")
    sv.add_argument("--no-launch-browser", action="store_true")
    sv.add_argument("--no-capture-on-start", action="store_true")
    sv.add_argument("--capture-timeout", type=int, default=180)
    sv.add_argument("--refresh-before", type=int, default=300)
    sv.add_argument("--refresh-retry", type=int, default=60)
    sv.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()