from __future__ import annotations

import importlib.util
import platform
import shutil
from pathlib import Path
from urllib.parse import urlparse

import sounddevice as sd
import requests

from archiveum.config import AppSettings


def collect_runtime_diagnostics(settings: AppSettings) -> dict:
    ollama = _check_ollama(settings)
    piper = _check_piper(settings)
    audio = _check_audio(settings)
    voice_python = _check_voice_python()
    stt_model = _check_stt_model(settings)

    checks = [
        ollama["chat_service"]["ok"],
        ollama["embed_service"]["ok"],
        ollama["chat_model"]["ok"],
        ollama["embed_model"]["ok"],
    ]
    ready = all(checks)
    voice_ready = piper["ok"] and audio["ok"] and voice_python["ok"] and stt_model["ok"]

    return {
        "ready": ready,
        "voice_ready": voice_ready,
        "settings": {
            "host": settings.host,
            "port": settings.port,
            "enable_voice": settings.enable_voice,
            "speak_responses": settings.speak_responses,
            "current_persona_id": settings.current_persona_id,
            "custom_system_prompt": settings.custom_system_prompt,
            "ollama_chat_model": settings.ollama_chat_model,
            "ollama_embed_model": settings.ollama_embed_model,
            "stt_model": settings.stt_model,
            "piper_command": settings.piper_command,
            "piper_model_path": settings.piper_model_path,
            "piper_device": settings.piper_device,
        },
        "ollama": ollama,
        "stt_model": stt_model,
        "piper": piper,
        "audio": audio,
        "voice_python": voice_python,
    }


def startup_messages(diagnostics: dict) -> list[str]:
    messages: list[str] = []

    for label, item in (
        ("Ollama chat service", diagnostics["ollama"]["chat_service"]),
        ("Ollama embed service", diagnostics["ollama"]["embed_service"]),
        ("Chat model", diagnostics["ollama"]["chat_model"]),
        ("Embedding model", diagnostics["ollama"]["embed_model"]),
        ("Local STT model", diagnostics["stt_model"]),
        ("Piper", diagnostics["piper"]),
        ("Audio input", diagnostics["audio"]),
        ("Voice Python", diagnostics["voice_python"]),
    ):
        prefix = "OK" if item["ok"] else "WARN"
        details = item.get("detail", "")
        messages.append(f"[{prefix}] {label}: {details}")

    return messages


def _check_ollama(settings: AppSettings) -> dict:
    base_url = _ollama_base_url(settings.ollama_chat_url or settings.ollama_embed_url)
    models_url = f"{base_url}/api/tags"

    try:
        response = requests.get(models_url, timeout=min(settings.ollama_timeout, 10))
        response.raise_for_status()
        payload = response.json()
        models = [item.get("name", "") for item in payload.get("models", []) if item.get("name")]
        return {
            "chat_service": {"ok": True, "detail": f"reachable at {base_url}"},
            "embed_service": {"ok": True, "detail": f"reachable at {base_url}"},
            "chat_model": _model_status(settings.ollama_chat_model, models),
            "embed_model": _model_status(settings.ollama_embed_model, models),
            "available_models": models,
        }
    except Exception as exc:
        detail = f"unreachable at {base_url}: {exc}"
        unavailable = {"ok": False, "detail": detail}
        return {
            "chat_service": unavailable,
            "embed_service": unavailable,
            "chat_model": {"ok": False, "detail": f"cannot verify '{settings.ollama_chat_model}' until Ollama responds"},
            "embed_model": {"ok": False, "detail": f"cannot verify '{settings.ollama_embed_model}' until Ollama responds"},
            "available_models": [],
        }


