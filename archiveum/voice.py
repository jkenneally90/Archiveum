from __future__ import annotations

import queue
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
import importlib
from pathlib import Path

import numpy as np
import sounddevice as sd
from archiveum.assistant import ArchiveumAssistant
from archiveum.config import AppSettings
from archiveum.diagnostics import collect_runtime_diagnostics
from archiveum.personas import get_persona
from archiveum.speech_text import to_spoken_text
from tts_piper import PiperTTS

# Import conversation pipeline (lazy import to avoid circular dependency)
_submit_conversation_turn = None
_process_voice_turn = None

def _get_conversation_turn_fn():
    global _submit_conversation_turn
    if _submit_conversation_turn is None:
        from archiveum.webapp import submit_conversation_turn
        _submit_conversation_turn = submit_conversation_turn
    return _submit_conversation_turn


def _get_process_voice_turn_fn():
    global _process_voice_turn
    if _process_voice_turn is None:
        from archiveum.webapp import process_voice_conversation_turn
        _process_voice_turn = process_voice_conversation_turn
    return _process_voice_turn

try:
    import webrtcvad
except Exception:
    webrtcvad = None

try:
    from audio.stt import FasterWhisperSTT
except Exception:
    FasterWhisperSTT = None


VOICE_ACTIVATE_COMMANDS = {"voice activated"}
VOICE_DEACTIVATE_COMMANDS = {"voice deactivated"}
VOICE_SHUTDOWN_COMMANDS = {
    "system shutdown",
    "system shut down",
    "shutdown system",
    "shut down system",
}
VOICE_CONFIRM_YES = {"yes", "yes please", "confirm", "confirmed"}
VOICE_CONFIRM_NO = {"no", "no thanks", "cancel", "stop"}


@dataclass(frozen=True)
class VoiceConfig:
    sample_rate: int = 16000
    channels: int = 1
    frame_duration_ms: int = 30
    max_record_seconds: float = 12.0
    silence_timeout_seconds: float = 2.0
    post_speech_delay_seconds: float = 2.5
    stt_model: str = "tiny.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "float32"
    piper_model_path: str = "/home/george/Archiveum/piper-voices/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx"
    piper_device: str = "plughw:0,0"

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "VoiceConfig":
        return cls(
            sample_rate=settings.voice_sample_rate,
            channels=settings.voice_channels,
            frame_duration_ms=settings.voice_frame_duration_ms,
            max_record_seconds=settings.voice_max_record_seconds,
            silence_timeout_seconds=settings.voice_silence_timeout_seconds,
            post_speech_delay_seconds=settings.voice_post_speech_delay_seconds,
            stt_model=settings.stt_model,
            stt_device=settings.stt_device,
            stt_compute_type=settings.stt_compute_type,
            piper_model_path=settings.piper_model_path,
            piper_device=settings.piper_device,
        )


