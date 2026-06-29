"""
BonsaiManager — lifecycle manager for the Bonsai 8B local model server.

Binary + model layout mirrors PrismML-Eng/Bonsai-demo exactly so that
running `python scripts/setup_bonsai.py` (or the official setup.ps1) is the
only prerequisite — no extra download step needed.

  Binary resolution order (within the project root):
    bin/cuda/llama-server.exe   ← CUDA build (setup.ps1 GPU path)
    bin/hip/llama-server.exe    ← AMD HIP/ROCm build
    bin/vulkan/llama-server.exe ← Vulkan build
    bin/cpu/llama-server.exe    ← CPU-only build
    llama.cpp/build/bin/Release/llama-server.exe  ← locally compiled
    llama.cpp/build/bin/llama-server.exe
    ~/.myapp/bin/llama-server.exe  ← legacy fallback
    system PATH

  Model resolution order:
    <project_root>/models/gguf/8B/*.Q1_0*.gguf  ← bonsai (1-bit native)
    <project_root>/models/gguf/8B/*.gguf        ← any quant fallback
    ~/.myapp/models/Bonsai-8B.gguf              ← legacy location

Server flags are taken directly from PrismML's start_llama_server.ps1:
  --temp 0.5 --top-p 0.85 --top-k 20 --min-p 0
  --reasoning-budget 0 --reasoning-format none
  --chat-template-kwargs '{"enable_thinking": false}'
"""

import atexit
import glob
import json
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
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True, timeout=5
                )
                print(f"[BonsaiManager] Freed port {port} — killed PID {pid}")
                time.sleep(0.5)
    except Exception as exc:
        print(f"[BonsaiManager] Could not free port {port}: {exc}")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root = two levels up from this file (local_model/manager.py)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_DATA   = os.path.join(os.path.expanduser("~"), ".myapp")
MODELS_DIR = os.path.join(APP_DATA, "models")
BIN_DIR    = os.path.join(APP_DATA, "bin")

for _d in (APP_DATA, MODELS_DIR, BIN_DIR):
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# PrismML release coordinates (mirrors setup.ps1)
# ---------------------------------------------------------------------------

PRISM_RELEASE_TAG  = "prism-b8846-d104cf1"
PRISM_WIN_ASSET_TAG = "prism-b1-d104cf1"          # Windows zip names use this
PRISM_BASE_URL = (
    f"https://github.com/PrismML-Eng/llama.cpp/releases/download/{PRISM_RELEASE_TAG}"
)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

MODELS: dict[str, dict] = {
    "bonsai-8b-native": {
        "hf_repo":     "prism-ml/Bonsai-8B-gguf",
        "hf_pattern":  "*Q1_0*.gguf",
        "local_dir":   os.path.join(_PROJECT_ROOT, "models", "gguf", "8B"),
        "size_gb":     1.15,
        "quant":       "Q1_0_g128 (native 1-bit)",
        "description": "Native 1-bit (~1.15 GB). Requires PrismML llama.cpp fork.",
        "requires_prismml_fork": True,
    },
    "bonsai-8b-q4": {
        "hf_repo":     "bartowski/prism-ml_Bonsai-8B-unpacked-GGUF",
        "hf_pattern":  "*Q4_K_M*.gguf",
        "local_dir":   os.path.join(_PROJECT_ROOT, "models", "gguf", "8B"),
        "size_gb":     4.6,
        "quant":       "Q4_K_M (unpacked, standard llama.cpp)",
        "description": "Unpacked Q4_K_M (~4.6 GB). Works with standard llama-server.",
        "requires_prismml_fork": False,
    },
}

DEFAULT_MODEL   = "bonsai-8b-native"
SERVER_HOST     = "127.0.0.1"
SERVER_PORT     = 8081
HEALTH_URL      = f"http://{SERVER_HOST}:{SERVER_PORT}/health"

# ---------------------------------------------------------------------------
# BonsaiManager
# ---------------------------------------------------------------------------

