# Archiveum Overview

Archiveum is a local-first archive companion designed for Jetson Orin Nano and Ubuntu.

## Core capabilities

- Web UI for file upload and archive chat
- Local embeddings and vector retrieval through Ollama
- Optional voice input and speech output
- Runtime health, readiness, and admin diagnostics

## Main components

- `main.py` starts the Archiveum web service and optional voice loop
- `archiveum/` contains the application core
- `audio/stt.py` provides speech-to-text support
- `tts_piper.py` provides Piper-based speech output
- `deploy/archiveum.service` is the systemd unit template
- `install_archiveum.sh` automates Jetson setup

## Operational endpoints

- `/health/live`
- `/health/ready`
- `/status`
- `/admin`