def _check_piper(settings: AppSettings) -> dict:
    model_path = Path(settings.piper_model_path)
    binary_path = _resolve_binary(settings.piper_command)
    model_exists = model_path.exists()
    system_name = platform.system().lower()
    playback_backend = "winsound" if system_name == "windows" else "aplay"
    playback_ok = True if system_name == "windows" else bool(shutil.which("aplay"))
    ok = bool(binary_path) and model_exists and playback_ok

    missing: list[str] = []
    if not binary_path:
        missing.append("piper executable not found on PATH")
    if not model_exists:
        missing.append(f"model file missing: {model_path}")
    if not playback_ok:
        missing.append("aplay not found on PATH")

    detail = "ready" if ok else "; ".join(missing)
    return {
        "ok": ok,
        "detail": detail,
        "binary": binary_path or "",
        "model_exists": model_exists,
        "playback_backend": playback_backend,
        "platform": system_name,
        "hint": _piper_hint(system_name, settings.piper_command),
    }


def _check_audio(settings: AppSettings) -> dict:
    try:
        devices = sd.query_devices()
        input_devices = [
            {
                "name": device["name"],
                "max_input_channels": device["max_input_channels"],
                "default_samplerate": device["default_samplerate"],
            }
            for device in devices
            if device.get("max_input_channels", 0) > 0
        ]
        ok = len(input_devices) > 0
        detail = f"{len(input_devices)} input device(s) found"
        return {
            "ok": ok,
            "detail": detail,
            "devices": input_devices[:10],
            "requested_channels": settings.voice_channels,
            "requested_sample_rate": settings.voice_sample_rate,
        }
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"audio query failed: {exc}",
            "devices": [],
            "requested_channels": settings.voice_channels,
            "requested_sample_rate": settings.voice_sample_rate,
        }


def _check_stt_model(settings: AppSettings) -> dict:
    model_path = Path(settings.stt_model)
    if model_path.exists():
        return {
            "ok": True,
            "detail": f"local model found at {model_path}",
            "path": str(model_path),
        }
    return {
        "ok": False,
        "detail": f"local STT model missing at {model_path}",
        "path": str(model_path),
    }


def _model_status(model_name: str, available_models: list[str]) -> dict:
    if _model_installed(model_name, available_models):
        return {"ok": True, "detail": f"'{model_name}' is installed"}
    return {"ok": False, "detail": f"'{model_name}' is not installed"}


def _model_installed(model_name: str, available_models: list[str]) -> bool:
    target = (model_name or "").strip()
    if not target:
        return False

    normalized_available = {_normalize_model_name(item) for item in available_models if item}
    return _normalize_model_name(target) in normalized_available


def _normalize_model_name(model_name: str) -> str:
    raw = (model_name or "").strip()
    if raw.endswith(":latest"):
        return raw[: -len(":latest")]
    return raw


def _ollama_base_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}".rstrip("/")


def _check_voice_python() -> dict:
    stt_ready = _module_importable("faster_whisper")
    vad_ready = _module_importable("webrtcvad")
    missing = []
    warnings = []

    if not stt_ready:
        missing.append("faster_whisper")
    if not vad_ready:
        warnings.append("webrtcvad unavailable; Archiveum will use a simpler voice activity fallback")

    if stt_ready:
        detail = "voice dependencies available"
        if warnings:
            detail += " (" + "; ".join(warnings) + ")"
        return {"ok": True, "detail": detail, "missing": missing, "warnings": warnings}
    return {
        "ok": False,
        "detail": "missing Python packages: " + ", ".join(missing),
        "missing": missing,
        "warnings": warnings,
    }


def _module_importable(module_name: str) -> bool:
    if importlib.util.find_spec(module_name) is None:
        return False
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


def _resolve_binary(command_or_path: str) -> str:
    raw = (command_or_path or "").strip()
    if not raw:
        return ""
    direct = Path(raw)
    if direct.exists():
        return str(direct)
    return shutil.which(raw) or ""


def _piper_hint(system_name: str, command_or_path: str) -> str:
    if system_name == "windows":
        return (
            "Install Piper for Windows, then either add `piper.exe` to PATH or set "
            "`piper_command` in archiveum_settings.json to the full executable path."
        )
    return (
        "Install Piper on the Jetson and make sure the `piper` command is available on PATH."
    )
