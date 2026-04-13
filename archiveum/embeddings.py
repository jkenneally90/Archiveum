from __future__ import annotations

import os

import requests

from archiveum.config import AppSettings


class ArchiveumEmbeddings:
    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        settings: AppSettings | None = None,
    ) -> None:
        if settings is not None:
            self.url = url or settings.ollama_embed_url
            self.model = model or settings.ollama_embed_model
            self.timeout = timeout if timeout != 120 else settings.ollama_timeout
        else:
            self.url = url or os.getenv("ARCHIVEUM_EMBED_URL", "http://127.0.0.1:11434/api/embed")
            self.model = model or os.getenv("ARCHIVEUM_EMBED_MODEL", "nomic-embed-text")
            self.timeout = timeout

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        payload = {
            "model": self.model,
            "input": texts,
        }
        try:
            response = requests.post(self.url, json=payload, timeout=self.timeout)
            if not response.ok:
                body = (response.text or "").strip()
                detail = f"HTTP {response.status_code}"
                if body:
                    detail += f"; response={body}"
                raise RuntimeError(f"Ollama embedding request failed for {self.url}: {detail}")
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Ollama embedding request failed for {self.url}: {exc}") from exc

        embeddings = data.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            return embeddings

        single = data.get("embedding")
        if isinstance(single, list):
            return [single]

        raise RuntimeError("Embedding response did not include vectors.")

    def embed_text(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        return vectors[0] if vectors else []
