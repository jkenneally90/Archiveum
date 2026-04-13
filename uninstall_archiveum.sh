#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
DATA_DIR="$PROJECT_DIR/archiveum_data"
SETTINGS_PATH="$PROJECT_DIR/archiveum_settings.json"
STT_MODEL_DIR="$PROJECT_DIR/models/faster-whisper"
SERVICE_DEST="/etc/systemd/system/archiveum.service"

REMOVE_PIPER=0
REMOVE_OLLAMA=0
KEEP_UPLOADS=0
KEEP_SETTINGS=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-piper)
      REMOVE_PIPER=1
      ;;
    --remove-ollama)
      REMOVE_OLLAMA=1
      ;;
    --keep-uploads)
      KEEP_UPLOADS=1
      ;;
    --keep-settings)
      KEEP_SETTINGS=1
      ;;
    --yes)
      ASSUME_YES=1
      ;;
    *)
      printf '[Archiveum] Unknown option: %s\n' "$1" >&2
      exit 1
      ;;
  esac
  shift
done

log() {
  printf '\n[Archiveum] %s\n' "$1"
}

confirm_step() {
  local prompt="$1"
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    return 0
  fi
  printf '\n[Archiveum] %s [y/N] ' "$prompt"
  read -r answer
  [[ "${answer:-n}" =~ ^[Yy]$ ]]
}

remove_if_exists() {
  local path="$1"
  local label="$2"
  if [[ ! -e "$path" ]]; then
    printf '[Archiveum] Skipping %s, not present.\n' "$label"
    return
  fi
  printf '[Archiveum] Removing %s\n' "$label"
  rm -rf -- "$path"
}

reset_data_dir() {
  if [[ ! -d "$DATA_DIR" ]]; then
    return
  fi

  if [[ "$KEEP_UPLOADS" -eq 1 ]]; then
    find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name uploads -exec rm -rf -- {} +
    printf '[Archiveum] Kept uploaded files and removed the rest of archiveum_data.\n'
    return
  fi

  remove_if_exists "$DATA_DIR" "Archiveum data"
}

stop_archiveum_service() {
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files archiveum.service >/dev/null 2>&1; then
    sudo systemctl stop archiveum.service || true
    sudo systemctl disable archiveum.service || true
    sudo rm -f "$SERVICE_DEST"
    sudo systemctl daemon-reload || true
    printf '[Archiveum] Removed systemd service.\n'
  fi
}

remove_autostart_desktop_entry() {
  local desktop_entry="$HOME/.config/autostart/archiveum-browser.desktop"
  remove_if_exists "$desktop_entry" "autostart desktop entry"
}

stop_archiveum_processes() {
  pkill -f "$PROJECT_DIR/main.py" 2>/dev/null || true
}

log "Uninstalling Archiveum from $PROJECT_DIR"
printf '[Archiveum] This removes Archiveum virtualenv, local data, helpers, and local speech model.\n'
if [[ "$REMOVE_PIPER" -eq 1 ]]; then
  printf '[Archiveum] Piper removal is enabled.\n'
fi
if [[ "$REMOVE_OLLAMA" -eq 1 ]]; then
  printf '[Archiveum] Ollama removal is enabled.\n'
fi

if ! confirm_step "Continue with Archiveum uninstall?"; then
  log "Cancelled"
  exit 0
fi

stop_archiveum_processes
stop_archiveum_service
remove_autostart_desktop_entry
remove_if_exists "$VENV_DIR" "virtual environment"
reset_data_dir
remove_if_exists "$STT_MODEL_DIR" "local speech model cache"

if [[ "$KEEP_SETTINGS" -eq 0 ]]; then
  remove_if_exists "$SETTINGS_PATH" "settings file"
else
  printf '[Archiveum] Keeping archiveum_settings.json\n'
fi

if [[ "$REMOVE_PIPER" -eq 1 ]]; then
  remove_if_exists "$HOME/.local/share/piper" "Piper user data"
  remove_if_exists "$HOME/piper" "Piper install directory"
fi

if [[ "$REMOVE_OLLAMA" -eq 1 ]]; then
  remove_if_exists "$HOME/.ollama" "Ollama user data"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get remove -y ollama || true
  fi
fi

log "Archiveum uninstall complete"
printf '[Archiveum] To start fresh, run install_archiveum.sh again.\n'