class ArchiveumVoiceAssistant:
    def __init__(self, assistant: ArchiveumAssistant, config: VoiceConfig | None = None) -> None:
        self.assistant = assistant
        self.config = config or VoiceConfig.from_settings(assistant.settings)
        self._frame_bytes = int(self.config.sample_rate * self.config.frame_duration_ms / 1000) * 2
        self._lock = threading.RLock()
        self._running = False
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._command_listener_running = False
        self._command_listener_thread: threading.Thread | None = None
        self._command_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=120)
        self._voice_commands_enabled = bool(self.assistant.settings.enable_voice)
        self._active_public_session_id = ""
        self._vad = None
        self._stt = None
        print(f"[Voice __init__] Creating TTS with piper_command: {self.assistant.settings.piper_command}")
        self._tts = PiperTTS(
            command=self.assistant.settings.piper_command,
            model_path=self._active_piper_model_path(),
            device=self.config.piper_device,
        )
        self._last_transcript = ""
        self._last_response = ""
        self._last_error = ""
        self._pending_confirmation: str | None = None
        self._status_message = "Voice mode is stopped."
        self._tts_is_speaking = False

    def _active_persona_id(self) -> str:
        if self.assistant.settings.public_mode and self._active_public_session_id:
            return self.assistant.settings.public_mode_persona_id or "nova"
        return self.assistant.settings.current_persona_id or "nova"

    def _active_piper_model_path(self) -> str:
        persona = get_persona(self._active_persona_id())
        if persona and persona.voice_model:
            return persona.voice_model
        return self.config.piper_model_path

    def refresh_settings(self) -> None:
        self.config = VoiceConfig.from_settings(self.assistant.settings)
        self._frame_bytes = int(self.config.sample_rate * self.config.frame_duration_ms / 1000) * 2
        self._voice_commands_enabled = self._voice_commands_enabled or bool(self.assistant.settings.enable_voice)
        print(f"[Voice refresh_settings] Creating TTS with piper_command: {self.assistant.settings.piper_command}")
        self._tts = PiperTTS(
            command=self.assistant.settings.piper_command,
            model_path=self._active_piper_model_path(),
            device=self.config.piper_device,
        )
        if self.assistant.settings.enable_voice and not self._running:
            self.ensure_command_listener()

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._running:
                return True, "Voice mode already running."

            diagnostics = collect_runtime_diagnostics(self.assistant.settings)
            if not diagnostics["voice_ready"]:
                self._status_message = self._voice_block_reason(diagnostics)
                return False, self._voice_block_reason(diagnostics)
            self._status_message = "Loading the speech model. This can take a few moments the first time."
            initialized, detail = self._ensure_runtime()
            if not initialized:
                self._status_message = detail
                return False, detail

            self._voice_commands_enabled = True
            self._stop_command_listener()
            self._drain_audio_queue(self._queue)
            self._running = True
            self._last_error = ""
            self._status_message = "Voice mode started. Listening for speech."
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()
            return True, "Voice mode started."

    def stop(self) -> None:
        self._request_stop(allow_voice_reactivation=True, reason="Voice mode stopped.")

    def shutdown(self) -> None:
        with self._lock:
            self._voice_commands_enabled = False
            self._running = False
            self._status_message = "Voice mode is stopped."
        self._tts.stop()
        self._stop_command_listener()

    def stop_speaking(self) -> None:
        self._tts.stop()
        self._tts_is_speaking = False
        if self._running:
            self._status_message = "Speech interrupted. Listening for the next question."
        else:
            self._status_message = "Speech interrupted."

    def status_snapshot(self) -> dict:
        tts_speaking = bool(self._tts_is_speaking or (self._tts and self._tts.is_speaking))
        return {
            "running": self._running,
            "command_listener_running": self._command_listener_running,
            "status_message": self._status_message,
            "last_transcript": self._last_transcript,
            "last_response": self._last_response,
            "last_error": self._last_error,
            "pending_confirmation": self._pending_confirmation or "",
            "tts_speaking": tts_speaking,
            "active_public_session_id": self._active_public_session_id,
        }

    def bind_public_session(self, session_id: str | None) -> None:
        with self._lock:
            self._active_public_session_id = (session_id or "").strip()

    def ensure_command_listener(self) -> tuple[bool, str]:
        with self._lock:
            if self._running:
                return True, "Voice mode already active."
            if self._command_listener_running:
                return True, "Voice activation listener already running."

            diagnostics = collect_runtime_diagnostics(self.assistant.settings)
            if not diagnostics["voice_ready"]:
                self._status_message = self._voice_block_reason(diagnostics)
                return False, self._voice_block_reason(diagnostics)

            initialized, detail = self._ensure_runtime()
            if not initialized:
                self._status_message = detail
                return False, detail

            self._voice_commands_enabled = True
            self._drain_audio_queue(self._command_queue)
            self._command_listener_running = True
            self._status_message = "Voice mode is stopped. Say 'Voice activated' to start."
            self._command_listener_thread = threading.Thread(target=self._command_listener_loop, daemon=True)
            self._command_listener_thread.start()
            return True, "Voice activation listener started."

    def _loop(self) -> None:
        print("[Voice] Archiveum voice mode ready.")
        print("[Voice] Speak naturally. Pause after each question.")
        self._status_message = "Voice mode is active and listening."
        try:
            with sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                callback=self._audio_callback,
                blocksize=int(self.config.sample_rate * self.config.frame_duration_ms / 1000),
            ):
                while self._running:
                    utterance = self._capture_utterance(self._queue)
                    if not utterance:
                        continue

                    transcript = self._transcribe(utterance)
                    if not transcript:
                        self._status_message = "Listening for speech."
                        continue

                    self._last_transcript = transcript
                    if self._handle_pending_confirmation(transcript):
                        if not self._running:
                            break
                        continue
                    command = _voice_command(transcript)
                    if command == "deactivate":
                        self._last_response = "Voice deactivated."
                        self._status_message = "Voice mode stopped. Say 'Voice activated' to start again."
                        self._request_stop(
                            allow_voice_reactivation=True,
                            reason="Voice mode stopped. Say 'Voice activated' to start again.",
                        )
                        break
                    if command == "shutdown":
                        self._pending_confirmation = "shutdown"
                        self._respond_without_assistant("Are you sure you want me to shut down?")
                        continue

                    self._status_message = "Heard speech. Thinking about a reply."
                    print(f"[Voice] Heard: {transcript}")
                    try:
                        process_voice_turn = _get_process_voice_turn_fn()
                        result = process_voice_turn(
                            transcript,
                            session_id=self._active_public_session_id,
                        )

                        if result.get("error"):
                            raise Exception(result["error"])

                        self._last_response = result["answer"]
                        self._last_error = ""
                        answer = result["answer"]

                        print(f"[Voice Debug] speak_responses={self.assistant.settings.speak_responses}, _tts={self._tts is not None}")
                        if self.assistant.settings.speak_responses:
                            self._status_message = "Speaking the latest reply."
                        else:
                            self._status_message = "Reply ready in text-only mode."
                        print(f"[Archiveum] {answer}")

                        if self.assistant.settings.speak_responses:
                            self._tts_is_speaking = True
                            try:
                                spoken_text = to_spoken_text(answer)
                                print(f"[Voice Debug] Calling _tts.speak() with text length: {len(spoken_text)}")
                                self._tts.speak(spoken_text)
                                print("[Voice Debug] _tts.speak() completed")
                            except Exception as speak_exc:
                                print(f"[Voice Debug] ERROR in _tts.speak(): {speak_exc}")
                                import traceback
                                traceback.print_exc()
                            finally:
                                self._tts_is_speaking = False
                            self._finish_speaking_turn()
                            self._status_message = "Listening for the next question."
                        else:
                            self._status_message = "Listening for the next question."
                    except Exception as exc:
                        error_text = f"I hit a problem while searching the archive: {exc}"
                        self._last_error = error_text
                        self._status_message = "Voice search hit a problem."
                        print(f"[Voice] {error_text}")
                        if self.assistant.settings.speak_responses:
                            self._tts_is_speaking = True
                            try:
                                self._tts.speak(to_spoken_text(error_text))
                            finally:
                                self._tts_is_speaking = False
                            self._finish_speaking_turn()
        finally:
            with self._lock:
                self._running = False
                self._worker = None
            self._drain_audio_queue(self._queue)
            if self._voice_commands_enabled:
                self.ensure_command_listener()

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if not self._running:
            return
        # Mute microphone while TTS is speaking to prevent self-hearing
        if self._tts_is_speaking or (self._tts and self._tts.is_speaking):
            return
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            pass

    def _command_audio_callback(self, indata, frames, time_info, status) -> None:
        if not self._command_listener_running or self._running:
            return
        # Mute microphone while TTS is speaking to prevent self-hearing
        if self._tts_is_speaking or (self._tts and self._tts.is_speaking):
            return
        try:
            self._command_queue.put_nowait(indata.copy())
        except queue.Full:
            pass

    def _command_listener_loop(self) -> None:
        activate_requested = False
        try:
            with sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                callback=self._command_audio_callback,
                blocksize=int(self.config.sample_rate * self.config.frame_duration_ms / 1000),
            ):
                while self._command_listener_running and not self._running:
                    utterance = self._capture_utterance(self._command_queue, keep_running=lambda: self._command_listener_running and not self._running)
                    if not utterance:
                        continue
                    transcript = self._transcribe(utterance)
                    if not transcript:
                        continue
                    command = _voice_command(transcript)
                    if command != "activate":
                        continue
                    self._last_transcript = transcript
                    self._last_response = "Voice activated."
                    self._status_message = "Voice activation command heard. Starting voice mode."
                    activate_requested = True
                    break
        finally:
            with self._lock:
                self._command_listener_running = False
                self._command_listener_thread = None
            self._drain_audio_queue(self._command_queue)

        if activate_requested:
            started, detail = self.start()
            if not started:
                self._last_error = detail

    def _capture_utterance(self, audio_queue: queue.Queue[np.ndarray], keep_running=None) -> bytes:
        pcm_chunks: list[bytes] = []
        heard_speech = False
        last_speech_time = time.time()
        started_at = time.time()
        keep_running = keep_running or (lambda: self._running)

        while keep_running():
            try:
                frame = audio_queue.get(timeout=0.5)
            except queue.Empty:
                if heard_speech and time.time() - last_speech_time > self.config.silence_timeout_seconds:
                    break
                continue

            pcm = frame.astype(np.int16).tobytes()
            if len(pcm) != self._frame_bytes:
                continue

            is_speech = self._is_speech(pcm)
            level = _pcm_rms_level(frame)
            if (is_speech and level > 150) or (self._vad is None and level > 250):
                heard_speech = True
                last_speech_time = time.time()

            if heard_speech:
                pcm_chunks.append(pcm)
                if time.time() - last_speech_time > self.config.silence_timeout_seconds:
                    break

            if time.time() - started_at > self.config.max_record_seconds:
                break

        return b"".join(pcm_chunks)

    def _transcribe(self, utterance: bytes) -> str:
        if self._stt is None:
            return ""
        try:
            return self._stt.run_stt(utterance, sample_rate=self.config.sample_rate).strip()
        except Exception as exc:
            self._last_error = str(exc)
            return ""

    def _finish_speaking_turn(self) -> None:
        self._status_message = "Finishing the reply before listening again."
        if self.config.post_speech_delay_seconds > 0:
            time.sleep(self.config.post_speech_delay_seconds)
        self._drain_audio_queue(self._queue)

    def _drain_audio_queue(self, audio_queue: queue.Queue[np.ndarray]) -> None:
        while True:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break

    def _request_stop(self, *, allow_voice_reactivation: bool, reason: str) -> None:
        with self._lock:
            self._voice_commands_enabled = self._voice_commands_enabled or allow_voice_reactivation
            self._running = False
            self._status_message = reason
        self._tts.stop()

    def _handle_pending_confirmation(self, transcript: str) -> bool:
        if self._pending_confirmation != "shutdown":
            return False

        normalized = _normalize_spoken_command(transcript)
        if normalized in VOICE_CONFIRM_YES:
            self._pending_confirmation = None
            confirmation = "Shutting down the system now."
            self._last_response = confirmation
            self._status_message = "Shutdown confirmed. Powering off the system."
            if self.assistant.settings.speak_responses:
                self._tts_is_speaking = True
                try:
                    self._tts.speak(to_spoken_text(confirmation))
                finally:
                    self._tts_is_speaking = False
            self._issue_system_shutdown()
            return True

        if normalized in VOICE_CONFIRM_NO:
            self._pending_confirmation = None
            self._respond_without_assistant("Shutdown cancelled.")
            return True

        self._respond_without_assistant("Please say yes or no.")
        return True

    def _respond_without_assistant(self, text: str) -> None:
        self._last_response = text
        self._last_error = ""
        if self.assistant.settings.speak_responses:
            self._status_message = "Speaking the latest reply."
            self._tts_is_speaking = True
            try:
                self._tts.speak(to_spoken_text(text))
            finally:
                self._tts_is_speaking = False
            self._finish_speaking_turn()
            if self._pending_confirmation:
                self._status_message = "Waiting for confirmation."
            else:
                self._status_message = "Listening for the next question."
        else:
            if self._pending_confirmation:
                self._status_message = "Waiting for confirmation."
            else:
                self._status_message = "Listening for the next question."

    def _issue_system_shutdown(self) -> None:
        command = _system_shutdown_command()
        self._voice_commands_enabled = False
        self._stop_command_listener()
        self._running = False
        self._drain_audio_queue(self._queue)
        self._drain_audio_queue(self._command_queue)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception as exc:
            self._last_error = f"System shutdown failed: {exc}"
            self._status_message = "System shutdown failed."
            self._running = True
            return

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            if not detail:
                detail = f"exit code {completed.returncode}"
            self._last_error = f"System shutdown failed: {detail}"
            self._status_message = "System shutdown failed."
            self._running = True
            self._voice_commands_enabled = True
            return

        self._status_message = "System shutdown requested."

    def _is_speech(self, pcm: bytes) -> bool:
        if self._vad is None:
            return False
        try:
            return self._vad.is_speech(pcm, self.config.sample_rate)
        except Exception:
            return False

    def _voice_block_reason(self, diagnostics: dict) -> str:
        issues: list[str] = []
        if not diagnostics["piper"]["ok"]:
            issues.append(diagnostics["piper"]["detail"])
        if not diagnostics["audio"]["ok"]:
            issues.append(diagnostics["audio"]["detail"])
        if not diagnostics["voice_python"]["ok"]:
            issues.append(diagnostics["voice_python"]["detail"])
        if not diagnostics["stt_model"]["ok"]:
            issues.append(diagnostics["stt_model"]["detail"])
        return "Voice mode unavailable: " + "; ".join(issues)

    def _ensure_runtime(self) -> tuple[bool, str]:
        vad_module = _load_webrtcvad()
        if FasterWhisperSTT is None:
            return False, "Voice mode unavailable: missing voice Python dependencies."
        stt_model_path = Path(self.config.stt_model)
        if not stt_model_path.exists():
            return False, f"Voice mode unavailable: local STT model missing at {stt_model_path}"

        if self._vad is None and vad_module is not None:
            self._vad = vad_module.Vad(2)

        if self._stt is None:
            try:
                self._stt = FasterWhisperSTT(
                    model_size_or_path=self.config.stt_model,
                    device=self.config.stt_device,
                    compute_type=self.config.stt_compute_type,
                    language="en",
                )
            except Exception as exc:
                self._last_error = str(exc)
                return False, f"Voice mode unavailable: could not load the speech model. {exc}"

        if vad_module is None:
            self._status_message = "Voice mode started with simplified speech detection."
        return True, "Voice runtime ready."

    def _stop_command_listener(self) -> None:
        thread = None
        with self._lock:
            if not self._command_listener_running:
                return
            self._command_listener_running = False
            thread = self._command_listener_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)


def _pcm_rms_level(frame: np.ndarray) -> float:
    audio = frame.astype(np.float32)
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio))))


def _load_webrtcvad():
    global webrtcvad
    if webrtcvad is not None:
        return webrtcvad
    try:
        webrtcvad = importlib.import_module("webrtcvad")
    except Exception:
        webrtcvad = None
    return webrtcvad


def _normalize_spoken_command(text: str) -> str:
    cleaned = " ".join((text or "").strip().lower().replace("-", " ").split())
    return cleaned


def _voice_command(text: str) -> str | None:
    normalized = _normalize_spoken_command(text)
    if normalized in VOICE_ACTIVATE_COMMANDS:
        return "activate"
    if normalized in VOICE_DEACTIVATE_COMMANDS:
        return "deactivate"
    if normalized in VOICE_SHUTDOWN_COMMANDS:
        return "shutdown"
    return None


def _system_shutdown_command() -> list[str]:
    system_name = platform.system().lower()
    if system_name == "windows":
        return ["shutdown", "/s", "/t", "0"]
    return ["systemctl", "poweroff"]
