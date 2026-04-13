#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SETTINGS_PATH="$PROJECT_DIR/archiveum_settings.json"
SERVICE_SRC="$PROJECT_DIR/deploy/archiveum.service"
RENDERED_SERVICE="$PROJECT_DIR/archiveum_data/archiveum.service.rendered"
SERVICE_DEST="/etc/systemd/system/archiveum.service"
CURRENT_USER="${SUDO_USER:-${USER:-$(id -un)}}"
ENABLE_AUTOSTART="${ENABLE_AUTOSTART:-false}"
DESKTOP_START_SHORTCUT="${DESKTOP_START_SHORTCUT:-false}"
DESKTOP_STOP_SHORTCUT="${DESKTOP_STOP_SHORTCUT:-false}"

log() {
  printf '\n[Archiveum] %s\n' "$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '[Archiveum] Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

ollama_model_bootstrap() {
  if ! command -v ollama >/dev/null 2>&1; then
    log "Ollama CLI not found on PATH; skipping Ollama model setup"
    return 0
  fi

  if [[ ! -f "$SETTINGS_PATH" ]]; then
    log "Settings file not found at $SETTINGS_PATH; skipping Ollama model setup"
    return 0
  fi

  local arch
  arch="$(uname -m 2>/dev/null || true)"

  local chat_model
  local embed_model
  chat_model="$($PYTHON_BIN - <<'PY'
import json
import os
from pathlib import Path

settings_path = Path(os.environ["SETTINGS_PATH"])
data = json.loads(settings_path.read_text(encoding="utf-8"))
print(str(data.get("ollama_chat_model", "llama3.1:8b")).strip())
PY
  )"
  embed_model="$($PYTHON_BIN - <<'PY'
import json
import os
from pathlib import Path

settings_path = Path(os.environ["SETTINGS_PATH"])
data = json.loads(settings_path.read_text(encoding="utf-8"))
print(str(data.get("ollama_embed_model", "nomic-embed-text")).strip())
PY
  )"

  if [[ "$arch" == "aarch64" && "$chat_model" == "llama3.1:8b" ]]; then
    printf '\n[Archiveum] Detected Jetson-style ARM64 (%s). The default chat model (%s) can fail on smaller devices.\n' "$arch" "$chat_model"
    printf '[Archiveum] Switch to a smaller Ollama chat model (qwen2.5:1.5b) for better Jetson compatibility? [y/N] '
    read -r switch_model_now
    if [[ "${switch_model_now:-n}" =~ ^[Yy]$ ]]; then
      SETTINGS_PATH="$SETTINGS_PATH" OLLAMA_CHAT_MODEL="qwen2.5:1.5b" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

settings_path = Path(os.environ["SETTINGS_PATH"])
data = json.loads(settings_path.read_text(encoding="utf-8"))
data["ollama_chat_model"] = os.environ["OLLAMA_CHAT_MODEL"]
settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(data["ollama_chat_model"])
PY
      chat_model="qwen2.5:1.5b"
    fi
  fi

  if [[ "${PULL_OLLAMA_MODELS:-}" == "true" ]]; then
    log "Pulling Ollama models (PULL_OLLAMA_MODELS=true)"
  else
    printf '\n[Archiveum] Ollama detected. Pull required models now? [y/N] '
    read -r pull_models_now
    if [[ ! "${pull_models_now:-n}" =~ ^[Yy]$ ]]; then
      log "Skipping Ollama model pull"
      printf '[Archiveum] If chat requests fail (HTTP 500), ensure these models are installed:\n'
      printf '  ollama pull %s\n' "$chat_model"
      printf '  ollama pull %s\n' "$embed_model"
      return 0
    fi
  fi

  log "Pulling Ollama chat model: $chat_model"
  ollama pull "$chat_model"

  log "Pulling Ollama embed model: $embed_model"
  ollama pull "$embed_model"
}

install_venv() {
  log "Creating virtual environment"
  "$PYTHON_BIN" -m venv "$VENV_DIR"

  log "Upgrading pip tooling"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

  log "Installing Python requirements"
  "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
}

