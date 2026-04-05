import os
import subprocess
import tempfile
import threading
import time


class PiperTTS:
    """
    Stable STDIN-based Piper wrapper for Archiveum voice playback.

    Design guarantees:
      - Uses STDIN for Jetson Piper builds
      - ALSA-safe playback via plughw
      - Clean temp-file handling
      - Safe repeated stop calls
    """

    TMP_MAX_AGE_SECONDS = 3600

    def __init__(
        self,
        model_path: str,
        sample_rate: int = 22050,
        device: str = "plughw:0,0",
        tmp_dir: str = "/tmp",
    ):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.device = device
        self.tmp_dir = tmp_dir

        self._play_proc: subprocess.Popen | None = None
        self._current_wav_file: str | None = None
        self._lock = threading.Lock()

        self._cleanup_old_temp_files()

    def _cleanup_old_temp_files(self):
        now = time.time()

        try:
            for name in os.listdir(self.tmp_dir):
                if not name.startswith("tmp") or not name.endswith(".wav"):
                    continue

                path = os.path.join(self.tmp_dir, name)
                try:
                    if not os.path.isfile(path):
                        continue

                    age = now - os.path.getmtime(path)
                    if age > self.TMP_MAX_AGE_SECONDS:
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass

    def _cleanup_locked(self):
        if self._current_wav_file and os.path.exists(self._current_wav_file):
            try:
                os.remove(self._current_wav_file)
            except OSError:
                pass

        self._current_wav_file = None

    def speak(self, text: str):
        if not text:
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav", dir=self.tmp_dir) as f_wav:
            wav_file = f_wav.name

        try:
            proc = subprocess.Popen(
                [
                    "piper",
                    "--model",
                    self.model_path,
                    "--output_file",
                    wav_file,
                    "--sample_rate",
                    str(self.sample_rate),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            proc.stdin.write(text)
            proc.stdin.close()
            proc.wait()
        except Exception as exc:
            print(f"[Piper] Synthesis error: {exc}")
            try:
                os.remove(wav_file)
            except Exception:
                pass
            return

        cmd_play = ["aplay", "-D", self.device, wav_file]
        print("-> Playing audio with:", " ".join(cmd_play))

        with self._lock:
            self._current_wav_file = wav_file
            try:
                self._play_proc = subprocess.Popen(
                    cmd_play,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                play_proc = self._play_proc
            except Exception as exc:
                print(f"[Piper] Playback error: {exc}")
                self._cleanup_locked()
                return

        try:
            play_proc.wait()
        except Exception:
            pass

        with self._lock:
            if self._play_proc is play_proc:
                self._play_proc = None
                self._cleanup_locked()

    def stop(self):
        with self._lock:
            proc = self._play_proc

            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass

            self._play_proc = None
            self._cleanup_locked()
