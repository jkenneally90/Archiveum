from __future__ import annotations

import ipaddress
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


@dataclass(frozen=True)
class ArchiveumPaths:
    base_dir: Path
    data_dir: Path
    uploads_dir: Path
    avatars_dir: Path
    chunks_path: Path
    settings_path: Path
    status_path: Path
    helper_status_path: Path


@dataclass(frozen=True)
class AppSettings:
    host: str
    port: int
    reload: bool
    enable_voice: bool
    speak_responses: bool
    current_persona_id: str
    custom_system_prompt: str
    ollama_chat_url: str
    ollama_chat_model: str
    ollama_embed_url: str
    ollama_embed_model: str
    ollama_timeout: int
    stt_model: str
    stt_device: str
    stt_compute_type: str
    piper_command: str
    piper_model_path: str
    piper_device: str
    voice_sample_rate: int
    voice_channels: int
    voice_frame_duration_ms: int
    voice_max_record_seconds: float
    voice_silence_timeout_seconds: float
    voice_post_speech_delay_seconds: float
    custom_upload_categories: list[dict[str, str]]
    custom_personas: list[dict[str, str]]
    persona_avatars: dict[str, str]
    # Public Mode settings
    public_mode: bool  # True = public mode (restricted UI), False = admin mode
    public_mode_persona_id: str  # Fixed persona for public mode
    admin_password_hash: str  # Hashed password for admin access
    session_timeout_minutes: int  # Session expiry timeout


def build_paths(base_dir: Path | None = None) -> ArchiveumPaths:
    root = (base_dir or Path(__file__).resolve().parent.parent).resolve()
    data_dir = root / "archiveum_data"
    uploads_dir = data_dir / "uploads"
    avatars_dir = data_dir / "avatars"
    chunks_path = data_dir / "chunks.json"
    settings_path = root / "archiveum_settings.json"
    status_path = data_dir / "status.json"
    helper_status_path = data_dir / "helper_install_status.json"

    uploads_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    return ArchiveumPaths(
        base_dir=root,
        data_dir=data_dir,
        uploads_dir=uploads_dir,
        avatars_dir=avatars_dir,
        chunks_path=chunks_path,
        settings_path=settings_path,
        status_path=status_path,
        helper_status_path=helper_status_path,
    )


