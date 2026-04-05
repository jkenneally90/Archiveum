import subprocess
import tempfile
import os
import sounddevice as sd
import soundfile as sf


class PiperTTS:
    def __init__(self, model_path: str):
        self.model_path = model_path

    def speak(self, text: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        cmd = [
            "piper",
            "--model", self.model_path,
            "--output_file", tmp_path,
            "--text", text,
        ]

        subprocess.run(cmd, check=True)

        data, samplerate = sf.read(tmp_path)
        sd.play(data, samplerate)
        sd.wait()

        os.remove(tmp_path)