class BonsaiManager:

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._server_lock   = threading.Lock()
        self._download_lock = threading.Lock()
        self._active_model_key: Optional[str] = None

    # ------------------------------------------------------------------
    # Binary resolution — mirrors Bonsai-demo start_llama_server.ps1
    # ------------------------------------------------------------------

    def _get_llama_server_path(self) -> Optional[str]:
        exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
        candidates: list[str] = []

        # 1. PyInstaller bundle
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(sys._MEIPASS, exe))
            candidates.append(os.path.join(os.path.dirname(sys.executable), exe))

        # 2. Bonsai-demo layout: bin/{cuda,hip,vulkan,cpu}/ relative to project root
        for subdir in ("cuda", "hip", "vulkan", "cpu"):
            candidates.append(os.path.join(_PROJECT_ROOT, "bin", subdir, exe))

        # 3. Locally compiled llama.cpp build directories
        candidates.append(os.path.join(_PROJECT_ROOT, "llama.cpp", "build", "bin", "Release", exe))
        candidates.append(os.path.join(_PROJECT_ROOT, "llama.cpp", "build", "bin", exe))

        # 4. Legacy ~/.myapp/bin/
        candidates.append(os.path.join(BIN_DIR, exe))

        # 5. System PATH
        path_result = shutil.which("llama-server")
        if path_result:
            candidates.append(path_result)

        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    # ------------------------------------------------------------------
    # Model path helpers — mirrors Bonsai-demo model dir layout
    # ------------------------------------------------------------------

    def get_model_path(self, model_key: str = DEFAULT_MODEL) -> str:
        info = MODELS[model_key]
        local_dir = info["local_dir"]
        pattern   = info["hf_pattern"]

        # PyInstaller: check bundle locations first
        if getattr(sys, "frozen", False):
            for base in (sys._MEIPASS, os.path.dirname(sys.executable)):
                frozen_dir = os.path.join(base, "models", "gguf", "8B")
                matches = glob.glob(os.path.join(frozen_dir, pattern))
                if matches:
                    return matches[0]

        # Project-relative Bonsai-demo layout: models/gguf/8B/
        matches = glob.glob(os.path.join(local_dir, pattern))
        if matches:
            return matches[0]

        # Any .gguf in the same dir (alternate quant)
        any_gguf = glob.glob(os.path.join(local_dir, "*.gguf"))
        if any_gguf:
            return any_gguf[0]

        # Legacy ~/.myapp/models/ fallback
        legacy = os.path.join(MODELS_DIR, "Bonsai-8B.gguf")
        return legacy

    def is_model_downloaded(self, model_key: str = DEFAULT_MODEL) -> bool:
        path = self.get_model_path(model_key)
        return os.path.isfile(path) and not os.path.isfile(path + ".partial")

    def get_models(self) -> list[dict]:
        result = []
        for key, info in MODELS.items():
            result.append({
                "key":          key,
                "size_gb":      info["size_gb"],
                "quant":        info["quant"],
                "description":  info["description"],
                "requires_prismml_fork": info["requires_prismml_fork"],
                "downloaded":   self.is_model_downloaded(key),
                "model_path":   self.get_model_path(key),
            })
        return result

    # ------------------------------------------------------------------
    # Download (streaming, resume-capable) — via huggingface_hub
    # ------------------------------------------------------------------

    def download_model(
        self,
        model_key: str = DEFAULT_MODEL,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> bool:
        with self._download_lock:
            if self.is_model_downloaded(model_key):
                if progress_cb:
                    progress_cb(100.0, "Already downloaded.")
                return True

            from huggingface_hub import hf_hub_download

            info       = MODELS[model_key]
            local_dir  = info["local_dir"]
            os.makedirs(local_dir, exist_ok=True)
            total_bytes = int(info["size_gb"] * 1024 ** 3)

            if progress_cb:
                progress_cb(0.0, f"Connecting to HuggingFace… ({info['size_gb']:.1f} GB)")

            _stop = threading.Event()

            def _poll_progress():
                while not _stop.is_set():
                    try:
                        for fname in os.listdir(local_dir):
                            if fname.endswith(".incomplete") or fname.endswith(".part"):
                                fpath = os.path.join(local_dir, fname)
                                size  = os.path.getsize(fpath)
                                if size > 0 and total_bytes > 0 and progress_cb:
                                    pct      = min(size / total_bytes * 100, 99.0)
                                    done_gb  = size / (1024 ** 3)
                                    total_gb = total_bytes / (1024 ** 3)
                                    progress_cb(pct, f"Downloading… {done_gb:.2f} / {total_gb:.2f} GB")
                                break
                    except OSError:
                        pass
                    _stop.wait(timeout=1.5)

            monitor = threading.Thread(target=_poll_progress, daemon=True)
            monitor.start()

            try:
                # Download only the target quant pattern
                import fnmatch
                from huggingface_hub import list_repo_files
                pattern = info["hf_pattern"]
                matching = [
                    f for f in list_repo_files(info["hf_repo"])
                    if fnmatch.fnmatch(os.path.basename(f), pattern)
                ]
                if not matching:
                    raise FileNotFoundError(
                        f"No file matching '{pattern}' found in {info['hf_repo']}"
                    )
                filename = matching[0]

                hf_hub_download(
                    repo_id=info["hf_repo"],
                    filename=filename,
                    local_dir=local_dir,
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

    # ------------------------------------------------------------------
    # Binary download — pulls PrismML's prebuilt llama.cpp fork
    # ------------------------------------------------------------------

    def download_binary(
        self,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> bool:
        """
        Download the PrismML llama.cpp prebuilt binary for this platform.
        Mirrors the logic in setup.ps1 section 8.
        Places the binary under <project_root>/bin/{cuda|cpu}/
        """
        import zipfile

        def _report(msg: str):
            print(f"[BonsaiManager] {msg}")
            if progress_cb:
                progress_cb(-1.0, msg)   # -1 = indeterminate progress

        # GPU detection (same as _detect_gpu but returns type string)
        gpu_type = "cpu"
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    timeout=5, stderr=subprocess.DEVNULL,
                ).decode().strip()
                if out:
                    gpu_type = "cuda"
            except Exception:
                pass

        if gpu_type == "cpu" and sys.platform == "darwin":
            import platform as _platform
            if "arm" in _platform.machine().lower():
                gpu_type = "metal"   # macOS Metal — no prebuilt zip needed

        # Build asset name (Windows x64 only; Linux/Mac uses build scripts)
        if sys.platform != "win32":
            _report("Prebuilt binary download only supported on Windows. "
                    "On Linux/macOS, run scripts/build_cpu_linux.sh or build_cuda_linux.sh.")
            return False

        if gpu_type == "cuda":
            asset   = f"llama-{PRISM_WIN_ASSET_TAG}-bin-win-cuda-12.4-x64.zip"
            bin_sub = "cuda"
        else:
            asset   = f"llama-bin-win-cpu-x64.zip"
            bin_sub = "cpu"

        bin_dir  = os.path.join(_PROJECT_ROOT, "bin", bin_sub)
        exe_path = os.path.join(bin_dir, "llama-server.exe")
        if os.path.isfile(exe_path):
            _report(f"Binary already present: {exe_path}")
            if progress_cb:
                progress_cb(100.0, "Binary already present.")
            return True

        os.makedirs(bin_dir, exist_ok=True)
        url     = f"{PRISM_BASE_URL}/{asset}"
        tmp_zip = os.path.join(bin_dir, "_download.zip")

        _report(f"Downloading {asset} from PrismML release {PRISM_RELEASE_TAG} …")
        if progress_cb:
            progress_cb(0.0, f"Downloading {asset}…")

        try:
            import urllib.request
            urllib.request.urlretrieve(url, tmp_zip)

            _report(f"Extracting to {bin_dir} …")
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(bin_dir)
            os.remove(tmp_zip)

            if not os.path.isfile(exe_path):
                # Binary may be in a sub-folder inside the zip — hoist it up
                for root, _, files in os.walk(bin_dir):
                    if "llama-server.exe" in files:
                        src = os.path.join(root, "llama-server.exe")
                        if src != exe_path:
                            shutil.move(src, exe_path)
                        break

            if os.path.isfile(exe_path):
                if progress_cb:
                    progress_cb(100.0, "Binary installed ✓")
                _report(f"Binary installed: {exe_path}")
                return True
            else:
                _report("Extraction completed but llama-server.exe not found inside zip.")
                return False

        except Exception as exc:
            _report(f"Binary download failed: {exc}")
            if progress_cb:
                progress_cb(-1.0, f"Binary download failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # One-shot setup helper (binary + model)
    # ------------------------------------------------------------------

    def setup(
        self,
        model_key: str = DEFAULT_MODEL,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> bool:
        """Download the binary and model if not already present."""
        ok_bin   = self.download_binary(progress_cb=progress_cb)
        ok_model = self.download_model(model_key=model_key, progress_cb=progress_cb)
        return ok_bin and ok_model

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
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    timeout=5, stderr=subprocess.DEVNULL,
                ).decode().strip()
                if out:
                    print(f"[BonsaiManager] NVIDIA GPU: {out.splitlines()[0]} — ngl 99")
                    return 99
            except Exception:
                pass
        if sys.platform == "darwin":
            import platform as _platform
            if "arm" in _platform.machine().lower():
                print("[BonsaiManager] Apple Silicon — ngl 99 (Metal)")
                return 99
        print("[BonsaiManager] No discrete GPU — CPU only (ngl 0)")
        return 0

    def start_server(
        self,
        model_key:      str           = DEFAULT_MODEL,
        n_gpu_layers:   Optional[int] = None,
        context_length: int           = 0,       # 0 = auto-fit (matches Bonsai-demo)
        timeout_s:      int           = 360,
        status_cb:      Optional[Callable[[str], None]] = None,
    ) -> bool:
        with self._server_lock:
            if self.is_server_running():
                return True

            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None

            _free_port(SERVER_PORT)

            llama_bin = self._get_llama_server_path()
            if llama_bin is None:
                print(
                    "[BonsaiManager] llama-server not found.\n"
                    "  Run: python scripts/setup_bonsai.py\n"
                    "  Or run the official setup.ps1 from PrismML-Eng/Bonsai-demo\n"
                    "  Or add llama-server to PATH."
                )
                return False

            model_path = self.get_model_path(model_key)
            print(f"[BonsaiManager] Model path : {model_path}")
            print(f"[BonsaiManager] Model found: {os.path.isfile(model_path)}")
            if not os.path.isfile(model_path):
                print(f"[BonsaiManager] Model not found — run setup first.")
                return False

            print(f"[BonsaiManager] Binary: {llama_bin}")

            if n_gpu_layers is None:
                n_gpu_layers = self._detect_gpu()

            # chat-template-kwargs must be JSON; PowerShell escaping not needed in Python
            chat_template_kwargs = json.dumps({"enable_thinking": False})

            cmd = [
                llama_bin,
                "-m",    model_path,
                "--host", SERVER_HOST,
                "--port", str(SERVER_PORT),
                "-ngl",   str(n_gpu_layers),
                "-c",     str(context_length),   # 0 = auto-fit context
                "--temp", "0.5",
                "--top-p", "0.85",
                "--top-k", "20",
                "--min-p", "0",
                "--reasoning-budget", "0",
                "--reasoning-format", "none",
                "--chat-template-kwargs", chat_template_kwargs,
            ]

            log_path = os.path.join(APP_DATA, "llama_server.log")

            startupinfo = None
            extra_kwargs: dict = {}
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                extra_kwargs["startupinfo"] = startupinfo
                extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **extra_kwargs,
            )
            self._active_model_key = model_key
            atexit.register(self.stop_server)
            print(f"[BonsaiManager] Started llama-server pid={self._process.pid} "
                  f"model={model_key} ngl={n_gpu_layers}")

        ready    = threading.Event()
        deadline = time.monotonic() + timeout_s
        crashed  = threading.Event()

        def _drain_and_detect(proc: subprocess.Popen, log: str) -> None:
            READY_MARKERS = (
                "server is listening on",
                "llama server listening at",
                "HTTP server listening",
                "all slots are idle",
                "model loaded",
                "listening on",
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
                        elif any(m.lower() in low for m in ERROR_MARKERS):
                            print(f"[BonsaiManager] Server error: {stripped}")
                            crashed.set()
                            return
            except Exception as exc:
                print(f"[BonsaiManager] stdout drain error: {exc}")
            finally:
                if not ready.is_set():
                    crashed.set()

        drain_thread = threading.Thread(
            target=_drain_and_detect,
            args=(self._process, log_path),
            daemon=True,
        )
        drain_thread.start()

        _last_health_check = time.monotonic()
        while not ready.is_set() and not crashed.is_set():
            if time.monotonic() > deadline:
                print("[BonsaiManager] Timed out waiting for server.")
                self.stop_server()
                return False
            if time.monotonic() - _last_health_check >= 2.0:
                _last_health_check = time.monotonic()
                if self.is_server_running():
                    print("[BonsaiManager] /health responded — server ready")
                    ready.set()
                    break
            time.sleep(0.1)

        if ready.is_set():
            print(f"[BonsaiManager] Server ready at http://{SERVER_HOST}:{SERVER_PORT}/v1")
            return True

        print("[BonsaiManager] llama-server exited — check ~/.myapp/llama_server.log")
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
            "model_key":        model_key,
            "model_downloaded": self.is_model_downloaded(model_key),
            "server_running":   self.is_server_running(),
            "active_model":     self._active_model_key,
            "model_path":       self.get_model_path(model_key),
            "server_url":       f"http://{SERVER_HOST}:{SERVER_PORT}/v1",
            "binary_found":     llama_bin is not None,
            "binary_path":      llama_bin,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

bonsai = BonsaiManager()
