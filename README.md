# Archiveum

Archiveum is a local archive companion built for Jetson Orin Nano and Ubuntu, with a web UI for file upload, grounded question answering, and optional voice interaction.

## Current status

- Local file ingestion, embeddings, and vector retrieval pipeline
- Optional voice mode layered on top of the same archive assistant core
- Admin console for runtime checks and ingestion-error cleanup
- Jetson deployment helper with `systemd` integration

## Run the new app

```bash
python main.py
```

Then open `http://localhost:8000`.

To enable voice input/output alongside the web UI:

```bash
python main.py
```

Set `enable_voice` inside `archiveum_settings.json`, or override any setting with environment variables.

## Health endpoints

- `GET /health/live`
- `GET /health/ready`
- `GET /status`

`/status` now includes:

- indexed document count
- indexed chunk count
- recent ingestion errors
- Ollama, Piper, and audio diagnostics

## Jetson deployment

Systemd unit:

- [deploy/archiveum.service](c:/Users/james/Documents/Archiveum/deploy/archiveum.service)

Self-test script:

- `python scripts/archiveum_self_test.py`
- `./install_archiveum.sh`

Suggested install flow on the Jetson:

```bash
cd ~/Archiveum
chmod +x install_archiveum.sh
./install_archiveum.sh
```

The installer now:

- creates or refreshes `.venv`
- installs Python requirements
- patches `archiveum_settings.json` for the detected project path
- lets you enable or disable voice mode during install
- runs the Archiveum self-test
- optionally installs and starts the `systemd` service

Manual flow is still available if you prefer:

```bash
cd ~/Archiveum
python scripts/archiveum_self_test.py
sudo cp deploy/archiveum.service /etc/systemd/system/archiveum.service
sudo systemctl daemon-reload
sudo systemctl enable archiveum.service
sudo systemctl start archiveum.service
sudo systemctl status archiveum.service
```

## Notes

- Retrieval now uses local embeddings through Ollama and cosine similarity over stored vectors.
- The default embedding model is `nomic-embed-text`; override with `ARCHIVEUM_EMBED_MODEL` if needed.
- Voice mode uses the same assistant core as the web UI, with `faster-whisper` for STT and Piper for TTS.
- On first startup, Archiveum writes `archiveum_settings.json` if it does not already exist.
- The admin console is available at `/admin` for viewing and clearing ingestion errors.
