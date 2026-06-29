"""
scripts/get_llama_server.py
===========================
Downloads a pre-built llama-server binary for the current platform.

Usage
-----
    python scripts/get_llama_server.py              # installs to ~/.myapp/bin/ (PrismML fork)
    python scripts/get_llama_server.py --local      # installs to ./bin/ (for PyInstaller bundling)
    python scripts/get_llama_server.py --standard   # use standard llama.cpp instead of PrismML fork
    python scripts/get_llama_server.py --prismml    # build PrismML fork from source (needs cmake)

Default behaviour (no flags):
    Downloads a pre-built PrismML llama.cpp release binary — the only binary
    that supports the native Bonsai 8B Q1_0_g128 1-bit kernel.  No compiler
    or cmake required.

For the bartowski Q4_K_M variant only, the standard llama.cpp binary works fine
(use --standard in that case).

After running this script, the binary is automatically detected by
local_model/manager.py via ~/.myapp/bin/ (or PATH).
"""

import argparse
import os
import platform
import shutil
import sys
import zipfile

import requests

GITHUB_RELEASES_URL        = "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest"
PRISMML_RELEASES_URL       = "https://api.github.com/repos/PrismML-Eng/llama.cpp/releases/latest"
PRISMML_REPO_URL           = "https://github.com/PrismML-Eng/llama.cpp.git"

APP_DATA_BIN = os.path.join(os.path.expanduser("~"), ".myapp", "bin")
LOCAL_BIN    = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


# ---------------------------------------------------------------------------
# Standard llama.cpp release
# ---------------------------------------------------------------------------

def _find_release_asset(assets: list[dict]) -> tuple[str, str]:
    """
    Pick the best release asset for the current platform.

    Asset naming convention (as of llama.cpp b3xxx+):
      llama-b<N>-bin-win-avx2-x64.zip
      llama-b<N>-bin-ubuntu-x64.zip
      llama-b<N>-bin-macos-arm64.zip
    """
    system  = platform.system()
    machine = platform.machine().lower()

    is_arm = "arm" in machine or "aarch64" in machine

    def _score(name: str) -> int:
        n = name.lower()
        score = 0

        # "cudart-" packages are CUDA runtime DLL packs only — no server binary.
        # They must never be selected regardless of platform.
        if n.startswith("cudart"):
            return -100

        if system == "Windows":
            # Must be a llama main binary zip
            if not (n.startswith("llama-") and "bin" in n and "win" in n and n.endswith(".zip")):
                return 0
            score += 10

            # Architecture — critical: wrong arch = Machine Type Mismatch crash
            if is_arm:
                if "arm64" in n:  score += 8
                else:             score -= 20
            else:
                if "x64" in n:   score += 8
                if "arm64" in n: score -= 20

            # Prefer CPU-only build (broadest hardware compatibility, no GPU drivers needed)
            if "cpu" in n:                          score += 4
            elif "cuda" in n or "vulkan" in n or "hip" in n or "sycl" in n:
                score -= 2   # deprioritise GPU-specific builds

            # Legacy AVX scoring (for older release naming that had avx2 in name)
            if "avx2" in n:   score += 2
            elif "avx" in n:  score += 1

        elif system == "Linux":
            if not (n.startswith("llama-") and "bin" in n and n.endswith((".zip", ".tar.gz"))):
                return 0
            score += 10
            if is_arm:
                if "arm64" in n or "aarch64" in n: score += 5
            else:
                if "x64" in n or "amd64" in n:    score += 5
            if "cpu" in n:  score += 3

        elif system == "Darwin":
            if not (n.startswith("llama-") and "macos" in n and n.endswith((".zip", ".tar.gz"))):
                return 0
            score += 10
            if is_arm and "arm64" in n:      score += 5
            elif not is_arm and "x64" in n:  score += 5

        return score

    ranked = sorted(assets, key=lambda a: _score(a["name"]), reverse=True)
    if not ranked or _score(ranked[0]["name"]) <= 0:
        raise RuntimeError(
            f"No compatible llama.cpp binary found for {system}/{machine}.\n"
            "Please download manually from https://github.com/ggerganov/llama.cpp/releases"
        )
    best = ranked[0]
    return best["browser_download_url"], best["name"]


def _download_file(url: str, dest_path: str) -> None:
    print(f"  → {url}")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done  = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done / total * 100
                    mb  = done / (1024 ** 2)
                    print(f"\r    {pct:5.1f}%  {mb:7.1f} MB", end="", flush=True)
    print()


