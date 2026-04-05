from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from archiveum.config import AppSettings, ArchiveumPaths, build_paths, load_settings
from archiveum.diagnostics import collect_runtime_diagnostics
from archiveum.embeddings import ArchiveumEmbeddings
from archiveum.ingest import build_chunks, extract_text
from archiveum.llm import ArchiveumLLM
from archiveum.runtime_status import RuntimeStatus
from archiveum.store import ArchiveStore


@dataclass
class AskResult:
    question: str
    answer: str
    context: str
    matches: list[dict]


class ArchiveumAssistant:
    def __init__(
        self,
        *,
        paths: ArchiveumPaths | None = None,
        settings: AppSettings | None = None,
        store: ArchiveStore | None = None,
        llm: ArchiveumLLM | None = None,
        embeddings: ArchiveumEmbeddings | None = None,
    ) -> None:
        self.paths = paths or build_paths()
        self.settings = settings or load_settings(self.paths)
        self.store = store or ArchiveStore(self.paths.chunks_path)
        self.llm = llm or ArchiveumLLM(settings=self.settings)
        self.embeddings = embeddings or ArchiveumEmbeddings(settings=self.settings)
        self.runtime_status = RuntimeStatus(self.paths.status_path)
        self._refresh_counts()

    @property
    def embedding_model(self) -> str:
        return self.embeddings.model

    def ingest_file(self, path: Path) -> int:
        try:
            text = extract_text(path)
            chunks = build_chunks(path.name, text)
            if not chunks:
                self.runtime_status.record_ingestion_error(path.name, "The uploaded file did not contain usable text.")
                return 0

            vectors = self.embeddings.embed_texts([str(chunk["text"]) for chunk in chunks])
            if len(vectors) != len(chunks):
                raise RuntimeError("Embedding count did not match chunk count.")

            enriched_chunks = []
            for chunk, vector in zip(chunks, vectors):
                enriched = dict(chunk)
                enriched["embedding"] = vector
                enriched_chunks.append(enriched)

            count = self.store.add_document(
                path.name,
                enriched_chunks,
                embedding_model=self.embeddings.model,
            )
            self.runtime_status.clear_ingestion_error(path.name)
            self._refresh_counts()
            return count
        except Exception as exc:
            self.runtime_status.record_ingestion_error(path.name, str(exc))
            raise

    def ask(self, question: str, *, limit: int = 4) -> AskResult:
        query = (question or "").strip()
        if not query:
            return AskResult(question="", answer="", context="", matches=[])

        query_vector = self.embeddings.embed_text(query)
        matches = self.store.search_by_vector(
            query_vector,
            limit=limit,
            embedding_model=self.embeddings.model,
        )
        context = self.store.build_context(matches)

        if not context:
            answer = "I couldn't find relevant archive context yet. Upload a few files and try again."
            return AskResult(question=query, answer=answer, context="", matches=[])

        answer = self.llm.answer(query, context)
        return AskResult(question=query, answer=answer, context=context, matches=matches)

    def diagnostics(self) -> dict:
        diagnostics = collect_runtime_diagnostics(self.settings)
        diagnostics["index"] = self.status_summary()
        return diagnostics

    def status_summary(self) -> dict:
        stats = self.store.stats()
        self.runtime_status.update_counts(
            documents=stats["documents"],
            chunks=stats["chunks"],
        )
        return self.runtime_status.read()

    def _refresh_counts(self) -> None:
        stats = self.store.stats()
        self.runtime_status.update_counts(
            documents=stats["documents"],
            chunks=stats["chunks"],
        )
