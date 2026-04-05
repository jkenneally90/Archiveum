from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


class ArchiveStore:
    def __init__(self, chunks_path: Path):
        self.chunks_path = chunks_path
        self._chunks: list[dict[str, Any]] = []
        self._load()

    def add_document(
        self,
        source_name: str,
        chunks: list[dict[str, Any]],
        *,
        embedding_model: str,
    ) -> int:
        normalized_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            normalized = dict(chunk)
            normalized["embedding_model"] = embedding_model
            normalized_chunks.append(normalized)

        self._chunks = [chunk for chunk in self._chunks if chunk.get("source") != source_name]
        self._chunks.extend(normalized_chunks)
        self._save()
        return len(normalized_chunks)

    def list_sources(self) -> list[dict[str, Any]]:
        self.reload()
        stats: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks:
            source = str(chunk.get("source", "unknown"))
            item = stats.setdefault(
                source,
                {"source": source, "chunks": 0, "embedding_model": chunk.get("embedding_model", "unknown")},
            )
            item["chunks"] += 1
            if item.get("embedding_model") in {None, "unknown"}:
                item["embedding_model"] = chunk.get("embedding_model", "unknown")

        return [stats[source] for source in sorted(stats)]

    def search_by_vector(
        self,
        query_vector: list[float],
        *,
        limit: int = 4,
        min_score: float = 0.20,
        embedding_model: str | None = None,
    ) -> list[dict[str, Any]]:
        self.reload()
        if not query_vector:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self._chunks:
            chunk_vector = chunk.get("embedding")
            if not isinstance(chunk_vector, list):
                continue
            if embedding_model and chunk.get("embedding_model") != embedding_model:
                continue

            score = _cosine_similarity(query_vector, chunk_vector)
            if score < min_score:
                continue

            enriched = dict(chunk)
            enriched["score"] = round(score, 4)
            scored.append((score, enriched))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

    def build_context(self, matches: list[dict[str, Any]]) -> str:
        if not matches:
            return ""

        lines = []
        for index, chunk in enumerate(matches, start=1):
            source = chunk.get("source", "unknown")
            score = chunk.get("score")
            text = str(chunk.get("text", "")).strip()
            score_suffix = f" (similarity {score})" if score is not None else ""
            lines.append(f"[{index}] Source: {source}{score_suffix}\n{text}")
        return "\n\n".join(lines)

    def stats(self) -> dict[str, int]:
        self.reload()
        return {
            "documents": len({str(chunk.get("source", "unknown")) for chunk in self._chunks}),
            "chunks": len(self._chunks),
        }

    def _load(self) -> None:
        if not self.chunks_path.exists():
            self._chunks = []
            return

        try:
            payload = json.loads(self.chunks_path.read_text(encoding="utf-8"))
            self._chunks = payload if isinstance(payload, list) else []
        except Exception:
            self._chunks = []

    def reload(self) -> None:
        self._load()

    def _save(self) -> None:
        self.chunks_path.write_text(
            json.dumps(self._chunks, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0

    return numerator / (left_norm * right_norm)