def _extract_server_binary(archive_path: str, dest_dir: str) -> str:
    """
    Extract llama-server[.exe] AND all companion DLLs/SOs from the archive.
    llama-server.exe depends on llama.dll, ggml.dll, etc. — they must all
    live in the same directory or the exe will fail with a missing-DLL error.
    """
    exe_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    server_path: str | None = None

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as z:
            for member in z.namelist():
                base = os.path.basename(member)
                if not base:          # skip directory entries
                    continue
                # Extract the server exe and every DLL in the archive
                is_server = (base == exe_name)
                is_dll    = base.lower().endswith(".dll")
                if is_server or is_dll:
                    dest_path = os.path.join(dest_dir, base)
                    with z.open(member) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if is_server:
                        if sys.platform != "win32":
                            os.chmod(dest_path, 0o755)
                        server_path = dest_path
    else:
        import tarfile
        with tarfile.open(archive_path) as t:
            for member in t.getmembers():
                base = os.path.basename(member.name)
                is_server = (base == exe_name)
                is_lib    = base.lower().endswith((".so", ".dylib"))
                if is_server or is_lib:
                    dest_path = os.path.join(dest_dir, base)
                    f = t.extractfile(member)
                    if f is None:
                        continue
                    with open(dest_path, "wb") as dst:
                        shutil.copyfileobj(f, dst)
                    os.chmod(dest_path, 0o755)
                    if is_server:
                        server_path = dest_path

    if server_path is None:
        raise RuntimeError(f"Could not find {exe_name} inside {archive_path}")
    return server_path


def download_standard_binary(dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)

    print("Fetching latest llama.cpp release info from GitHub…")
    resp = requests.get(GITHUB_RELEASES_URL, timeout=15)
    resp.raise_for_status()
    data   = resp.json()
    tag    = data["tag_name"]
    assets = data["assets"]

    print(f"Latest release: {tag}")
    url, filename = _find_release_asset(assets)
    print(f"Downloading {filename}…")

    archive_path = os.path.join(dest_dir, filename)
    _download_file(url, archive_path)

    print("Extracting llama-server…")
    binary_path = _extract_server_binary(archive_path, dest_dir)

    os.remove(archive_path)
    print(f"✓ llama-server installed at {binary_path}")
    return binary_path


# ---------------------------------------------------------------------------
# PrismML pre-built release download (no cmake needed)
# ---------------------------------------------------------------------------

def _find_prismml_release_asset(assets: list[dict]) -> tuple[str, str]:
    """
    Pick the best PrismML release asset for the current platform.

    PrismML release naming conventions:
      Windows CPU:  llama-bin-win-cpu-x64.zip / llama-bin-win-cpu-arm64.zip
      Windows CUDA: llama-prism-b1-*-bin-win-cuda-12.4-x64.zip
      macOS ARM64:  llama-prism-*-bin-macos-arm64.tar.gz
      macOS x64:    llama-prism-*-bin-macos-x64.tar.gz
      Linux x64:    llama-prism-*-bin-ubuntu-x64.tar.gz
      Linux ARM64:  llama-prism-*-bin-ubuntu-arm64.tar.gz

    Preference order: CUDA > Vulkan > CPU (so GPU users get acceleration).
    Falls back to CPU build if no GPU-specific asset matches.
    """
    system  = platform.system()
    machine = platform.machine().lower()
    is_arm  = "arm" in machine or "aarch64" in machine

    # Check for CUDA availability
    has_cuda = shutil.which("nvcc") is not None

    def _score(name: str) -> int:
        n = name.lower()
        score = 0

        # Skip CUDA runtime DLL-only packs
        if n.startswith("cudart"):
            return -100
        # Skip source archives
        if "xcframework" in n or n.endswith(".tar.gz.sha256") or n.endswith(".zip.sha256"):
            return -100

        if system == "Windows":
            # Must be a zip
            if not n.endswith(".zip"):
                return 0
            score += 10

            # Architecture
            if is_arm:
                if "arm64" in n:  score += 8
                else:             score -= 20
            else:
                if "x64" in n:   score += 8
                if "arm64" in n: score -= 20

            # GPU vs CPU
            if has_cuda and "cuda" in n:  score += 6
            elif "cpu" in n:              score += 4
            elif "vulkan" in n:           score += 3

        elif system == "Linux":
            if not n.endswith(".tar.gz"):
                return 0
            # Must be ubuntu/linux build (not macOS)
            if not ("ubuntu" in n or "linux" in n):
                return 0
            score += 10

            if is_arm:
                if "arm64" in n or "aarch64" in n: score += 5
            else:
                if "x64" in n or "amd64" in n:    score += 5

            if has_cuda and "cuda" in n:  score += 6
            elif "vulkan" in n:           score += 3
            elif not ("cuda" in n or "vulkan" in n or "rocm" in n): score += 2  # plain CPU build

        elif system == "Darwin":
            if not n.endswith(".tar.gz"):
                return 0
            if "macos" not in n:
                return 0
            score += 10
            if is_arm and "arm64" in n:      score += 5
            elif not is_arm and "x64" in n:  score += 5
            # Prefer KleidiAI build on Apple Silicon (faster Q1 kernels)
            if is_arm and "kleidiai" in n:   score += 2

        return score

    ranked = sorted(assets, key=lambda a: _score(a["name"]), reverse=True)
    if not ranked or _score(ranked[0]["name"]) <= 0:
        raise RuntimeError(
            f"No compatible PrismML binary found for {system}/{machine}.\n"
            "Visit https://github.com/PrismML-Eng/llama.cpp/releases to download manually."
        )
    best = ranked[0]
    return best["browser_download_url"], best["name"]


