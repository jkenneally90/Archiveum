import collections
import time
import numpy as np
import sounddevice as sd
import webrtcvad


class VADListener:
    """
    Stable WebRTC VAD microphone listener with:
    - Ring buffer smoothing
    - Echo protection
    - Minimum speech length filter
    """

    def __init__(
        self,
        on_speech_callback,
        tts_ref=None,
        aggressiveness=2,
        sample_rate=16000,
        frame_duration_ms=30,
    ):
        self.vad = webrtcvad.Vad(aggressiveness)
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_duration_ms / 1000)
        self.on_speech_callback = on_speech_callback
        self.tts_ref = tts_ref  # Used for echo suppression

        self.ring_buffer = collections.deque(maxlen=20)
        self.audio_buffer = []
        self.triggered = False

    def start(self):
        print("-> VAD listening loop started (WebRTC).")

        with sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_size,
            dtype="int16",
            channels=1,
            callback=self.audio_callback,
        ):
            while True:
                time.sleep(0.01)

    def audio_callback(self, indata, frames, time_info, status):
        if self.tts_ref and self.tts_ref.is_speaking:
            return  # ✅ Echo protection: Ignore mic while TTS speaks

        frame = bytes(indata)
        is_speech = self.vad.is_speech(frame, self.sample_rate)

        if not self.triggered:
            self.ring_buffer.append(is_speech)

            voiced = sum(self.ring_buffer)
            if voiced > len(self.ring_buffer) * 0.75:
                self.triggered = True
                self.audio_buffer.clear()
                self.ring_buffer.clear()

        else:
            self.audio_buffer.append(frame)
            self.ring_buffer.append(is_speech)

            unvoiced = len(self.ring_buffer) - sum(self.ring_buffer)
            if unvoiced > len(self.ring_buffer) * 0.8:
                speech_data = b"".join(self.audio_buffer)

                # ✅ Anti-noise filter
                if len(speech_data) >= 8000:
                    print(f"-> Speech segment detected ({len(speech_data)} bytes).")
                    self.on_speech_callback(speech_data)
                else:
                    print("-> Ignoring very short noise burst.")

                self.audio_buffer.clear()
                self.ring_buffer.clear()
                self.triggered = False
