"""
scripts/download_model_for_bundle.py
=====================================
Pre-downloads the Bonsai 8B model into ./models/ so it can be bundled
into the PyInstaller exe by paramodus.spec.

Run this ONCE before building the exe with build.py:
    python scripts/download_model_for_bundle.py

The model (~1.15 GB) is downloaded to ./models/Bonsai-8B.gguf and then
packaged into the .exe bundle.  End users never need an internet connection
to use the model — it is extracted from the bundle on first launch.

By default, this downloads the native 1-bit model (bonsai-8b-native,
Q1_0_g128, ~1.15 GB). To bundle the Q4_K_M variant instead, pass:
    python scripts/download_model_for_bundle.py --model bonsai-8b-q4
"""

import argparse
import os
import sys

# Resolve repo root
REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(REPO_ROOT, "models")

# Add repo root to path so we can import local_model.manager
sys.path.insert(0, REPO_ROOT)

from local_model.manager import MODELS


def download_for_bundle(model_key: str) -> str:
    """
    Download the specified model GGUF into ./models/ using huggingface_hub.
    Returns the local path.
    """
    if model_key not in MODELS:
        raise ValueError(
            f"Unknown model key: {model_key!r}\n"
            f"Valid keys: {list(MODELS.keys())}"
        )

    info     = MODELS[model_key]
    os.makedirs(MODELS_DIR, exist_ok=True)
    dest     = os.path.join(MODELS_DIR, info["filename"])

    if os.path.isfile(dest):
        size_gb = os.path.getsize(dest) / (1024 ** 3)
        print(f"[bundle-model] Already present: {dest} ({size_gb:.2f} GB) — skipping download.")
        return dest

    print(f"[bundle-model] Downloading {info['filename']} ({info['size_gb']:.1f} GB) from HuggingFace…")
    print(f"               Repo: {info['repo_id']}")

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("[bundle-model] ERROR: huggingface_hub not installed.")
        print("  Run: pip install huggingface_hub")
        sys.exit(1)

    hf_hub_download(
        repo_id=info["repo_id"],
        filename=info["filename"],
        local_dir=MODELS_DIR,
        local_dir_use_symlinks=False,
    )

    if not os.path.isfile(dest):
        raise RuntimeError(f"Download appeared to succeed but file not found at {dest}")

    size_gb = os.path.getsize(dest) / (1024 ** 3)
    print(f"[bundle-model] ✓ Downloaded: {dest} ({size_gb:.2f} GB)")
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pre-download Bonsai model into ./models/ for PyInstaller bundling."
    )
    parser.add_argument(
        "--model",
        default="bonsai-8b-native",
        choices=list(MODELS.keys()),
        help="Which model variant to download. Default: bonsai-8b-native (1.15 GB, 1-bit native).",
    )
    args = parser.parse_args()

    try:
        path = download_for_bundle(args.model)
        print()
        print("The model is now in ./models/ and will be bundled into the exe by paramodus.spec.")
        print("Next steps:")
        print("  1. python scripts/get_llama_server.py --local")
        print("  2. python build.py")
        print()
        print(f"Bundled file: {path}")
    except Exception as exc:
        print(f"\n✗ Error: {exc}", file=sys.stderr)
        sys.exit(1)
