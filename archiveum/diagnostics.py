from __future__ import annotations

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

    checks = [
        ollama["chat_service"]["ok"],
        ollama["embed_service"]["ok"],
        ollama["chat_model"]["ok"],
        ollama["embed_model"]["ok"],
    ]
    ready = all(checks)
    voice_ready = piper["ok"] and audio["ok"]

    return {
        "ready": ready,
        "voice_ready": voice_ready,
        "settings": {
            "host": settings.host,
            "port": settings.port,
            "enable_voice": settings.enable_voice,
            "ollama_chat_model": settings.ollama_chat_model,
            "ollama_embed_model": settings.ollama_embed_model,
            "stt_model": settings.stt_model,
            "piper_model_path": settings.piper_model_path,
            "piper_device": settings.piper_device,
        },
        "ollama": ollama,
        "piper": piper,
        "audio": audio,
    }


def startup_messages(diagnostics: dict) -> list[str]:
    messages: list[str] = []

    for label, item in (
        ("Ollama chat service", diagnostics["ollama"]["chat_service"]),
        ("Ollama embed service", diagnostics["ollama"]["embed_service"]),
        ("Chat model", diagnostics["ollama"]["chat_model"]),
        ("Embedding model", diagnostics["ollama"]["embed_model"]),
        ("Piper", diagnostics["piper"]),
        ("Audio input", diagnostics["audio"]),
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
    binary_path = shutil.which("piper")
    model_exists = model_path.exists()
    ok = bool(binary_path) and model_exists

    missing: list[str] = []
    if not binary_path:
        missing.append("piper executable not found on PATH")
    if not model_exists:
        missing.append(f"model file missing: {model_path}")

    detail = "ready" if ok else "; ".join(missing)
    return {
        "ok": ok,
        "detail": detail,
        "binary": binary_path or "",
        "model_exists": model_exists,
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


def _model_status(model_name: str, available_models: list[str]) -> dict:
    if model_name in available_models:
        return {"ok": True, "detail": f"'{model_name}' is installed"}
    return {"ok": False, "detail": f"'{model_name}' is not installed"}


def _ollama_base_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    return f"{scheme}://{netloc}".rstrip("/")
