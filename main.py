from __future__ import annotations

import time

import uvicorn

from archiveum.assistant import ArchiveumAssistant
from archiveum.config import ensure_settings_file, load_settings
from archiveum.diagnostics import startup_messages
from archiveum.voice import ArchiveumVoiceAssistant


def main() -> None:
    settings_path = ensure_settings_file()
    shared_assistant = ArchiveumAssistant(settings=load_settings())
    diagnostics = shared_assistant.diagnostics()

    print("-> Starting Archiveum...")
    print(f"   Settings: {settings_path}")
    print(f"   Web UI: http://{shared_assistant.settings.host}:{shared_assistant.settings.port}")
    print("   Motion arbitration: disabled")
    print("   Retrieval: embeddings + vector similarity")
    for line in startup_messages(diagnostics):
        print(f"   {line}")

    voice_assistant: ArchiveumVoiceAssistant | None = None
    if shared_assistant.settings.enable_voice:
        voice_assistant = ArchiveumVoiceAssistant(shared_assistant)
        started, detail = voice_assistant.start()
        print(f"   Voice mode: {'enabled' if started else 'disabled'}")
        print(f"   Voice detail: {detail}")
    else:
        print("   Voice mode: disabled")

    try:
        uvicorn.run(
            "archiveum.webapp:app",
            host=shared_assistant.settings.host,
            port=shared_assistant.settings.port,
            reload=shared_assistant.settings.reload,
        )
    finally:
        if voice_assistant is not None:
            voice_assistant.stop()
            time.sleep(0.2)


if __name__ == "__main__":
    main()
