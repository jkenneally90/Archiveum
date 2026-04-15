import os
import platform
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path


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
        device: str = "",
        tmp_dir: str | None = None,
        command: str = "piper",
    ):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.platform_name = platform.system().lower()
        self.device = device or self._default_device()
        self.tmp_dir = tmp_dir or tempfile.gettempdir()
        self.command = self._resolve_piper_command(command)

        self._play_proc: subprocess.Popen | None = None
        self._synth_proc: subprocess.Popen | None = None
        self._current_wav_file: str | None = None
        self._lock = threading.Lock()
        self._is_speaking = False
        self._stop_event = threading.Event()

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

    def _resolve_piper_command(self, command: str) -> str:
        """Auto-detect Piper executable in common locations if not found on PATH."""
        # If command is already a full path that exists, use it
        if Path(command).exists():
            return command

        # If it's just 'piper' or 'piper.exe', try to find it
        if command in ("piper", "piper.exe"):
            # Try to find piper on PATH first
            try:
                result = subprocess.run(
                    ["where", "piper.exe"] if self.platform_name == "windows" else ["which", "piper"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    found = result.stdout.strip().split("\n")[0].strip()
                    if found:
                        print(f"[Piper] Found on PATH: {found}")
                        return found
            except Exception:
                pass

            # Search common locations on Windows
            if self.platform_name == "windows":
                common_paths = [
                    Path.home() / "AppData" / "Local" / "Programs" / "Piper" / "piper" / "piper.exe",
                    Path.home() / "AppData" / "Local" / "Piper" / "piper.exe",
                    Path("C:/Program Files/Piper/piper.exe"),
                    Path("C:/Program Files (x86)/Piper/piper.exe"),
                ]
                # Also check if running from project directory with tools/piper
                project_dir = Path.cwd()
                if (project_dir / "tools" / "piper" / "piper.exe").exists():
                    common_paths.insert(0, project_dir / "tools" / "piper" / "piper.exe")
                if (project_dir / "tools" / "piper.exe").exists():
                    common_paths.insert(0, project_dir / "tools" / "piper.exe")

                for path in common_paths:
                    if path.exists():
                        print(f"[Piper] Auto-detected at: {path}")
                        return str(path)

        return command

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def speak(self, text: str):
        if not text:
            return

        self._stop_event.clear()
        self._is_speaking = True
        try:
            self._do_speak(text)
        finally:
            self._is_speaking = False
    
    def _do_speak(self, text: str):
        print(f"[Piper Debug] _do_speak called with command: '{self.command}'")
        print(f"[Piper Debug] Model path: '{self.model_path}'")
        print(f"[Piper Debug] Model exists: {Path(self.model_path).exists()}")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav", dir=self.tmp_dir) as f_wav:
            wav_file = f_wav.name

        try:
            proc = subprocess.Popen(
                [
                    self.command,
                    "--model",
                    self.model_path,
                    "--output_file",
                    wav_file,
                    "--sample_rate",
                    str(self.sample_rate),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._lock:
                self._synth_proc = proc
            # Wait briefly to see if Piper crashes immediately
            import time
            time.sleep(0.1)
            if proc.poll() is not None:
                # Piper exited immediately
                stdout, stderr = proc.communicate()
                print(f"[Piper] ERROR: Piper crashed on startup!")
                print(f"[Piper] ERROR: stdout: {stdout}")
                print(f"[Piper] ERROR: stderr: {stderr}")
                print(f"[Piper] ERROR: returncode: {proc.returncode}")
                raise RuntimeError(f"Piper crashed on startup: {stderr or stdout or 'unknown'}")
            try:
                proc.stdin.write(text)
                proc.stdin.close()
            except ValueError as e:
                # stdin was closed - Piper probably crashed
                stdout, stderr = proc.communicate(timeout=5)
                print(f"[Piper] ERROR: Piper crashed after write. stderr: {stderr}")
                print(f"[Piper] ERROR: Model path: {self.model_path}")
                raise RuntimeError(f"Piper crashed: {stderr or 'unknown error'}")
            stdout, stderr = proc.communicate(timeout=30)
            if proc.returncode != 0:
                raise RuntimeError(f"Piper exited with code {proc.returncode}: {stderr or stdout}")
        except FileNotFoundError as exc:
            print(f"[Piper] ERROR: Piper executable not found: '{self.command}'")
            print(f"[Piper] Please install Piper or update piper_command in archiveum_settings.json")
            print(f"[Piper] Download: https://github.com/rhasspy/piper/releases")
            try:
                os.remove(wav_file)
            except Exception:
                pass
            return
        except Exception as exc:
            print(f"[Piper] Synthesis error: {exc}")
            try:
                os.remove(wav_file)
            except Exception:
                pass
            return
        finally:
            with self._lock:
                if self._synth_proc is proc if 'proc' in locals() else False:
                    self._synth_proc = None

        self._play_wav(wav_file)

    def stop(self):
        self._stop_event.set()
        if self.platform_name == "windows":
            self._stop_windows_playback()

        with self._lock:
            proc = self._play_proc
            synth_proc = self._synth_proc

            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass

            if synth_proc and synth_proc.poll() is None:
                try:
                    synth_proc.terminate()
                    try:
                        synth_proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        synth_proc.kill()
                except Exception:
                    pass

            self._play_proc = None
            self._synth_proc = None
            self._cleanup_locked()

    def _play_wav(self, wav_file: str) -> None:
        if self.platform_name == "windows":
            self._play_wav_windows(wav_file)
            return

        self._play_wav_posix(wav_file)

    def _play_wav_posix(self, wav_file: str) -> None:
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

    def _play_wav_windows(self, wav_file: str) -> None:
        try:
            import winsound

            duration_seconds = self._wav_duration_seconds(wav_file)
            with self._lock:
                self._current_wav_file = wav_file
                self._play_proc = None

            winsound.PlaySound(wav_file, winsound.SND_FILENAME | winsound.SND_ASYNC)
            threading.Thread(
                target=self._cleanup_windows_wav_after_playback,
                args=(wav_file,),
                daemon=True,
            ).start()
            # winsound playback is asynchronous on Windows, so keep this call
            # blocked until playback should be finished to avoid re-enabling STT
            # while Archiveum is still speaking.
            self._stop_event.wait(timeout=max(duration_seconds, 0.1) + 0.35)
        except Exception as exc:
            print(f"[Piper] Windows playback error: {exc}")
            with self._lock:
                self._cleanup_locked()

    def _cleanup_windows_wav_after_playback(self, wav_file: str) -> None:
        duration_seconds = self._wav_duration_seconds(wav_file)
        time.sleep(max(duration_seconds, 0.1) + 0.25)
        with self._lock:
            if self._current_wav_file == wav_file:
                self._cleanup_locked()

    def _wav_duration_seconds(self, wav_file: str) -> float:
        try:
            with wave.open(wav_file, "rb") as wav:
                frame_rate = wav.getframerate() or 1
                return wav.getnframes() / float(frame_rate)
        except Exception:
            return 0.0

    def _stop_windows_playback(self) -> None:
        try:
            import winsound

            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def _default_device(self) -> str:
        if self.platform_name == "windows":
            return "windows-default"
        return "plughw:0,0"

    def playback_backend(self) -> str:
        if self.platform_name == "windows":
            return "winsound"
        return "aplay"
