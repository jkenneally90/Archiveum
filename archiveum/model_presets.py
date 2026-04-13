from __future__ import annotations

import platform
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelPreset:
    id: str
    name: str
    target: str
    chat_model: str
    embed_model: str
    description: str


PRESETS = [
    ModelPreset(
        id="jetson_small",
        name="Jetson Small",
        target="Jetson Orin Nano",
        chat_model="neural-chat",
        embed_model="nomic-embed-text",
        description="Lightweight Jetson model that works better on low-memory hardware.",
    ),
    ModelPreset(
        id="windows_balanced",
        name="Windows Balanced",
        target="Windows Desktop",
        chat_model="llama3.2:3b",
        embed_model="nomic-embed-text",
        description="Balanced local desktop preset with a stronger 3B chat model.",
    ),
    ModelPreset(
        id="windows_light",
        name="Windows Light",
        target="Windows Desktop",
        chat_model="phi3.5",
        embed_model="nomic-embed-text",
        description="Compact desktop preset with strong lightweight reasoning and the same embedding model.",
    ),
]


def list_model_presets() -> list[dict]:
    return [asdict(preset) for preset in PRESETS]


def get_model_preset(preset_id: str) -> ModelPreset | None:
    for preset in PRESETS:
        if preset.id == preset_id:
            return preset
    return None


def recommended_preset_id() -> str:
    if platform.system().lower() == "windows":
        return "windows_balanced"
    return "jetson_small"
