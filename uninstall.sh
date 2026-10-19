#!/usr/bin/env bash
# Disinstalla tutto cio' che e' stato installato per m365-copilot-openai-proxy.
# Uso: ./uninstall.sh          (chiede conferma per ogni voce delicata)
#      ./uninstall.sh -y       (non chiede nulla, rimuove tutto)

set -u

ASSUME_YES=0
[ "${1:-}" = "-y" ] && ASSUME_YES=1

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_DIR="$HOME/.m365-copilot-openai-proxy"
PORT=8000

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  local answer
  read -r -p "$1 [y/N] " answer
  [[ "$answer" =~ ^[yY]$ ]]
}

remove() {
  if [ -e "$1" ]; then
    rm -rf "$1" && echo "rimosso: $1"
  fi
}

# 1. Ferma il proxy se e' in esecuzione
PIDS="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  if confirm "Fermare il processo in ascolto sulla porta $PORT?"; then
    kill $PIDS 2>/dev/null && echo "processo fermato sulla porta $PORT"
  fi
fi

# 2. File generati dentro il repo (venv, token in .env, cache)
remove "$REPO_DIR/.venv"
remove "$REPO_DIR/.env"
remove "$REPO_DIR/.pytest_cache"
remove "$REPO_DIR/.ruff_cache"
find "$REPO_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
find "$REPO_DIR/src" -name "*.egg-info" -type d -prune -exec rm -rf {} + 2>/dev/null

# 3. Profilo Edge dedicato (contiene la sessione di login, solo se creato dal proxy)
if [ -d "$PROFILE_DIR" ] && confirm "Rimuovere il profilo Edge dedicato in $PROFILE_DIR (sessione di login)?"; then
  remove "$PROFILE_DIR"
fi

# 4. Python gestiti da uv e cache di uv
if [ -d "$HOME/.local/share/uv" ] && confirm "Rimuovere i Python gestiti da uv in ~/.local/share/uv?"; then
  remove "$HOME/.local/share/uv"
fi
if [ -d "$HOME/.cache/uv" ] && confirm "Rimuovere la cache di uv in ~/.cache/uv?"; then
  remove "$HOME/.cache/uv"
fi

# 5. uv installato con Homebrew
if command -v brew >/dev/null 2>&1 && brew list --formula uv >/dev/null 2>&1; then
  if confirm "Disinstallare uv (brew uninstall uv)?"; then
    brew uninstall uv && echo "uv disinstallato"
  fi
fi

# 6. Cartella del repo clonato (ultima voce: lo script esce dal repo prima di cancellarlo)
if confirm "Rimuovere anche la cartella del repo clonato ($REPO_DIR)?"; then
  cd "$HOME" || exit 1
  rm -rf "$REPO_DIR" && echo "rimosso: $REPO_DIR"
fi

echo "Disinstallazione completata."
