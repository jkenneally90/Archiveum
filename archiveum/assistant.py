from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from archiveum.config import AppSettings, ArchiveumPaths, build_paths, load_settings
from archiveum.diagnostics import collect_runtime_diagnostics
from archiveum.embeddings import ArchiveumEmbeddings
from archiveum.ingest import build_chunks, extract_text
from archiveum.llm import ArchiveumLLM
from archiveum.ollama_manager import OllamaManager
from archiveum.runtime_status import RuntimeStatus
from archiveum.store import ArchiveStore


@dataclass
class AskResult:
    question: str
    answer: str
    context: str
    matches: list[dict]
    mode: str = "chat"


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
        self.ollama_manager = OllamaManager(self)
        self._refresh_counts()

    @property
    def embedding_model(self) -> str:
        return self.embeddings.model

    def ingest_file(self, path: Path, *, source_name: str | None = None) -> int:
        try:
            text = extract_text(path)
            chunk_source = source_name or self._source_name_for_path(path)
            chunks = build_chunks(chunk_source, text)
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
                chunk_source,
                enriched_chunks,
                embedding_model=self.embeddings.model,
            )
            self.runtime_status.clear_ingestion_error(path.name)
            self._refresh_counts()
            return count
        except Exception as exc:
            self.runtime_status.record_ingestion_error(path.name, str(exc))
            raise

    def ask(
        self,
        question: str,
        *,
        limit: int = 4,
        avatar_context: str = "",
        memory_context_override: str | None = None,
        recent_chats_override: str | None = None,
        persona_id_override: str | None = None,
        prefer_archive_retrieval: bool = False,
    ) -> AskResult:
        query = (question or "").strip()
        if not query:
            return AskResult(question="", answer="", context="", matches=[], mode="chat")

        mode = self._choose_response_mode(query)
        matches: list[dict] = []
        context = ""
        memory_context = ""
        recent_chats = ""

        if mode == "archive":
            query_vector = self.embeddings.embed_text(query)
            matches = self.store.search_by_vector(
                query_vector,
                limit=8,
                embedding_model=self.embeddings.model,
            )
            context = self.store.build_context(matches)
        elif prefer_archive_retrieval:
            # Public Mode: Allow natural conversations but also search archive for potentially relevant info
            # Only switch to archive mode if query would normally trigger it, OR if we find highly relevant matches
            query_vector = self.embeddings.embed_text(query)
            matches = self.store.search_by_vector(
                query_vector,
                limit=8,
                embedding_model=self.embeddings.model,
            )
            # Only use archive context if we have a highly relevant match (top score > 0.4)
            # This prevents casual conversation from being hijacked by loosely related archive content
            if matches and matches[0].get("score", 0) > 0.3:
                mode = "archive"
                context = self.store.build_context(matches)
            else:
                matches = []
                matches = []
        else:
            # Chat mode - load memory context from webapp helpers unless explicitly overridden
            memory_context = memory_context_override if memory_context_override is not None else ""
            recent_chats = recent_chats_override if recent_chats_override is not None else ""
            if memory_context_override is None or recent_chats_override is None:
                try:
                    from archiveum.webapp import _get_memory_for_prompt, _get_recent_chats_for_prompt
                    if memory_context_override is None:
                        memory_context = _get_memory_for_prompt()
                    if recent_chats_override is None:
                        recent_chats = _get_recent_chats_for_prompt(limit=5)
                except Exception:
                    # If memory loading fails, continue without it
                    pass

        answer = self.llm.answer(
            query,
            context,
            mode=mode,
            memory_context=memory_context,
            recent_chats=recent_chats,
            avatar_context=avatar_context,
            persona_id_override=persona_id_override,
        )
        return AskResult(question=query, answer=answer, context=context, matches=matches, mode=mode)

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

    def reload_settings(self) -> None:
        self.settings = load_settings(self.paths)
        self.llm = ArchiveumLLM(settings=self.settings)
        self.embeddings = ArchiveumEmbeddings(settings=self.settings)
    
    def get_system_prompt_diagnostic(self) -> dict:
        """Get diagnostic info about the current system prompt configuration."""
        return {
            "current_persona_id": self.settings.current_persona_id,
            "custom_system_prompt_length": len(self.settings.custom_system_prompt or ""),
            "custom_system_prompt_active": bool((self.settings.custom_system_prompt or "").strip()),
            "custom_system_prompt_preview": (self.settings.custom_system_prompt or "")[:100],
            "system_prompt_used": self.llm._system_prompt()[:100],
        }

    def apply_model_selection(self, chat_model: str, embed_model: str) -> None:
        from archiveum.config import persist_settings

        persist_settings(
            self.paths,
            {
                "ollama_chat_model": chat_model,
                "ollama_embed_model": embed_model,
            },
        )
        self.reload_settings()

    def remove_source(self, source_name: str) -> int:
        removed = self.store.remove_document(source_name)
        self._refresh_counts()
        return removed

    def reindex_file(self, path: Path, *, source_name: str | None = None) -> int:
        chunk_source = source_name or self._source_name_for_path(path)
        self.store.remove_document(chunk_source)
        return self.ingest_file(path, source_name=chunk_source)

    def _source_name_for_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.paths.uploads_dir.resolve()).as_posix()
        except Exception:
            return path.name

    def _choose_response_mode(self, question: str) -> str:
        query = (question or "").strip()
        if not query:
            return "chat"

        lowered = query.lower()
        archive_phrases = (
            "in the archive",
            "from the archive",
            "in my archive",
            "from my archive",
            "from the documents",
            "in the documents",
            "from the files",
            "in the files",
            "search the archive",
            "search my archive",
            "search the documents",
            "look in the archive",
            "look through the archive",
            "find in the archive",
            "according to the archive",
            "according to the documents",
            "based on the archive",
            "based on the documents",
            "summarize the document",
            "summarise the document",
            "summarize this document",
            "summarise this document",
            "summarize the file",
            "summarise the file",
            "what does the document say",
            "what does the file say",
            "what do the documents say",
            "what do the files say",
            "quote the document",
            "quote the file",
            "read the document",
            "read the file",
            "open the document",
            "open the file",
            "check the indexed resources",
            "check the resources",
            "check indexed resources",
            "who are the",
            "who is the",
            "where is",
            "where are",
        )
        if any(phrase in lowered for phrase in archive_phrases):
            return "archive"

        if any(token in lowered for token in ("document", "documents", "file", "files", "pdf", "folder", "archive", "archives", "source", "sources", "upload", "uploads")):
            return "archive"

        if self._question_mentions_source_name(query):
            return "archive"

        return "chat"

    def _question_mentions_source_name(self, question: str) -> bool:
        normalized_question = _normalize_for_matching(question)
        for item in self.store.list_sources():
            source = str(item.get("source", "") or "").strip()
            if not source:
                continue
            source_path = source.replace("\\", "/")
            source_name = Path(source_path).name
            source_stem = Path(source_name).stem
            candidates = {
                _normalize_for_matching(source),
                _normalize_for_matching(source_name),
                _normalize_for_matching(source_stem),
            }
            if any(candidate and candidate in normalized_question for candidate in candidates):
                return True
        return False


def _normalize_for_matching(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
