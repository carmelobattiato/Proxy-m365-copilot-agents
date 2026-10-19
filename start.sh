#!/usr/bin/env bash
# Avvia il proxy: apre il browser, ti fa fare login a M365 Copilot,
# cattura il token da solo, lo scrive in .env e avvia il server su http://127.0.0.1:8000

set -eu
cd "$(dirname "${BASH_SOURCE[0]}")"

PORT="${M365_CDP_PORT:-9222}"
PROFILE="$HOME/.m365-copilot-openai-proxy/edge-profile"
SERVER_PORT="${M365_SERVER_PORT:-8000}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv non trovato. Installa con: brew install uv" >&2
  exit 1
fi

uv sync --quiet

if uv run python scripts/capture_token.py --needs-token; then
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
    echo "Né Microsoft Edge né Google Chrome trovati in /Applications." >&2
    exit 1
  fi

  if ! curl -s "http://localhost:$PORT/json/version" >/dev/null 2>&1; then
    mkdir -p "$PROFILE"
    echo "Apro il browser dedicato (profilo: $PROFILE)..."
    nohup "$BROWSER" \
      --remote-debugging-port="$PORT" \
      --user-data-dir="$PROFILE" \
      --no-first-run \
      "https://m365.cloud.microsoft/chat" >/dev/null 2>&1 &
  fi

  uv run python scripts/capture_token.py
else
  echo "Token ancora valido, salto il login."
fi

echo "Avvio proxy su http://127.0.0.1:$SERVER_PORT (Ctrl+C per fermarlo)"
exec uv run uvicorn m365_copilot_openai_proxy.app:create_app \
  --factory --host 127.0.0.1 --port "$SERVER_PORT"
