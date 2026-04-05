from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class RuntimeStatus:
    def __init__(self, path: Path, max_errors: int = 20) -> None:
        self.path = path
        self.max_errors = max_errors

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_payload()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("recent_ingestion_errors", [])
                return payload
        except Exception:
            pass
        return self._default_payload()

    def record_ingestion_error(self, filename: str, error: str) -> None:
        payload = self.read()
        errors = list(payload.get("recent_ingestion_errors", []))
        errors.insert(
            0,
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "filename": filename,
                "error": error,
            },
        )
        payload["recent_ingestion_errors"] = errors[: self.max_errors]
        self._write(payload)

    def clear_ingestion_error(self, filename: str) -> None:
        payload = self.read()
        errors = [
            item
            for item in payload.get("recent_ingestion_errors", [])
            if item.get("filename") != filename
        ]
        payload["recent_ingestion_errors"] = errors
        self._write(payload)

    def clear_all_ingestion_errors(self) -> None:
        payload = self.read()
        payload["recent_ingestion_errors"] = []
        self._write(payload)

    def update_counts(self, *, documents: int, chunks: int) -> None:
        payload = self.read()
        payload["indexed_documents"] = documents
        payload["indexed_chunks"] = chunks
        payload["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _default_payload(self) -> dict[str, Any]:
        return {
            "indexed_documents": 0,
            "indexed_chunks": 0,
            "last_updated": None,
            "recent_ingestion_errors": [],
        }
