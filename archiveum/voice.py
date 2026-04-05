from __future__ import annotations

import audioop
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import webrtcvad

from audio.stt import FasterWhisperSTT
from archiveum.assistant import ArchiveumAssistant
from archiveum.config import AppSettings
from archiveum.diagnostics import collect_runtime_diagnostics
from tts_piper import PiperTTS


@dataclass(frozen=True)
class VoiceConfig:
    sample_rate: int = 16000
    channels: int = 1
    frame_duration_ms: int = 30
    max_record_seconds: float = 12.0
    silence_timeout_seconds: float = 1.0
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
        self._running = False
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._vad = webrtcvad.Vad(2)
        self._stt = FasterWhisperSTT(
            model_size_or_path=self.config.stt_model,
            device=self.config.stt_device,
            compute_type=self.config.stt_compute_type,
            language="en",
        )
        self._tts = PiperTTS(
            model_path=self.config.piper_model_path,
            device=self.config.piper_device,
        )

    def start(self) -> tuple[bool, str]:
        if self._running:
            return True, "Voice mode already running."

        diagnostics = collect_runtime_diagnostics(self.assistant.settings)
        if not diagnostics["voice_ready"]:
            return False, self._voice_block_reason(diagnostics)

        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        return True, "Voice mode started."

    def stop(self) -> None:
        self._running = False
        self._tts.stop()

    def _loop(self) -> None:
        print("[Voice] Archiveum voice mode ready.")
        print("[Voice] Speak naturally. Pause after each question.")

        with sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="int16",
            callback=self._audio_callback,
            blocksize=int(self.config.sample_rate * self.config.frame_duration_ms / 1000),
        ):
            while self._running:
                utterance = self._capture_utterance()
                if not utterance:
                    continue

                transcript = self._stt.run_stt(utterance, sample_rate=self.config.sample_rate).strip()
                if not transcript:
                    continue

                print(f"[Voice] Heard: {transcript}")
                try:
                    result = self.assistant.ask(transcript)
                    print(f"[Archiveum] {result.answer}")
                    self._tts.speak(result.answer)
                except Exception as exc:
                    error_text = f"I hit a problem while searching the archive: {exc}"
                    print(f"[Voice] {error_text}")
                    self._tts.speak(error_text)

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if not self._running:
            return
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            pass

    def _capture_utterance(self) -> bytes:
        pcm_chunks: list[bytes] = []
        heard_speech = False
        last_speech_time = time.time()
        started_at = time.time()

        while self._running:
            try:
                frame = self._queue.get(timeout=0.5)
            except queue.Empty:
                if heard_speech and time.time() - last_speech_time > self.config.silence_timeout_seconds:
                    break
                continue

            pcm = frame.astype(np.int16).tobytes()
            if len(pcm) != self._frame_bytes:
                continue

            is_speech = self._is_speech(pcm)
            level = audioop.rms(pcm, 2)
            if is_speech and level > 150:
                heard_speech = True
                last_speech_time = time.time()

            if heard_speech:
                pcm_chunks.append(pcm)
                if time.time() - last_speech_time > self.config.silence_timeout_seconds:
                    break

            if time.time() - started_at > self.config.max_record_seconds:
                break

        return b"".join(pcm_chunks)

    def _is_speech(self, pcm: bytes) -> bool:
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
        return "Voice mode unavailable: " + "; ".join(issues)
