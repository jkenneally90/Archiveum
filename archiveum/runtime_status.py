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
                payload.setdefault("model_install", self._default_model_install())
                payload.setdefault("setup_wizard", self._default_setup_wizard())
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

    def set_model_install_state(self, updates: dict[str, Any]) -> None:
        payload = self.read()
        state = dict(payload.get("model_install", self._default_model_install()))
        state.update(updates)
        payload["model_install"] = state
        self._write(payload)

    def mark_setup_step(self, step_id: str, *, completed: bool, detail: str = "") -> None:
        payload = self.read()
        wizard = dict(payload.get("setup_wizard", self._default_setup_wizard()))
        steps = dict(wizard.get("completed_steps", {}))
        steps[step_id] = {
            "completed": completed,
            "detail": detail,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        wizard["completed_steps"] = steps
        wizard["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload["setup_wizard"] = wizard
        self._write(payload)

    def set_helper_script(self, *, path: str, ready: bool, note: str = "") -> None:
        payload = self.read()
        wizard = dict(payload.get("setup_wizard", self._default_setup_wizard()))
        wizard["helper_script_path"] = path
        wizard["helper_script_ready"] = ready
        wizard["helper_script_note"] = note
        wizard["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload["setup_wizard"] = wizard
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
            "model_install": self._default_model_install(),
            "setup_wizard": self._default_setup_wizard(),
        }

    def _default_model_install(self) -> dict[str, Any]:
        return {
            "active": False,
            "stage": "",
            "preset_id": "",
            "chat_model": "",
            "embed_model": "",
            "last_message": "",
            "last_error": "",
            "last_completed": None,
        }

    def _default_setup_wizard(self) -> dict[str, Any]:
        return {
            "completed_steps": {},
            "helper_script_path": "",
            "helper_script_ready": False,
            "helper_script_note": "",
            "last_updated": None,
        }
