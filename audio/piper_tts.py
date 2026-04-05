import subprocess
import tempfile
import os


class PiperTTS:
    def __init__(self, model_path: str, config_path: str | None = None):
        self.model_path = model_path
        self.config_path = config_path
        self.is_speaking = False

        print("✅ Piper TTS initialized.")
        print(f"   -> Model: {self.model_path}")

    def speak(self, text: str):
        if not text.strip():
            return

        self.is_speaking = True

        try:
            # Temporary WAV output
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                wav_path = f.name

            # Build Piper command
            cmd = [
                "piper",
                "--model", self.model_path,
                "--output_file", wav_path
            ]

            if self.config_path:
                cmd += ["--config", self.config_path]

            # Run Piper
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True
            )

            process.stdin.write(text)
            process.stdin.close()
            process.wait()

            # Play audio safely
            subprocess.run([
                "aplay",
                "-q",
                wav_path
            ])

        except Exception as e:
            print("❌ Piper TTS error:", e)

        finally:
            self.is_speaking = False
            try:
                os.remove(wav_path)
            except Exception:
                pass
