from __future__ import annotations

import json
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archiveum.assistant import ArchiveumAssistant
from archiveum.config import ensure_settings_file
from archiveum.diagnostics import startup_messages


def main() -> int:
    settings_path = ensure_settings_file()
    assistant = ArchiveumAssistant()
    diagnostics = assistant.diagnostics()

    print("Archiveum self-test")
    print(f"Settings: {settings_path}")
    print(f"Base dir: {assistant.paths.base_dir}")
    print(f"Uploads dir: {assistant.paths.uploads_dir}")
    print(f"Status path: {assistant.paths.status_path}")
    print()

    for line in startup_messages(diagnostics):
        print(line)

    print()
    print("Index summary:")
    print(json.dumps(diagnostics["index"], indent=2, ensure_ascii=False))

    status_code = 0 if diagnostics["ready"] else 1
    if diagnostics["ready"]:
        print("\nArchiveum core services look ready.")
    else:
        print("\nArchiveum is not fully ready yet. Check Ollama reachability and model installation.")

    if diagnostics["voice_ready"]:
        print("Voice prerequisites look ready.")
    else:
        print("Voice prerequisites are incomplete. This does not block the web UI.")

    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
