from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArchiveumPaths:
    base_dir: Path
    data_dir: Path
    uploads_dir: Path
    chunks_path: Path
    settings_path: Path
    status_path: Path


@dataclass(frozen=True)
class AppSettings:
    host: str
    port: int
    reload: bool
    enable_voice: bool
    ollama_chat_url: str
    ollama_chat_model: str
    ollama_embed_url: str
    ollama_embed_model: str
    ollama_timeout: int
    stt_model: str
    stt_device: str
    stt_compute_type: str
    piper_model_path: str
    piper_device: str
    voice_sample_rate: int
    voice_channels: int
    voice_frame_duration_ms: int
    voice_max_record_seconds: float
    voice_silence_timeout_seconds: float


def build_paths(base_dir: Path | None = None) -> ArchiveumPaths:
    root = (base_dir or Path(__file__).resolve().parent.parent).resolve()
    data_dir = root / "archiveum_data"
    uploads_dir = data_dir / "uploads"
    chunks_path = data_dir / "chunks.json"
    settings_path = root / "archiveum_settings.json"
    status_path = data_dir / "status.json"

    uploads_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    return ArchiveumPaths(
        base_dir=root,
        data_dir=data_dir,
        uploads_dir=uploads_dir,
        chunks_path=chunks_path,
        settings_path=settings_path,
        status_path=status_path,
    )


def load_settings(paths: ArchiveumPaths | None = None) -> AppSettings:
    resolved_paths = paths or build_paths()
    file_data = _load_settings_file(resolved_paths.settings_path)

    return AppSettings(
        host=_get_str("ARCHIVEUM_HOST", file_data, "host", "0.0.0.0"),
        port=_get_int("ARCHIVEUM_PORT", file_data, "port", 8000),
        reload=_get_bool("ARCHIVEUM_RELOAD", file_data, "reload", False),
        enable_voice=_get_bool("ARCHIVEUM_ENABLE_VOICE", file_data, "enable_voice", False),
        ollama_chat_url=_get_str("ARCHIVEUM_OLLAMA_URL", file_data, "ollama_chat_url", "http://127.0.0.1:11434/api/chat"),
        ollama_chat_model=_get_str("ARCHIVEUM_OLLAMA_MODEL", file_data, "ollama_chat_model", "llama3.1:8b"),
        ollama_embed_url=_get_str("ARCHIVEUM_EMBED_URL", file_data, "ollama_embed_url", "http://127.0.0.1:11434/api/embed"),
        ollama_embed_model=_get_str("ARCHIVEUM_EMBED_MODEL", file_data, "ollama_embed_model", "nomic-embed-text"),
        ollama_timeout=_get_int("ARCHIVEUM_OLLAMA_TIMEOUT", file_data, "ollama_timeout", 120),
        stt_model=_get_str("ARCHIVEUM_STT_MODEL", file_data, "stt_model", "tiny.en"),
        stt_device=_get_str("ARCHIVEUM_STT_DEVICE", file_data, "stt_device", "cpu"),
        stt_compute_type=_get_str("ARCHIVEUM_STT_COMPUTE_TYPE", file_data, "stt_compute_type", "float32"),
        piper_model_path=_get_str(
            "ARCHIVEUM_PIPER_MODEL",
            file_data,
            "piper_model_path",
            "/home/george/Archiveum/piper-voices/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx",
        ),
        piper_device=_get_str("ARCHIVEUM_PIPER_DEVICE", file_data, "piper_device", "plughw:0,0"),
        voice_sample_rate=_get_int("ARCHIVEUM_VOICE_SAMPLE_RATE", file_data, "voice_sample_rate", 16000),
        voice_channels=_get_int("ARCHIVEUM_VOICE_CHANNELS", file_data, "voice_channels", 1),
        voice_frame_duration_ms=_get_int("ARCHIVEUM_VOICE_FRAME_MS", file_data, "voice_frame_duration_ms", 30),
        voice_max_record_seconds=_get_float("ARCHIVEUM_VOICE_MAX_RECORD_SECONDS", file_data, "voice_max_record_seconds", 12.0),
        voice_silence_timeout_seconds=_get_float(
            "ARCHIVEUM_VOICE_SILENCE_TIMEOUT_SECONDS",
            file_data,
            "voice_silence_timeout_seconds",
            1.0,
        ),
    )


def ensure_settings_file(paths: ArchiveumPaths | None = None) -> Path:
    resolved_paths = paths or build_paths()
    if not resolved_paths.settings_path.exists():
        resolved_paths.settings_path.write_text(
            json.dumps(_default_settings_payload(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return resolved_paths.settings_path


def _load_settings_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _get_str(env_name: str, data: dict[str, Any], key: str, default: str) -> str:
    return str(os.getenv(env_name, data.get(key, default)))


def _get_int(env_name: str, data: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, data.get(key, default)))
    except Exception:
        return default


def _get_float(env_name: str, data: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(os.getenv(env_name, data.get(key, default)))
    except Exception:
        return default


def _get_bool(env_name: str, data: dict[str, Any], key: str, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        raw = data.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _default_settings_payload() -> dict[str, Any]:
    return {
        "host": "0.0.0.0",
        "port": 8000,
        "reload": False,
        "enable_voice": False,
        "ollama_chat_url": "http://127.0.0.1:11434/api/chat",
        "ollama_chat_model": "llama3.1:8b",
        "ollama_embed_url": "http://127.0.0.1:11434/api/embed",
        "ollama_embed_model": "nomic-embed-text",
        "ollama_timeout": 120,
        "stt_model": "tiny.en",
        "stt_device": "cpu",
        "stt_compute_type": "float32",
        "piper_model_path": "/home/george/Archiveum/piper-voices/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx",
        "piper_device": "plughw:0,0",
        "voice_sample_rate": 16000,
        "voice_channels": 1,
        "voice_frame_duration_ms": 30,
        "voice_max_record_seconds": 12.0,
        "voice_silence_timeout_seconds": 1.0,
    }
