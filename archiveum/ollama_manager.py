from __future__ import annotations

import shutil
import subprocess
import threading
import time
import re

from archiveum.config import persist_settings
from archiveum.model_presets import ModelPreset


class OllamaManager:
    def __init__(self, assistant) -> None:
        self.assistant = assistant
        self._lock = threading.Lock()

    def _strip_ansi_codes(self, text: str) -> str:
        """Strip ANSI escape codes from text."""
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    def cli_path(self) -> str:
        return shutil.which("ollama") or ""

    def list_models(self) -> list[dict]:
        """List all models installed in Ollama."""
        cli = self.cli_path()
        if not cli:
            return []
        try:
            result = subprocess.run(
                [cli, "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode != 0:
                print(f"Ollama list failed: {result.stderr}")
                return []
            # Parse the output. Ollama list outputs space-separated values.
            lines = result.stdout.strip().split('\n')
            if not lines:
                return []
            # Skip header
            models = []
            for line in lines[1:]:
                parts = line.split()  # Split on whitespace
                if len(parts) >= 4:  # NAME ID SIZE MODIFIED
                    name = parts[0]
                    size = f"{parts[2]} {parts[3]}"  # e.g., "5.0 GB"
                    models.append({"name": name, "size": size})
            return models
        except Exception as e:
            print(f"Exception in list_models: {e}")
            return []

    def pull_model(self, model_name: str) -> tuple[bool, str]:
        """Pull a specific model asynchronously."""
        cli = self.cli_path()
        if not cli:
            return False, "Ollama CLI not found."

        # Sanitize model name: replace spaces with slashes for proper format
        sanitized_name = model_name.strip().replace(" ", "/")
        if not sanitized_name:
            return False, "Model name cannot be empty."

        state = self.assistant.runtime_status.read().get("model_install", {})
        if state.get("active"):
            return False, "A model install is already running."

        # Set initial status before starting thread
        self.assistant.runtime_status.set_model_install_state(
            {
                "active": True,
                "stage": f"Pulling {sanitized_name}",
                "preset_id": "",
                "chat_model": "",
                "embed_model": "",
                "last_message": f"Running `ollama pull {sanitized_name}`",
                "last_error": "",
            }
        )

        thread = threading.Thread(target=self._run_pull, args=(sanitized_name,), daemon=True)
        thread.start()
        return True, f"Started pulling model '{sanitized_name}'."

    def _run_pull(self, model_name: str) -> None:
        cli = self.cli_path()
        # Status already set in pull_model

        process = subprocess.Popen(
            [cli, "pull", model_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        last_progress = ""
        while True:
            line = process.stderr.readline()
            if not line:
                break
            line = line.strip()
            if "pulling" in line and "%" in line:
                # Parse progress like "pulling 1de498fe2691: 24% ▕████ ▏ 1.2 GB/5.0 GB 16 MB/s 3m45s"
                progress_match = re.search(r'(\d+)%', line)
                if progress_match:
                    percent = int(progress_match.group(1))
                    self.assistant.runtime_status.set_model_install_state({
                        "active": True,
                        "stage": f"Pulling {model_name} ({percent}%)",
                        "preset_id": "",
                        "chat_model": "",
                        "embed_model": "",
                        "last_message": f"Downloading model... {percent}% complete",
                        "last_error": "",
                    })
                    last_progress = f"{percent}%"

        process.wait()
        if process.returncode != 0:
            error_text = self._strip_ansi_codes(process.stderr.read() or f"Failed to pull {model_name}")
            self.assistant.runtime_status.set_model_install_state(
                {
                    "active": False,
                    "stage": "Pull failed",
                    "last_error": error_text,
                    "last_message": "",
                    "last_completed": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            return

        self.assistant.runtime_status.set_model_install_state(
            {
                "active": False,
                "stage": "Pull complete",
                "last_error": "",
                "last_message": f"Successfully pulled model '{model_name}'.",
                "last_completed": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    def install_preset_async(self, preset: ModelPreset) -> tuple[bool, str]:
        state = self.assistant.runtime_status.read().get("model_install", {})
        if state.get("active"):
            return False, "A model install is already running."

        self.assistant.apply_model_selection(preset.chat_model, preset.embed_model)
        thread = threading.Thread(target=self._run_install, args=(preset,), daemon=True)
        thread.start()
        return True, f"Started installing preset '{preset.name}'."

    def apply_preset(self, preset: ModelPreset) -> str:
        self.assistant.apply_model_selection(preset.chat_model, preset.embed_model)
        self.assistant.runtime_status.mark_setup_step(
            "preset_selected",
            completed=True,
            detail=f"Applied preset '{preset.name}'.",
        )
        self.assistant.runtime_status.set_model_install_state(
            {
                "active": False,
                "stage": "Preset applied",
                "preset_id": preset.id,
                "chat_model": preset.chat_model,
                "embed_model": preset.embed_model,
                "last_message": f"Applied preset '{preset.name}'.",
                "last_error": "",
                "last_completed": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        return f"Applied preset '{preset.name}'."

    def _run_install(self, preset: ModelPreset) -> None:
        cli = self.cli_path()
        self.assistant.runtime_status.set_model_install_state(
            {
                "active": True,
                "stage": "Preparing install",
                "preset_id": preset.id,
                "chat_model": preset.chat_model,
                "embed_model": preset.embed_model,
                "last_message": f"Preparing preset '{preset.name}'.",
                "last_error": "",
            }
        )

        if not cli:
            self.assistant.runtime_status.mark_setup_step(
                "models_installed",
                completed=False,
                detail="The ollama CLI was not found on PATH.",
            )
            self.assistant.runtime_status.set_model_install_state(
                {
                    "active": False,
                    "stage": "Install failed",
                    "last_error": "The ollama CLI was not found on PATH.",
                    "last_message": "",
                    "last_completed": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            return

        for role, model in (("chat", preset.chat_model), ("embed", preset.embed_model)):
            self.assistant.runtime_status.set_model_install_state(
                {
                    "active": True,
                    "stage": f"Pulling {role} model",
                    "last_message": f"Running `ollama pull {model}`",
                    "last_error": "",
                }
            )
            result = subprocess.run(
                [cli, "pull", model],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                error_text = (result.stderr or result.stdout or "Unknown ollama pull error").strip()
                error_text = self._strip_ansi_codes(error_text)
                self.assistant.runtime_status.mark_setup_step(
                    "models_installed",
                    completed=False,
                    detail=error_text,
                )
                self.assistant.runtime_status.set_model_install_state(
                    {
                        "active": False,
                        "stage": "Install failed",
                        "last_error": error_text,
                        "last_message": "",
                        "last_completed": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                return

        persist_settings(
            self.assistant.paths,
            {
                "ollama_chat_model": preset.chat_model,
                "ollama_embed_model": preset.embed_model,
            },
        )
        self.assistant.reload_settings()
        self.assistant.runtime_status.mark_setup_step(
            "models_installed",
            completed=True,
            detail=f"Installed preset '{preset.name}'.",
        )
        self.assistant.runtime_status.set_model_install_state(
            {
                "active": False,
                "stage": "Install complete",
                "preset_id": preset.id,
                "chat_model": preset.chat_model,
                "embed_model": preset.embed_model,
                "last_error": "",
                "last_message": f"Installed preset '{preset.name}'.",
                "last_completed": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
