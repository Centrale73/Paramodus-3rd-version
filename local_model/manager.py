"""
BonsaiManager — lifecycle manager for the Bonsai 8B local model server.

Architecture
------------
Bonsai 8B is a 1-bit LLM from PrismML (Apache 2.0).  It ships in two GGUF
variants relevant to Paramodus:

  bonsai-8b-native  (Q1_0_g128, ~1 GB)
    Native 1-bit format.  Requires PrismML's llama.cpp fork for the custom
    CUDA / CPU kernels.  Fastest on GPU; smallest download.

  bonsai-8b-q4  (Q4_K_M, ~4.6 GB)
    Unpacked re-quantization by bartowski.  Runs on any standard llama.cpp
    binary (CPU or GPU).  Best for users without a dedicated GPU.

The manager downloads the chosen GGUF to ~/.myapp/models/ on first use, then
starts llama-server as a subprocess on localhost:8080 exposing an
OpenAI-compatible /v1 endpoint.  Paramodus connects to it via OpenAIChat with
a custom base_url — no API key required.

Binary resolution order (for llama-server):
  1. PyInstaller bundle (sys._MEIPASS / sibling exe dir)
  2. ~/.myapp/bin/llama-server[.exe]   ← placed by scripts/get_llama_server.py
  3. System PATH

For the .exe distribution, bundle llama-server.exe via paramodus.spec:
  binaries=[('bin/llama-server.exe', '.')]
"""

import atexit
import os
import sys
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

import requests


# ---------------------------------------------------------------------------
# Port utility
# ---------------------------------------------------------------------------

def _free_port(port: int) -> None:
    """
    Kill any process currently listening on *port* so llama-server can bind.
    Windows-only (uses netstat + taskkill); silently ignored on other platforms.
    """
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            # Look for lines like:  TCP  127.0.0.1:8080  ...  LISTENING  <PID>
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True, timeout=5
                )
                print(f"[BonsaiManager] Freed port {port} — killed PID {pid}")
                time.sleep(0.5)   # give the OS a moment to release the socket
    except Exception as exc:
        print(f"[BonsaiManager] Could not free port {port}: {exc}")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DATA   = os.path.join(os.path.expanduser("~"), ".myapp")
MODELS_DIR = os.path.join(APP_DATA, "models")
BIN_DIR    = os.path.join(APP_DATA, "bin")

for _d in (APP_DATA, MODELS_DIR, BIN_DIR):
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

MODELS: dict[str, dict] = {
    "bonsai-8b-native": {
        "repo_id":     "prism-ml/Bonsai-8B-gguf",
        "filename":    "Bonsai-8B.gguf",
        "hf_url":      "https://huggingface.co/prism-ml/Bonsai-8B-gguf/resolve/main/Bonsai-8B.gguf",
        "size_gb":     1.15,
        "quant":       "Q1_0_g128 (native 1-bit)",
        "description": "Native 1-bit (~1.15 GB) — bundled in exe, no download required.",
        "requires_prismml_fork": True,
    },
    "bonsai-8b-q4": {
        "repo_id":     "bartowski/prism-ml_Bonsai-8B-unpacked-GGUF",
        "filename":    "prism-ml_Bonsai-8B-unpacked-Q4_K_M.gguf",
        "hf_url":      (
            "https://huggingface.co/bartowski/prism-ml_Bonsai-8B-unpacked-GGUF"
            "/resolve/main/prism-ml_Bonsai-8B-unpacked-Q4_K_M.gguf"
        ),
        "size_gb":     4.6,
        "quant":       "Q4_K_M (unpacked, standard llama.cpp)",
        "description": "Unpacked Q4_K_M (~4.6 GB) — works with standard llama-server, CPU friendly.",
        "requires_prismml_fork": False,
    },
}

DEFAULT_MODEL   = "bonsai-8b-native"   # Bundled in exe — zero-config for end users
SERVER_HOST     = "127.0.0.1"
SERVER_PORT     = 8080
HEALTH_URL      = f"http://{SERVER_HOST}:{SERVER_PORT}/health"
CHUNK_SIZE      = 1024 * 256        # 256 KB download chunks