install_local_stt_models() {
  log "Preparing local speech-to-text models (faster-whisper)"

  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    printf '[Archiveum] Cannot install STT models: missing venv python at %s\n' "$VENV_DIR/bin/python" >&2
    return 1
  fi

  local target_root="$PROJECT_DIR/models/faster-whisper"
  mkdir -p "$target_root"

  ARCHIVEUM_STT_TARGET_ROOT="$target_root" "$VENV_DIR/bin/python" - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import snapshot_download

target_root = Path(os.environ["ARCHIVEUM_STT_TARGET_ROOT"]).resolve()

models = [
    ("Systran/faster-whisper-tiny.en", target_root / "tiny.en"),
    ("Systran/faster-whisper-tiny", target_root / "tiny"),
]

for repo_id, target in models:
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(target),
        local_dir_use_symlinks=False,
    )
    print(f"[Archiveum] Speech model saved to {target}")
PY
}

configure_settings() {
  local voice_enabled_json="false"

  printf '\n[Archiveum] Enable voice mode in archiveum_settings.json? [y/N] '
  read -r enable_voice_now
  if [[ "${enable_voice_now:-n}" =~ ^[Yy]$ ]]; then
    voice_enabled_json="true"
  fi

  log "Patching archiveum_settings.json for project path and voice mode"
  SETTINGS_PATH="$SETTINGS_PATH" PROJECT_DIR="$PROJECT_DIR" VOICE_ENABLED_JSON="$voice_enabled_json" "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

settings_path = Path(os.environ["SETTINGS_PATH"])
project_dir = Path(os.environ["PROJECT_DIR"])
voice_enabled = os.environ["VOICE_ENABLED_JSON"].strip().lower() == "true"

if settings_path.exists():
    data = json.loads(settings_path.read_text(encoding="utf-8"))
else:
    data = {}

default_piper = project_dir / "piper-voices" / "en" / "en_GB" / "jenny_dioco" / "medium" / "en_GB-jenny_dioco-medium.onnx"
fallback_piper = project_dir / "models" / "piper" / "en_GB-northern_english_male-medium.onnx"

chosen_piper = default_piper if default_piper.exists() else fallback_piper

data["enable_voice"] = voice_enabled
data["piper_command"] = "piper"
data["piper_model_path"] = str(chosen_piper)

settings_path.write_text(
    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY

  log "Settings updated at $SETTINGS_PATH"
}

run_self_test() {
  log "Running Archiveum self-test"
  if "$VENV_DIR/bin/python" "$PROJECT_DIR/scripts/archiveum_self_test.py"; then
    log "Self-test passed"
  else
    printf '[Archiveum] Self-test reported warnings. Web UI may still work, but check the output above.\n'
  fi
}

install_service() {
  log "Rendering systemd service for user '$CURRENT_USER'"
  mkdir -p "$PROJECT_DIR/archiveum_data"
  sed \
    -e "s|__ARCHIVEUM_USER__|$CURRENT_USER|g" \
    -e "s|__ARCHIVEUM_PROJECT_DIR__|$PROJECT_DIR|g" \
    "$SERVICE_SRC" > "$RENDERED_SERVICE"

  log "Installing systemd service"
  sudo cp "$RENDERED_SERVICE" "$SERVICE_DEST"
  sudo systemctl daemon-reload
  sudo systemctl enable archiveum.service
  sudo systemctl restart archiveum.service
  sudo systemctl --no-pager --full status archiveum.service || true
}

enable_autostart() {
  if [[ "$ENABLE_AUTOSTART" != "true" ]]; then
    return
  fi

  log "Setting up browser autostart on Ubuntu"

  local autostart_dir="$HOME/.config/autostart"
  mkdir -p "$autostart_dir"

  local desktop_file="$autostart_dir/archiveum-browser.desktop"
  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=Archiveum Browser
Exec=$PROJECT_DIR/scripts/start_archiveum_browser.sh
Terminal=false
EOF

  chmod +x "$PROJECT_DIR/scripts/start_archiveum_browser.sh"

  log "Created desktop entry at: $desktop_file"
}

create_desktop_shortcuts() {
  local create_start="${1:-false}"
  local create_stop="${2:-false}"

  if [[ "$create_start" != "true" && "$create_stop" != "true" ]]; then
    return
  fi

  log "Creating desktop shortcuts"

  # Detect Desktop directory
  local desktop_dir="$HOME/Desktop"
  if [[ ! -d "$desktop_dir" && -d "$HOME/.local/share/desktop-directories" ]]; then
    desktop_dir="$HOME/.local/share/desktop-directories"
  fi
  mkdir -p "$desktop_dir"

  chmod +x "$PROJECT_DIR/scripts/start_archiveum_browser.sh"
  chmod +x "$PROJECT_DIR/scripts/stop_archiveum.sh"

  if [[ "$create_start" == "true" ]]; then
    local start_file="$desktop_dir/Start-Archiveum.desktop"
    cat > "$start_file" <<EOF
[Desktop Entry]
Type=Application
Name=Start Archiveum
Comment=Start Archiveum and open web interface
Exec=$PROJECT_DIR/scripts/start_archiveum_browser.sh
Icon=media-playback-start
Terminal=false
Categories=Utility;
EOF
    chmod +x "$start_file"
    log "Created desktop shortcut: Start Archiveum"
  fi

  if [[ "$create_stop" == "true" ]]; then
    local stop_file="$desktop_dir/Stop-Archiveum.desktop"
    cat > "$stop_file" <<EOF
[Desktop Entry]
Type=Application
Name=Stop Archiveum
Comment=Stop running Archiveum
Exec=$PROJECT_DIR/scripts/stop_archiveum.sh
Icon=media-playback-stop
Terminal=false
Categories=Utility;
EOF
    chmod +x "$stop_file"
    log "Created desktop shortcut: Stop Archiveum"
  fi
}

main() {
  require_cmd "$PYTHON_BIN"
  require_cmd sudo

  log "Project directory: $PROJECT_DIR"
  log "Detected service user: $CURRENT_USER"
  log "Using Python: $PYTHON_BIN"

  install_venv
  configure_settings

  SETTINGS_PATH="$SETTINGS_PATH" ollama_model_bootstrap

  if [[ "${INSTALL_STT_MODELS:-}" == "true" ]]; then
    install_local_stt_models
  else
    printf '\n[Archiveum] Download the local Whisper speech models now (tiny.en + tiny)? [y/N] '
    read -r install_stt_now
    if [[ "${install_stt_now:-n}" =~ ^[Yy]$ ]]; then
      install_local_stt_models
    else
      log "Skipping local speech model download"
      printf '[Archiveum] Voice mode will stay unavailable until you download a local STT model into: %s\n' "$PROJECT_DIR/models/faster-whisper"
      printf '[Archiveum] Re-run with INSTALL_STT_MODELS=true to download automatically.\n'
    fi
  fi

  run_self_test

  printf '\n[Archiveum] Install the systemd service now? [y/N] '
  read -r install_now
  if [[ "${install_now:-n}" =~ ^[Yy]$ ]]; then
    install_service
  else
    log "Skipping systemd installation"
    printf '[Archiveum] You can install it later with:\n'
    printf '  sed -e "s|__ARCHIVEUM_USER__|%s|g" -e "s|__ARCHIVEUM_PROJECT_DIR__|%s|g" %s | sudo tee %s >/dev/null\n' "$CURRENT_USER" "$PROJECT_DIR" "$SERVICE_SRC" "$SERVICE_DEST"
    printf '  sudo systemctl daemon-reload && sudo systemctl enable --now archiveum.service\n'
  fi

  printf '\n[Archiveum] Enable browser autostart on login? [y/N] '
  read -r enable_autostart_now
  if [[ "${enable_autostart_now:-n}" =~ ^[Yy]$ ]]; then
    ENABLE_AUTOSTART="true"
    enable_autostart
  else
    log "Skipping browser autostart setup"
  fi

  # Desktop shortcuts
  if [[ "$DESKTOP_START_SHORTCUT" == "true" || "$DESKTOP_STOP_SHORTCUT" == "true" ]]; then
    create_desktop_shortcuts "$DESKTOP_START_SHORTCUT" "$DESKTOP_STOP_SHORTCUT"
  else
    printf '\n[Archiveum] Create desktop shortcuts for starting/stopping Archiveum? [y/N] '
    read -r desktop_shortcuts
    if [[ "${desktop_shortcuts:-n}" =~ ^[Yy]$ ]]; then
      printf '[Archiveum] Create Start Archiveum shortcut? [y/N] '
      read -r start_shortcut
      printf '[Archiveum] Create Stop Archiveum shortcut? [y/N] '
      read -r stop_shortcut
      create_desktop_shortcuts "$([[ "$start_shortcut" =~ ^[Yy]$ ]] && echo true || echo false)" "$([[ "$stop_shortcut" =~ ^[Yy]$ ]] && echo true || echo false)"
    fi
  fi

  log "Done"
  printf '[Archiveum] Settings file: %s\n' "$SETTINGS_PATH"
  printf '[Archiveum] Start manually with: %s/bin/python %s/main.py\n' "$VENV_DIR" "$PROJECT_DIR"
}

main "$@"