def download_prismml_binary(dest_dir: str) -> str:
    """
    Download a pre-built PrismML llama.cpp binary from GitHub releases.
    This is the recommended path: no cmake, no compiler, no git-clone needed.
    Required for native Bonsai 8B Q1_0_g128 1-bit kernel support.
    """
    os.makedirs(dest_dir, exist_ok=True)

    print("Fetching latest PrismML llama.cpp release info from GitHub…")
    resp = requests.get(PRISMML_RELEASES_URL, timeout=15)
    resp.raise_for_status()
    data   = resp.json()
    tag    = data["tag_name"]
    assets = data["assets"]

    print(f"Latest PrismML release: {tag}")
    url, filename = _find_prismml_release_asset(assets)
    print(f"Downloading {filename}…")

    archive_path = os.path.join(dest_dir, filename)
    _download_file(url, archive_path)

    print("Extracting llama-server…")
    binary_path = _extract_server_binary(archive_path, dest_dir)

    os.remove(archive_path)
    print(f"✓ PrismML llama-server installed at {binary_path}")
    return binary_path


# ---------------------------------------------------------------------------
# PrismML fork (build from source)
# ---------------------------------------------------------------------------

def build_prismml_binary(dest_dir: str) -> str:
    """
    Clone PrismML's llama.cpp fork and build llama-server.
    Requires: git, cmake, a C++17 compiler.
    Optional: CUDA toolkit for GPU support.
    """
    import subprocess

    os.makedirs(dest_dir, exist_ok=True)
    build_dir = os.path.join(dest_dir, "_prismml_build")

    print("Cloning PrismML/llama.cpp fork…")
    subprocess.run(
        ["git", "clone", "--depth=1", PRISMML_REPO_URL, build_dir],
        check=True
    )

    # Detect CUDA
    has_cuda = shutil.which("nvcc") is not None
    cmake_args = ["-B", "build"]
    if has_cuda:
        cmake_args += ["-DGGML_CUDA=ON"]
        print("CUDA detected — building with GPU support.")
    else:
        print("No CUDA detected — building CPU-only binary.")

    print("Running cmake configure…")
    subprocess.run(["cmake"] + cmake_args, check=True, cwd=build_dir)

    print("Building llama-server (this takes a few minutes)…")
    subprocess.run(
        ["cmake", "--build", "build", "--config", "Release", "-j", "--target", "llama-server"],
        check=True,
        cwd=build_dir
    )

    exe_name   = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    built_path = None

    # Locate the binary in common build output locations
    for candidate_rel in [
        os.path.join("build", "bin", "Release", exe_name),
        os.path.join("build", "bin", exe_name),
        os.path.join("build", exe_name),
    ]:
        candidate = os.path.join(build_dir, candidate_rel)
        if os.path.isfile(candidate):
            built_path = candidate
            break

    if built_path is None:
        raise RuntimeError("Could not locate built llama-server binary.")

    dest_path = os.path.join(dest_dir, exe_name)
    shutil.copy2(built_path, dest_path)
    if sys.platform != "win32":
        os.chmod(dest_path, 0o755)

    shutil.rmtree(build_dir, ignore_errors=True)
    print(f"✓ PrismML llama-server built at {dest_path}")
    return dest_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download or build the llama-server binary for Paramodus."
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Install to ./bin/ (for PyInstaller bundling) instead of ~/.myapp/bin/",
    )
    parser.add_argument(
        "--standard",
        action="store_true",
        help="Download standard llama.cpp binary instead of PrismML fork (only for Q4_K_M variant).",
    )
    parser.add_argument(
        "--prismml",
        action="store_true",
        help=(
            "Build PrismML's llama.cpp fork from source (enables native Q1_0_g128 "
            "1-bit kernels). Requires git, cmake, and a C++17 compiler."
        ),
    )
    args = parser.parse_args()

    dest_dir = LOCAL_BIN if args.local else APP_DATA_BIN

    try:
        if args.prismml:
            # Build from source (cmake required)
            path = build_prismml_binary(dest_dir)
        elif args.standard:
            # Standard llama.cpp release (only works with Q4_K_M unpacked model)
            path = download_standard_binary(dest_dir)
        else:
            # Default: download PrismML pre-built release (no cmake needed)
            # Required for native Bonsai 8B Q1_0_g128 1-bit kernel
            path = download_prismml_binary(dest_dir)

        print()
        print("Done.  Paramodus will automatically detect the binary at:")
        print(f"  {path}")
        print()
        if args.local:
            print("Include it in your PyInstaller build via paramodus.spec:")
            print("  binaries=[('bin/llama-server.exe', '.')]")
    except Exception as exc:
        print(f"\n✗ Error: {exc}", file=sys.stderr)
        sys.exit(1)
