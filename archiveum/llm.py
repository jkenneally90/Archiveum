from __future__ import annotations

import os

import requests

from archiveum.config import AppSettings


DEFAULT_SYSTEM_PROMPT = (
    "You are Archiveum, a calm and thoughtful archive companion. "
    "Answer using the retrieved context when it is relevant, be honest about uncertainty, "
    "and cite source filenames in plain language when possible."
)


class ArchiveumLLM:
    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        settings: AppSettings | None = None,
    ) -> None:
        if settings is not None:
            self.url = url or settings.ollama_chat_url
            self.model = model or settings.ollama_chat_model
            self.timeout = timeout if timeout != 120 else settings.ollama_timeout
        else:
            self.url = url or os.getenv("ARCHIVEUM_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
            self.model = model or os.getenv("ARCHIVEUM_OLLAMA_MODEL", "llama3.1:8b")
            self.timeout = timeout

    def answer(self, question: str, retrieved_context: str) -> str:
        prompt = (
            "Use the archive context if it helps answer the question.\n\n"
            f"Question:\n{question.strip()}\n\n"
            f"Archive context:\n{retrieved_context or 'No relevant archive context was found.'}\n\n"
            "Respond conversationally and keep the answer grounded in the provided context."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }

        response = requests.post(self.url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return data.get("message", {}).get("content", "").strip()