def load_settings(paths: ArchiveumPaths | None = None) -> AppSettings:
    resolved_paths = paths or build_paths()
    file_data = _load_settings_file(resolved_paths.settings_path)
    normalized_data = _normalized_settings_data(file_data, resolved_paths)
    if normalized_data != file_data:
        resolved_paths.settings_path.write_text(
            json.dumps(normalized_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    file_data = normalized_data
    default_piper_model = _resolve_default_piper_model_path(resolved_paths)
    default_stt_model = _resolve_default_stt_model_path(resolved_paths)

    return AppSettings(
        host=_get_str("ARCHIVEUM_HOST", file_data, "host", "0.0.0.0"),
        port=_get_int("ARCHIVEUM_PORT", file_data, "port", 8000),
        reload=_get_bool("ARCHIVEUM_RELOAD", file_data, "reload", False),
        enable_voice=_get_bool("ARCHIVEUM_ENABLE_VOICE", file_data, "enable_voice", False),
        speak_responses=_get_bool("ARCHIVEUM_SPEAK_RESPONSES", file_data, "speak_responses", True),
        current_persona_id=_get_str("ARCHIVEUM_CURRENT_PERSONA_ID", file_data, "current_persona_id", "nova"),
        custom_system_prompt=_get_str("ARCHIVEUM_CUSTOM_SYSTEM_PROMPT", file_data, "custom_system_prompt", ""),
        ollama_chat_url=_normalize_url(_get_str("ARCHIVEUM_OLLAMA_URL", file_data, "ollama_chat_url", "http://127.0.0.1:11434/api/chat")),
        ollama_chat_model=_get_str("ARCHIVEUM_OLLAMA_MODEL", file_data, "ollama_chat_model", "llama3.1:8b"),
        ollama_embed_url=_normalize_url(_get_str("ARCHIVEUM_EMBED_URL", file_data, "ollama_embed_url", "http://127.0.0.1:11434/api/embed")),
        ollama_embed_model=_get_str("ARCHIVEUM_EMBED_MODEL", file_data, "ollama_embed_model", "nomic-embed-text"),
        ollama_timeout=_get_int("ARCHIVEUM_OLLAMA_TIMEOUT", file_data, "ollama_timeout", 120),
        stt_model=_get_str("ARCHIVEUM_STT_MODEL", file_data, "stt_model", default_stt_model),
        stt_device=_get_str("ARCHIVEUM_STT_DEVICE", file_data, "stt_device", "cpu"),
        stt_compute_type=_get_str("ARCHIVEUM_STT_COMPUTE_TYPE", file_data, "stt_compute_type", "float32"),
        piper_command=_get_str("ARCHIVEUM_PIPER_COMMAND", file_data, "piper_command", "piper"),
        piper_model_path=_get_str(
            "ARCHIVEUM_PIPER_MODEL",
            file_data,
            "piper_model_path",
            default_piper_model,
        ),
        piper_device=_get_str("ARCHIVEUM_PIPER_DEVICE", file_data, "piper_device", _default_piper_device()),
        voice_sample_rate=_get_int("ARCHIVEUM_VOICE_SAMPLE_RATE", file_data, "voice_sample_rate", 16000),
        voice_channels=_get_int("ARCHIVEUM_VOICE_CHANNELS", file_data, "voice_channels", 1),
        voice_frame_duration_ms=_get_int("ARCHIVEUM_VOICE_FRAME_MS", file_data, "voice_frame_duration_ms", 30),
        voice_max_record_seconds=_get_float("ARCHIVEUM_VOICE_MAX_RECORD_SECONDS", file_data, "voice_max_record_seconds", 12.0),
        voice_silence_timeout_seconds=_get_float(
            "ARCHIVEUM_VOICE_SILENCE_TIMEOUT_SECONDS",
            file_data,
            "voice_silence_timeout_seconds",
            2.0,
        ),
        voice_post_speech_delay_seconds=_get_float(
            "ARCHIVEUM_VOICE_POST_SPEECH_DELAY_SECONDS",
            file_data,
            "voice_post_speech_delay_seconds",
            2.5,
        ),
        custom_upload_categories=_get_custom_upload_categories(file_data),
        custom_personas=_get_custom_personas(file_data),
        persona_avatars=_get_persona_avatars(file_data),
        # Public Mode settings with defaults
        public_mode=_get_bool("ARCHIVEUM_PUBLIC_MODE", file_data, "public_mode", False),
        public_mode_persona_id=_get_str("ARCHIVEUM_PUBLIC_MODE_PERSONA_ID", file_data, "public_mode_persona_id", "nova"),
        admin_password_hash=_get_str("ARCHIVEUM_ADMIN_PASSWORD_HASH", file_data, "admin_password_hash", ""),
        session_timeout_minutes=_get_int("ARCHIVEUM_SESSION_TIMEOUT_MINUTES", file_data, "session_timeout_minutes", 30),
    )


def ensure_settings_file(paths: ArchiveumPaths | None = None) -> Path:
    resolved_paths = paths or build_paths()
    if not resolved_paths.settings_path.exists():
        resolved_paths.settings_path.write_text(
            json.dumps(_default_settings_payload(resolved_paths), indent=2, ensure_ascii=False) + "\n",
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


def _normalize_url(raw_url: str) -> str:
    raw_url = str(raw_url or "").strip()
    if not raw_url:
        return raw_url
    if "://" not in raw_url:
        raw_url = "http://" + raw_url
    try:
        parsed = urlparse(raw_url)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname
        port = parsed.port
        if not hostname:
            return raw_url
        try:
            hostname = ipaddress.ip_address(hostname).compressed
        except ValueError:
            hostname = hostname
        netloc = hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        return raw_url


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


def _get_custom_upload_categories(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("custom_upload_categories", [])
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path", "") or "").replace("\\", "/").strip()
        raw_label = str(item.get("label", "") or "").strip()
        if not raw_path or not raw_label:
            continue
        parts = [part for part in raw_path.split("/") if part and part not in {".", ".."}]
        if not parts:
            continue
        normalized_path = Path("/".join(parts)).as_posix()
        normalized.append({"path": normalized_path, "label": raw_label})

    return normalized


def _get_custom_personas(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("custom_personas", [])
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        persona_id = str(item.get("id", "") or "").strip()
        name = str(item.get("name", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        system_prompt = str(item.get("system_prompt", "") or "").strip()
        avatar = str(item.get("avatar", "") or "").strip()
        llm_model = str(item.get("llm_model", "") or "").strip()
        voice_model = str(item.get("voice_model", "") or "").strip()
        if not persona_id or not name or not system_prompt:
            continue
        normalized.append({
            "id": persona_id,
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "avatar": avatar,
            "llm_model": llm_model,
            "voice_model": voice_model,
        })

    return normalized


def _get_persona_avatars(data: dict[str, Any]) -> dict[str, str]:
    raw = data.get("persona_avatars", {})
    if not isinstance(raw, dict):
        return {}
    # Ensure all keys and values are strings
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _default_settings_payload(paths: ArchiveumPaths) -> dict[str, Any]:
    return {
        "host": "0.0.0.0",
        "port": 8000,
        "reload": False,
        "enable_voice": False,
        "speak_responses": True,
        "current_persona_id": "nova",
        "custom_system_prompt": "",
        "ollama_chat_url": "http://127.0.0.1:11434/api/chat",
        "ollama_chat_model": "llama3.1:8b",
        "ollama_embed_url": "http://127.0.0.1:11434/api/embed",
        "ollama_embed_model": "nomic-embed-text",
        "ollama_timeout": 120,
        "stt_model": _resolve_default_stt_model_path(paths),
        "stt_device": "cpu",
        "stt_compute_type": "float32",
        "piper_command": "piper",
        "piper_model_path": _resolve_default_piper_model_path(paths),
        "piper_device": _default_piper_device(),
        "voice_sample_rate": 16000,
        "voice_channels": 1,
        "voice_frame_duration_ms": 30,
        "voice_max_record_seconds": 12.0,
        "voice_silence_timeout_seconds": 2.0,
        "voice_post_speech_delay_seconds": 2.5,
        "custom_upload_categories": [],
        "custom_personas": [],
        # Public Mode defaults
        "public_mode": False,
        "public_mode_persona_id": "nova",
        "admin_password_hash": "",
        "session_timeout_minutes": 30,
    }


def _convert_windows_path_to_platform(path_str: str, paths: ArchiveumPaths) -> str | None:
    """Convert Windows-style paths to current platform paths."""
    if not path_str:
        return None
    # Detect Windows absolute paths (C:\ or C:/)
    if path_str[1:3] == ":\\" or path_str[1:3] == ":/":
        # Extract path after "Archiveum\" or "Archiveum/"
        parts = path_str.replace("\\", "/").split("/")
        try:
            archiveum_idx = parts.index("Archiveum")
            # Rebuild relative path from Archiveum onwards
            relative_parts = parts[archiveum_idx + 1:]
            new_path = paths.base_dir.joinpath(*relative_parts)
            return str(new_path)
        except ValueError:
            pass
    return None


def _normalized_settings_data(data: dict[str, Any], paths: ArchiveumPaths) -> dict[str, Any]:
    normalized = dict(data)
    detected_piper_model = _resolve_default_piper_model_path(paths)
    current_piper_model = str(normalized.get("piper_model_path", "")).strip()
    default_piper_device = _default_piper_device()
    default_stt_model = _resolve_default_stt_model_path(paths)

    # Auto-convert Windows paths to current platform
    converted_piper = _convert_windows_path_to_platform(current_piper_model, paths)
    if converted_piper:
        print(f"[Settings] Converting Windows piper_model_path to: {converted_piper}")
        current_piper_model = converted_piper
        normalized["piper_model_path"] = converted_piper

    if not current_piper_model or not Path(current_piper_model).exists():
        normalized["piper_model_path"] = detected_piper_model

    current_piper_command = str(normalized.get("piper_command", "")).strip()
    if not current_piper_command:
        normalized["piper_command"] = "piper"

    current_stt_model = str(normalized.get("stt_model", "")).strip()
    # Auto-convert Windows paths for STT model
    converted_stt = _convert_windows_path_to_platform(current_stt_model, paths)
    if converted_stt:
        print(f"[Settings] Converting Windows stt_model to: {converted_stt}")
        current_stt_model = converted_stt
        normalized["stt_model"] = converted_stt

    if not current_stt_model:
        normalized["stt_model"] = default_stt_model
    elif not Path(current_stt_model).exists() and current_stt_model.lower() in {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
    }:
        normalized["stt_model"] = default_stt_model

    current_piper_device = str(normalized.get("piper_device", "")).strip()
    if not current_piper_device:
        normalized["piper_device"] = default_piper_device
    elif platform.system().lower() == "windows" and current_piper_device == "plughw:0,0":
        normalized["piper_device"] = default_piper_device
    elif platform.system().lower() != "windows" and current_piper_device == "windows-default":
        normalized["piper_device"] = default_piper_device

    normalized["custom_upload_categories"] = _get_custom_upload_categories(normalized)
    normalized["custom_personas"] = _get_custom_personas(normalized)
    return normalized


def _resolve_default_piper_model_path(paths: ArchiveumPaths) -> str:
    candidates = [
        paths.base_dir / "piper-voices" / "en" / "en_GB" / "jenny_dioco" / "medium" / "en_GB-jenny_dioco-medium.onnx",
        paths.base_dir / "piper-voices" / "en" / "en_GB" / "northern_english_male" / "medium" / "en_GB-northern_english_male-medium.onnx",
        paths.base_dir / "models" / "piper" / "en_GB-northern_english_male-medium.onnx",
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return str(candidates[0])


def _resolve_default_stt_model_path(paths: ArchiveumPaths) -> str:
    platform_name = platform.system().lower()
    if platform_name == "windows":
        preferred_models = ("base.en", "small.en", "tiny.en")
    else:
        preferred_models = ("tiny.en", "base.en", "small.en")

    candidates: list[Path] = []
    for model_name in preferred_models:
        candidates.append(paths.base_dir / "models" / "faster-whisper" / model_name)
        candidates.append(paths.base_dir / "models" / "stt" / model_name)

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return str(candidates[0])


def _detect_linux_audio_device() -> str:
    """Detect best audio output device - prefer ReSpeaker, then USB audio, then fallback."""
    try:
        import subprocess
        result = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.split("\n")
            respeaker_card = None
            usb_card = None
            for line in lines:
                line_lower = line.lower()
                # Look for ReSpeaker variants (respeaker, ArrayUAC10, 4 mic array, etc.)
                if any(x in line_lower for x in ["respeaker", "arrayuac", "4 mic array", "mic array"]):
                    parts = line.split()
                    if parts and parts[0] == "card":
                        try:
                            respeaker_card = int(parts[1].rstrip(":"))
                            print(f"[Audio] ReSpeaker detected on card {respeaker_card}")
                            return f"plughw:{respeaker_card},0"
                        except (ValueError, IndexError):
                            pass
                # Look for any USB audio device as fallback
                if "usb" in line_lower and "audio" in line_lower:
                    parts = line.split()
                    if parts and parts[0] == "card":
                        try:
                            usb_card = int(parts[1].rstrip(":"))
                        except (ValueError, IndexError):
                            pass
            # Use USB card if found but no ReSpeaker
            if usb_card is not None:
                print(f"[Audio] USB audio detected on card {usb_card}")
                return f"plughw:{usb_card},0"
    except Exception as e:
        print(f"[Audio] Device detection failed: {e}, using default")
    return "plughw:0,0"


def _default_piper_device() -> str:
    if platform.system().lower() == "windows":
        return "windows-default"
    return _detect_linux_audio_device()


def persist_settings(paths: ArchiveumPaths | None = None, updates: dict[str, Any] | None = None) -> Path:
    resolved_paths = paths or build_paths()
    ensure_settings_file(resolved_paths)
    existing = _load_settings_file(resolved_paths.settings_path)
    merged = dict(existing)
    merged.update(updates or {})
    normalized = _normalized_settings_data(merged, resolved_paths)
    resolved_paths.settings_path.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return resolved_paths.settings_path
