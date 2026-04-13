from __future__ import annotations

import time

import uvicorn

from archiveum.assistant import ArchiveumAssistant
from archiveum.config import ensure_settings_file, load_settings
from archiveum.diagnostics import startup_messages


def main() -> None:
    settings_path = ensure_settings_file()
    shared_assistant = ArchiveumAssistant(settings=load_settings())
    diagnostics = shared_assistant.diagnostics()

    print("-> Starting Archiveum...")
    print(f"   Settings: {settings_path}")
    print(f"   Web UI: http://{shared_assistant.settings.host}:{shared_assistant.settings.port}")
    print(f"   Ollama chat URL: {shared_assistant.settings.ollama_chat_url}")
    print(f"   Ollama embed URL: {shared_assistant.settings.ollama_embed_url}")
    print("   Motion arbitration: disabled")
    print("   Retrieval: embeddings + vector similarity")
    for line in startup_messages(diagnostics):
        print(f"   {line}")
    print("   Voice mode: available in the Web UI")

    try:
        uvicorn.run(
            "archiveum.webapp:app",
            host=shared_assistant.settings.host,
            port=shared_assistant.settings.port,
            reload=shared_assistant.settings.reload,
        )
    finally:
        time.sleep(0.2)


if __name__ == "__main__":
    main()
