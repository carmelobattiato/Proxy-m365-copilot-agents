#!/usr/bin/env bash
# lm-gateway launcher for macOS / Linux
# Opens the dedicated browser profile, captures the token automatically,
# and starts the gateway on http://127.0.0.1:8000

set -eu
cd "$(dirname "${BASH_SOURCE[0]}")"

CDP_PORT="${LMG_CDP_PORT:-9222}"
SERVER_PORT="${LMG_SERVER_PORT:-8000}"
PROFILE_DIR="${HOME}/.lm-gateway/browser-profile"

# ── Dependency check ──────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' not found. Install with: brew install uv" >&2
  exit 1
fi

uv sync --quiet

# ── Token check ───────────────────────────────────────────────────────────────
if uv run python - <<'EOF'
import sys, time, json, base64, re
from pathlib import Path

env = Path(".env")
token = None
if env.exists():
    for line in env.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "LMG_ACCESS_TOKEN":
            token = v.strip().strip("\"'")

if not token or not token.startswith("eyJ"):
    sys.exit(0)  # needs token

payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
remaining = int(claims.get("exp", 0)) - int(time.time())
sys.exit(0 if remaining < 600 else 1)
EOF
then
  # Needs a fresh token — open the browser
  BROWSER=""
  for candidate in \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if [ -x "$candidate" ]; then
      BROWSER="$candidate"
      break
    fi
  done

  if [ -z "$BROWSER" ]; then
    echo "ERROR: No supported browser found in /Applications." >&2
    exit 1
  fi

  if ! curl -s "http://localhost:${CDP_PORT}/json/version" >/dev/null 2>&1; then
    mkdir -p "$PROFILE_DIR"
    echo "Opening browser (profile: ${PROFILE_DIR})..."
    nohup "$BROWSER" \
      --remote-debugging-port="${CDP_PORT}" \
      --user-data-dir="${PROFILE_DIR}" \
      --no-first-run \
      "https://m365.cloud.microsoft/chat" >/dev/null 2>&1 &
  fi

  echo "Capturing token from browser..."
  uv run lm-gateway capture-token --cdp-port "${CDP_PORT}" --timeout-seconds 120 || true
else
  echo "Existing token is still valid — skipping login."
fi

# ── Start gateway ─────────────────────────────────────────────────────────────
echo "Starting LM Gateway on http://127.0.0.1:${SERVER_PORT}"
exec uv run uvicorn lmgateway.server:create_app \
  --factory --host 127.0.0.1 --port "${SERVER_PORT}"