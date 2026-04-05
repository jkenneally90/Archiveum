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

log() {
  printf '\n[Archiveum] %s\n' "$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '[Archiveum] Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

install_venv() {
  log "Creating virtual environment"
  "$PYTHON_BIN" -m venv "$VENV_DIR"

  log "Upgrading pip tooling"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

  log "Installing Python requirements"
  "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
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

main() {
  require_cmd "$PYTHON_BIN"
  require_cmd sudo

  log "Project directory: $PROJECT_DIR"
  log "Detected service user: $CURRENT_USER"
  log "Using Python: $PYTHON_BIN"

  install_venv
  configure_settings
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

  log "Done"
  printf '[Archiveum] Settings file: %s\n' "$SETTINGS_PATH"
  printf '[Archiveum] Start manually with: %s/bin/python %s/main.py\n' "$VENV_DIR" "$PROJECT_DIR"
}

main "$@"
