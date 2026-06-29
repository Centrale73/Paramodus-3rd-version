"""
app.py — Paramodus entry point.

Startup sequence
----------------
1. Create the pywebview window immediately (user sees the UI within ~1s)
2. In a background thread, run all heavy initialisation:
   - init_db()
   - Import workspace_agent (pulls in agno, torch, fastembed, etc.)
   - begin_auto_setup (download model if needed + start llama-server)
3. The JS side calls begin_auto_setup() via pywebview API only after
   pywebviewready fires, by which point the window is already visible.

Why: PyInstaller frozen exes decompress hundreds of .pyc files from
_MEIPASS on first import.  Heavy libraries (agno, torch, fastembed,
lancedb) can take 20-60 seconds.  Running them on the main thread
makes the window appear frozen/unresponsive until they finish.

Fixes applied
-------------
FIX 1 — sys.excepthook patch (safety net):
    pythonnet 3.x reflects WebView2's AccessibilityObject.Bounds.Empty
    struct recursively until Python hits RecursionError.  We intercept
    RecursionError silently at the top-level excepthook so it never
    crashes the process.  setrecursionlimit(2000) gives extra breathing
    room without blowing the real stack.

FIX 2 — WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS env var:
    Passes --disable-renderer-accessibility to the WebView2 runtime
    before webview.start() so the COM accessibility tree is never
    exposed — prevents the pythonnet reflection from being triggered
    in the first place.  Belt-and-suspenders with FIX 1.

FIX 3 — lazy bonsai import on main thread:
    The top-level `from local_model.manager import bonsai` ran on the
    main thread before the window was created, blocking GUI startup.
    Moved to a lazy import inside _on_exit() and a local reference
    inside __main__ only when strictly needed.

FIX 4 — _background_init signals completion via threading.Event:
    _init_done is set once all heavy imports finish.  ApiBridge methods
    called by JS immediately after pywebviewready now return a safe
    {"status": "initializing"} dict instead of blocking or crashing.
"""

# ── FIX 1: patch excepthook BEFORE any other import ─────────────────────────
import sys

sys.setrecursionlimit(2000)  # default 1000 is too tight for pythonnet reflection

_orig_excepthook = sys.excepthook


def _safe_excepthook(exc_type, exc_value, exc_tb):
    """Silently swallow the pywebview/pythonnet AccessibilityObject recursion bug."""
    if exc_type is RecursionError:
        # Always caused by pythonnet reflecting WebView2 accessibility structs.
        # Log once so it is still visible in dev, then suppress.
        print("[app] Suppressed RecursionError (pywebview/pythonnet a11y bug)")
        return
    _orig_excepthook(exc_type, exc_value, exc_tb)


sys.excepthook = _safe_excepthook
# ─────────────────────────────────────────────────────────────────────────────

# ── FIX 2: disable WebView2 accessibility BEFORE webview is imported ─────────
import os
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-renderer-accessibility"
# ─────────────────────────────────────────────────────────────────────────────

import atexit
import threading

import webview
from dotenv import load_dotenv

load_dotenv()

# ── FIX 4: event that signals JS-facing bridge methods when init is done ─────
_init_done = threading.Event()
# ─────────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Heavy imports are deferred to a background thread (see _background_init).
# Only lightweight imports happen here so the window opens instantly.
# ---------------------------------------------------------------------------

def _background_init():
    """
    Run all slow initialisation off the main thread so the window stays
    responsive while libraries are being imported and the DB is set up.
    """
    from database import init_db
    init_db()

    # Importing workspace_agent triggers agno, fastembed, lancedb, etc.
    # This can take 20-60 s on a cold PyInstaller run — keep it off the GUI thread.
    import agents.workspace_agent  # noqa: F401  — side-effect: initialises module globals

    # FIX 4: signal that all heavy init is complete
    _init_done.set()
    print("[app] Background init complete — bridge is now fully available.")


# ── FIX 3: bonsai is no longer imported on the main thread at startup ────────
def _on_exit():
    """Ensure llama-server is terminated when Paramodus closes."""
    try:
        from local_model.manager import bonsai  # lazy — safe to call multiple times
        bonsai.stop_server()
    except Exception as exc:
        print(f"[app] _on_exit error (non-fatal): {exc}")
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == '__main__':
    atexit.register(_on_exit)

    # Kick off heavy init in background immediately — it will be done
    # (or mostly done) by the time the user tries to send their first message.
    init_thread = threading.Thread(target=_background_init, daemon=True, name="bg-init")
    init_thread.start()

    from api.bridge import ApiBridge
    api = ApiBridge()
    api.set_init_event(_init_done)   # FIX 4: give bridge a reference to _init_done

    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    html_path  = os.path.join(base_path, "ui", "index.html")

    window = webview.create_window(
        "Agentic Workspace",
        html_path,
        js_api=api,
        width=1100,
        height=850,
        background_color="#180079",
    )
    api.set_window(window)
    window.events.closed += _on_exit

    webview.start(debug=False)