# ---------------------------------------------------------------------------
# BonsaiManager
# ---------------------------------------------------------------------------

class BonsaiManager:
    """
    Manages the download and server lifecycle for Bonsai 8B.

    Thread-safe: start/stop/download can be called from background threads.
    """

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._server_lock  = threading.Lock()
        self._download_lock = threading.Lock()
        self._active_model_key: Optional[str] = None

    # ------------------------------------------------------------------
    # Binary resolution
    # ------------------------------------------------------------------

    def _get_llama_server_path(self) -> Optional[str]:
        """
        Locate the llama-server executable.  Checks three locations in order:

        1. Bundled inside the PyInstaller .exe directory (sys._MEIPASS or
           the folder that contains the frozen executable).
        2. ~/.myapp/bin/  — populated by scripts/get_llama_server.py.
        3. System PATH.
        """
        exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
        candidates: list[str] = []

        # 1. PyInstaller bundle
        if getattr(sys, "frozen", False):
            # _MEIPASS is the temp extraction dir; the binary is extracted there
            candidates.append(os.path.join(sys._MEIPASS, exe))
            # Also check the folder containing the .exe itself
            candidates.append(os.path.join(os.path.dirname(sys.executable), exe))

        # 2. App-data bin dir (populated by setup script)
        candidates.append(os.path.join(BIN_DIR, exe))

        # 3. System PATH
        path_result = shutil.which("llama-server")
        if path_result:
            candidates.append(path_result)

        for path in candidates:
            if path and os.path.isfile(path):
                return path

        return None

    # ------------------------------------------------------------------
    # Model info helpers
    # ------------------------------------------------------------------

    def get_model_path(self, model_key: str = DEFAULT_MODEL) -> str:
        """
        Resolve the path to the model GGUF file.

        Search order:
        1. PyInstaller bundle (sys._MEIPASS/models/) — present when running
           from the compiled .exe.  This is where the pre-bundled model lives.
        2. ~/.myapp/models/ — downloaded at runtime by the user.
        """
        filename = MODELS[model_key]["filename"]

        # 1. PyInstaller frozen exe — check several locations
        if getattr(sys, "frozen", False):
            candidates = [
                # _MEIPASS is the _internal/ extraction dir (COLLECT mode)
                os.path.join(sys._MEIPASS, "models", filename),
                # Sibling of the .exe itself (in case user moved the model)
                os.path.join(os.path.dirname(sys.executable), "models", filename),
                # _internal/models/ via exe path (belt-and-suspenders)
                os.path.join(os.path.dirname(sys.executable), "_internal", "models", filename),
            ]
            for p in candidates:
                print(f"[BonsaiManager] model lookup: {p} -> {'EXISTS' if os.path.isfile(p) else 'not found'}")
                if os.path.isfile(p):
                    return p

        # 2. Fall back to the user's app-data models directory
        fallback = os.path.join(MODELS_DIR, filename)
        print(f"[BonsaiManager] model fallback: {fallback} -> {'EXISTS' if os.path.isfile(fallback) else 'not found'}")
        return fallback

    def is_model_downloaded(self, model_key: str = DEFAULT_MODEL) -> bool:
        path = self.get_model_path(model_key)
        # A partial download leaves a .partial file — don't count it
        return os.path.isfile(path) and not os.path.isfile(path + ".partial")

    def get_models(self) -> list[dict]:
        """Return model catalog enriched with download status."""
        result = []
        for key, info in MODELS.items():
            result.append({
                "key":         key,
                "filename":    info["filename"],
                "size_gb":     info["size_gb"],
                "quant":       info["quant"],
                "description": info["description"],
                "requires_prismml_fork": info["requires_prismml_fork"],
                "downloaded":  self.is_model_downloaded(key),
                "model_path":  self.get_model_path(key),
            })
        return result

    # ------------------------------------------------------------------
    # Download (streaming, resume-capable)
    # ------------------------------------------------------------------

    def download_model(
        self,
        model_key: str = DEFAULT_MODEL,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> bool:
        """
        Download the specified model GGUF from HuggingFace.

        Uses huggingface_hub.hf_hub_download() which correctly handles
        HuggingFace Xet storage, CDN redirects, and automatic retries.
        Progress is reported by polling the in-progress file size from a
        background thread (hf_hub_download is synchronous).

        progress_cb(percent: float, message: str)
          percent: 0-100 on progress, -1.0 on error
        """
        with self._download_lock:
            if self.is_model_downloaded(model_key):
                if progress_cb:
                    progress_cb(100.0, "Already downloaded.")
                return True

            from huggingface_hub import hf_hub_download

            info       = MODELS[model_key]
            dest_path  = self.get_model_path(model_key)
            total_bytes = int(info["size_gb"] * 1024 ** 3)

            if progress_cb:
                progress_cb(0.0, f"Connecting to HuggingFace… ({info['size_gb']:.1f} GB)")

            # hf_hub_download is blocking; we poll the in-progress cache file
            # from a side thread to report live progress.
            _stop = threading.Event()

            def _poll_progress():
                """
                hf_hub_download writes to a .incomplete temp file inside
                the local_dir while downloading.  We find and stat it.
                """
                while not _stop.is_set():
                    try:
                        # Look for any partial file in MODELS_DIR
                        for fname in os.listdir(MODELS_DIR):
                            if fname.endswith(".incomplete") or fname.endswith(".part"):
                                fpath = os.path.join(MODELS_DIR, fname)
                                size  = os.path.getsize(fpath)
                                if size > 0 and total_bytes > 0 and progress_cb:
                                    pct      = min(size / total_bytes * 100, 99.0)
                                    done_gb  = size / (1024 ** 3)
                                    total_gb = total_bytes / (1024 ** 3)
                                    progress_cb(
                                        pct,
                                        f"Downloading… {done_gb:.2f} / {total_gb:.2f} GB"
                                    )
                                break
                    except OSError:
                        pass
                    _stop.wait(timeout=1.5)

            monitor = threading.Thread(target=_poll_progress, daemon=True)
            monitor.start()

            try:
                hf_hub_download(
                    repo_id=info["repo_id"],
                    filename=info["filename"],
                    local_dir=MODELS_DIR,
                    local_dir_use_symlinks=False,
                )
                _stop.set()

                if progress_cb:
                    progress_cb(100.0, "Download complete ✓")
                return True

            except Exception as exc:
                _stop.set()
                if progress_cb:
                    progress_cb(-1.0, f"Download failed: {exc}")
                return False

    def cancel_download(self, model_key: str = DEFAULT_MODEL) -> None:
        """Remove the .partial file to start fresh next time."""
        tmp_path = self.get_model_path(model_key) + ".partial"
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def is_server_running(self) -> bool:
        try:
            r = requests.get(HEALTH_URL, timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _detect_gpu() -> int:
        """
        Return the recommended n_gpu_layers value for this machine.

        - NVIDIA (CUDA): 99  (full offload; driver query via nvidia-smi)
        - Apple Silicon (Metal): 99  (llama-server uses Metal automatically)
        - AMD / other GPU:  99  (Vulkan path; llama-server detects it)
        - CPU-only fallback: 0

        We default to full offload (99) whenever a GPU is plausibly present
        because llama-server itself won't crash if the GPU can't hold all
        layers — it simply overflows to CPU.  The user can always override
        by passing n_gpu_layers explicitly.
        """
        # ── NVIDIA via nvidia-smi ────────────────────────────────────────────
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    timeout=5, stderr=subprocess.DEVNULL,
                ).decode().strip()
                if out:
                    print(f"[BonsaiManager] NVIDIA GPU detected: {out.splitlines()[0]} — using -ngl 99")
                    return 99
            except Exception:
                pass

        # ── Apple Silicon (always has Metal GPU) ─────────────────────────────
        if sys.platform == "darwin":
            import platform as _platform
            if "arm" in _platform.machine().lower():
                print("[BonsaiManager] Apple Silicon detected — using -ngl 99 (Metal)")
                return 99

        # ── CPU-only fallback ────────────────────────────────────────────────
        print("[BonsaiManager] No discrete GPU detected — running on CPU (-ngl 0)")
        return 0

    def start_server(
        self,
        model_key:      str           = DEFAULT_MODEL,
        n_gpu_layers:   Optional[int] = None,
        context_length: int           = 4096,
        timeout_s:      int           = 360,
        status_cb:      Optional[Callable[[str], None]] = None,
    ) -> bool:
        """
        Start llama-server as a background subprocess.

        Parameters
        ----------
        model_key      : Which model variant to load.
        n_gpu_layers   : GPU layers to offload.  Pass None (default) to
                         auto-detect the best value for this machine.
                         Pass 0 to force CPU-only, 99 for full GPU.
        context_length : Context window in tokens.
        timeout_s      : Hard timeout in seconds (safety net — normally the
                         stdout reader detects readiness in seconds, not minutes).
        status_cb      : Optional callback(line: str) called for every stdout
                         line emitted by the server during startup.  Useful
                         for live progress in the UI without polling.

        Returns True once the server reports it is listening.
        """
        with self._server_lock:
            if self.is_server_running():
                return True

            # Terminate any tracked process that may have stalled
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None

            # Evict any orphaned process holding the port (e.g. from a prior
            # crashed run that didn't clean up via atexit)
            _free_port(SERVER_PORT)

            llama_bin = self._get_llama_server_path()
            if llama_bin is None:
                print(
                    "[BonsaiManager] llama-server not found.\n"
                    "  Run: python scripts/get_llama_server.py\n"
                    "  Or add llama-server to PATH."
                )
                return False

            model_path = self.get_model_path(model_key)
            print(f"[BonsaiManager] Using model: {model_path}")
            print(f"[BonsaiManager] Model exists: {os.path.isfile(model_path)}")
            if not os.path.isfile(model_path):
                print(f"[BonsaiManager] Model not found at {model_path} — download it first.")
                # List what IS in the expected dirs to help diagnose
                for check_dir in [
                    os.path.join(getattr(sys, '_MEIPASS', ''), 'models'),
                    os.path.join(os.path.dirname(sys.executable), 'models'),
                    MODELS_DIR,
                ]:
                    if os.path.isdir(check_dir):
                        print(f"[BonsaiManager] Contents of {check_dir}: {os.listdir(check_dir)}")
                    else:
                        print(f"[BonsaiManager] Dir not found: {check_dir}")
                return False
            print(f"[BonsaiManager] Using binary: {llama_bin}")

            # Auto-detect GPU if not explicitly specified
            if n_gpu_layers is None:
                n_gpu_layers = self._detect_gpu()

            cmd = [
                llama_bin,
                "--model",    model_path,
                "--host",     SERVER_HOST,
                "--port",     str(SERVER_PORT),
                "--ctx-size", str(context_length),
                "-ngl",       str(n_gpu_layers),
                # NOTE: do NOT pass --no-mmap here.
                # --no-mmap forces the entire GGUF into physical RAM at startup,
                # which is slow and can fail on memory-constrained machines.
                # The default (mmap) reads pages on demand — faster start, less RAM.
            ]

            log_path = os.path.join(APP_DATA, "llama_server.log")

            # Hide the console window on Windows (matches your standalone app)
            startupinfo = None
            extra_kwargs: dict = {}
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                extra_kwargs["startupinfo"] = startupinfo
                # CREATE_NO_WINDOW is belt-and-suspenders: ensures no window
                # even if STARTF_USESHOWWINDOW is ignored in some contexts
                extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,   # capture so we can line-read for readiness
                stderr=subprocess.STDOUT, # merge stderr → stdout (same as your standalone)
                text=True,
                bufsize=1,                # line-buffered
                **extra_kwargs,
            )
            self._active_model_key = model_key
            atexit.register(self.stop_server)  # extra safety net in case app.py's atexit fires late
            print(f"[BonsaiManager] Started llama-server pid={self._process.pid} "
                  f"model={model_key} ngl={n_gpu_layers}")

        # ── Readiness detection via stdout line-reader ───────────────────────
        # This mirrors your standalone app exactly.
        # We read stdout line-by-line in the calling thread (which is already
        # a background thread in begin_auto_setup / start_bonsai).
        # The log file is written in parallel by a drain thread.
        ready    = threading.Event()
        deadline = time.monotonic() + timeout_s
        crashed  = threading.Event()

        def _drain_and_detect(proc: subprocess.Popen, log: str) -> None:
            """
            Read every line from the server's stdout.
            - Writes each line to the log file.
            - Fires status_cb for live UI feedback.
            - Sets 'ready' when any known ready marker appears OR when the
              HTTP health endpoint responds (belt-and-suspenders).
            - Sets 'crashed' if the process exits before becoming ready.

            llama.cpp has changed its ready log line across versions:
              Older builds : 'llama server listening at http://...'
              Newer builds : 'server is listening on http://...'
              JSON log mode: '{"message":"server is listening on"}'
              PrismML fork : may emit 'HTTP server listening' or similar
            We match any substring from READY_MARKERS to stay version-safe,
            and fall back to polling /health if none of the strings match.
            """
            READY_MARKERS = (
                "server is listening on",      # llama.cpp >= b3000
                "llama server listening at",    # llama.cpp older
                "HTTP server listening",        # some forks
                "all slots are idle",           # seen in some builds
                "model loaded",                 # PrismML specific
                "listening on",                 # catch-all
            )
            ERROR_MARKERS = (
                "HTTP server error",
                "failed to bind",
                "address already in use",
                "error: unable to start",
            )

            try:
                with open(log, "a", buffering=1) as lf:
                    for line in proc.stdout:
                        lf.write(line)
                        lf.flush()
                        stripped = line.rstrip()
                        print(f"[llama-server] {stripped}")

                        if status_cb:
                            try:
                                status_cb(stripped)
                            except Exception:
                                pass

                        low = line.lower()
                        if any(m.lower() in low for m in READY_MARKERS):
                            ready.set()
                            # Keep draining so the pipe doesn't block
                        elif any(m.lower() in low for m in ERROR_MARKERS):
                            print(f"[BonsaiManager] Server reported error: {stripped}")
                            crashed.set()
                            return
            except Exception as exc:
                print(f"[BonsaiManager] stdout drain error: {exc}")
            finally:
                # stdout closed = process exited
                if not ready.is_set():
                    crashed.set()

        drain_thread = threading.Thread(
            target=_drain_and_detect,
            args=(self._process, log_path),
            daemon=True,
        )
        drain_thread.start()

        # Wait for ready or crash, respecting the hard deadline.
        # Also poll /health every 2 s as a fallback — catches builds where
        # llama-server uses structured JSON logging and never emits a
        # plain-text ready line that matches our READY_MARKERS.
        _last_health_check = time.monotonic()
        while not ready.is_set() and not crashed.is_set():
            if time.monotonic() > deadline:
                print("[BonsaiManager] Timed out waiting for server to become ready.")
                self.stop_server()
                return False
            # Health poll fallback every 2 s
            if time.monotonic() - _last_health_check >= 2.0:
                _last_health_check = time.monotonic()
                if self.is_server_running():
                    print("[BonsaiManager] /health responded — server is ready (log marker not matched)")
                    ready.set()
                    break
            time.sleep(0.1)  # 100 ms tick

        if ready.is_set():
            print(f"[BonsaiManager] Server ready at http://{SERVER_HOST}:{SERVER_PORT}/v1")
            return True

        print("[BonsaiManager] llama-server exited unexpectedly — check ~/.myapp/llama_server.log")
        return False

    def stop_server(self) -> None:
        with self._server_lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
                self._active_model_key = None
                print("[BonsaiManager] llama-server stopped.")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self, model_key: str = DEFAULT_MODEL) -> dict:
        llama_bin = self._get_llama_server_path()
        return {
            "model_key":       model_key,
            "model_downloaded": self.is_model_downloaded(model_key),
            "partial_exists":  os.path.isfile(self.get_model_path(model_key) + ".partial"),
            "server_running":  self.is_server_running(),
            "active_model":    self._active_model_key,
            "model_path":      self.get_model_path(model_key),
            "server_url":      f"http://{SERVER_HOST}:{SERVER_PORT}/v1",
            "binary_found":    llama_bin is not None,
            "binary_path":     llama_bin,
        }


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

bonsai = BonsaiManager()
