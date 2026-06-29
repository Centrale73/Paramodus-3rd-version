"""
build.py — One-command Paramodus .exe builder.

Usage:
    python build.py

What it does:
    1. Downloads the PrismML llama-server binary into ./bin/  (skipped if present)
    2. Downloads the Bonsai 8B GGUF model into ./models/      (skipped if present)
    3. Runs PyInstaller with paramodus.spec
    4. Prints the path to the final dist/ folder

End users receive the dist/Paramodus/ folder.  They open Paramodus.exe and
chat immediately — no internet connection, no API keys, no extra steps.

The model (~1.15 GB) is bundled inside the exe distribution so the binary +
model ship together as a single distributable.
"""

import subprocess
import sys
import os
import shutil


def _find_iscc() -> str | None:
    """
    Locate the Inno Setup compiler (iscc.exe) on Windows.
    Checks PATH first, then the default Inno Setup 6 install location.
    Returns the full path if found, None otherwise.
    """
    # 1. PATH
    result = shutil.which("iscc")
    if result:
        return result

    # 2. Default Inno Setup 6 install paths
    # Inno Setup can be installed per-user (AppData\Local) or system-wide
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        # Per-user install (default when installed without admin rights)
        os.path.join(local_appdata, "Programs", "Inno Setup 6", "ISCC.exe"),
        os.path.join(local_appdata, "Programs", "Inno Setup 5", "ISCC.exe"),
        # System-wide installs
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        r"C:\Program Files\Inno Setup 5\ISCC.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


def main():
    root = os.path.dirname(os.path.abspath(__file__))

    # ── Step 1: PrismML llama-server binary ───────────────────────────────────
    # The PrismML fork is required for native Q1_0_g128 1-bit kernel support.
    # A pre-built binary is downloaded from GitHub releases — no cmake needed.
    exe_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    bin_path  = os.path.join(root, "bin", exe_name)

    if os.path.isfile(bin_path):
        print(f"[build] llama-server already in bin/ — skipping download.")
    else:
        print("[build] Downloading PrismML llama-server binary (1-bit kernel support)…")
        # No --standard flag = downloads PrismML pre-built release by default
        result = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "get_llama_server.py"), "--local"],
            check=False,
        )
        if result.returncode != 0:
            print("[build] ERROR: Failed to download llama-server. Check your internet connection.")
            sys.exit(1)
        if not os.path.isfile(bin_path):
            print(f"[build] ERROR: Expected binary not found at {bin_path}")
            sys.exit(1)
        print(f"[build] llama-server ready at {bin_path}")

    # ── Step 2: Bonsai 8B model ───────────────────────────────────────────────
    # Download the native 1-bit Bonsai GGUF (~1.15 GB) into ./models/ so that
    # paramodus.spec can bundle it.  End users never download it themselves.
    model_path = os.path.join(root, "models", "Bonsai-8B.gguf")

    if os.path.isfile(model_path):
        size_gb = os.path.getsize(model_path) / (1024 ** 3)
        print(f"[build] Bonsai-8B.gguf already in models/ ({size_gb:.2f} GB) — skipping download.")
    else:
        print("[build] Downloading Bonsai 8B model (~1.15 GB) into ./models/ for bundling…")
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(root, "scripts", "download_model_for_bundle.py"),
                "--model", "bonsai-8b-native",
            ],
            check=False,
        )
        if result.returncode != 0:
            print("[build] ERROR: Failed to download Bonsai model. Check your internet connection.")
            sys.exit(1)
        if not os.path.isfile(model_path):
            print(f"[build] ERROR: Expected model not found at {model_path}")
            sys.exit(1)
        print(f"[build] Bonsai-8B.gguf ready at {model_path}")

    # ── Step 3: PyInstaller ───────────────────────────────────────────────────
    if shutil.which("pyinstaller") is None:
        print("[build] PyInstaller not found — installing…")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    spec = os.path.join(root, "paramodus.spec")
    print("[build] Running PyInstaller…")
    result = subprocess.run(
        ["pyinstaller", spec, "--clean", "--noconfirm"],
        check=False,
    )
    if result.returncode != 0:
        print("[build] ERROR: PyInstaller failed.")
        sys.exit(1)

    # ── Step 4: Inno Setup installer (Windows only, optional) ───────────────
    # If Inno Setup is installed, automatically produce a single
    # ParamodusSetup.exe installer that end users just double-click.
    # Download Inno Setup from: https://jrsoftware.org/isdl.php
    installer_exe = None
    if sys.platform == "win32":
        iscc = _find_iscc()
        iss  = os.path.join(root, "paramodus.iss")
        if iscc and os.path.isfile(iss):
            os.makedirs(os.path.join(root, "installer"), exist_ok=True)
            print("[build] Running Inno Setup to produce ParamodusSetup.exe…")
            result = subprocess.run([iscc, iss], check=False)
            installer_path = os.path.join(root, "installer", "ParamodusSetup.exe")
            if result.returncode == 0 and os.path.isfile(installer_path):
                size_mb = os.path.getsize(installer_path) / (1024 ** 2)
                installer_exe = installer_path
                print(f"[build] ✓ Installer ready: {installer_path} ({size_mb:.0f} MB)")
            else:
                print("[build] WARNING: Inno Setup failed — distributing folder instead.")
        elif not iscc:
            print(
                "[build] Inno Setup not found — skipping installer generation.\n"
                "[build]   Install from https://jrsoftware.org/isdl.php for a single-file installer."
            )

    # ── Done ──────────────────────────────────────────────────────────────────
    dist_dir = os.path.join(root, "dist", "Paramodus")
    exe_out  = os.path.join(dist_dir, "Paramodus.exe" if sys.platform == "win32" else "Paramodus")
    if os.path.isfile(exe_out):
        dist_size_mb = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, dn, fn in os.walk(dist_dir) for f in fn
        ) / (1024 ** 2)
        print(f"\n[build] ✓ Build complete ({dist_size_mb:.0f} MB uncompressed)")
        if installer_exe:
            ins_mb = os.path.getsize(installer_exe) / (1024 ** 2)
            print(f"[build]   Distribute: installer/ParamodusSetup.exe ({ins_mb:.0f} MB)")
            print("[build]   Users double-click ParamodusSetup.exe → install → chat.")
        else:
            print(f"[build]   Distribute: dist/Paramodus/ folder (zip it first)")
            print("[build]   Install Inno Setup to auto-produce a single-file installer.")
    else:
        print(f"\n[build] Build finished. Check the dist/ folder.")


if __name__ == "__main__":
    main()
