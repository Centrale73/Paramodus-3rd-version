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
   onBackendReady fires (polled from index.html), by which point the
   window is already visible and the bridge is fully initialised.

Fixes applied
-------------
FIX 1 — sys.excepthook + threading.excepthook patch (safety net):
    pythonnet 3.x reflects WebView2’s AccessibilityObject.Bounds.Empty
    struct recursively until Python hits RecursionError.  We intercept
    RecursionError silently at BOTH the top-level excepthook AND the
    per-thread excepthook (threading.excepthook) introduced in Python 3.8.
    This covers the case where the error fires on the .NET worker thread
    that manages the WebView2 COM controller, not the main thread.
    setrecursionlimit(2000) gives extra breathing room.

FIX 2 — Three WebView2 env vars to disable accessibility COM tree:
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: --disable-renderer-accessibility
        Disables the Blink/renderer-side accessibility tree.
    WEBVIEW2_RELEASE_CHANNEL_PREFERENCE: 0
        Pins channel so the env var is respected by the loader.
    WEBVIEW2_WIN32_COREWEBVIEW2CONTROLLER_OPTIONS: AllowExternalDrop=0
        Disables AllowExternalDrop on the COM controller, which is the
        second error in the log (CoreWebView2Controller.get_AllowExternalDrop
        failing via E_NOINTERFACE on cross-thread COM call).
    These must be set before `import webview`.

FIX 3 — lazy bonsai import:
    Moved `from local_model.manager import bonsai` out of module-level
    into _on_exit() so it never runs on the main thread at startup.

FIX 4 — _init_done threading.Event:
    Signals the JS bridge when all heavy background imports finish.
    JS polls get_init_status() every 500ms (index.html) and only calls
    bridge methods after ready:true is returned.
"""

# ── FIX 1: patch BOTH excepthooks BEFORE any other import ────────────────────
import sys
import threading

sys.setrecursionlimit(2000)  # default 1000 is too tight for pythonnet reflection

_orig_excepthook = sys.excepthook


def _safe_excepthook(exc_type, exc_value, exc_tb):
    """Silently swallow the pywebview/pythonnet AccessibilityObject recursion bug."""
    if exc_type is RecursionError:
        print("[app] Suppressed RecursionError (pywebview/pythonnet a11y bug)")
        return
    _orig_excepthook(exc_type, exc_value, exc_tb)


sys.excepthook = _safe_excepthook


# Python 3.8+: also patch the per-thread hook so errors on .NET worker
# threads (where the WebView2 COM controller lives) are also suppressed.
def _safe_thread_excepthook(args):
    if args.exc_type is RecursionError:
        print("[app] Suppressed RecursionError on thread (pywebview/pythonnet a11y bug)")
        return
    # Fall back to default behaviour for everything else
    threading.__excepthook__(args)  # built-in default handler


threading.excepthook = _safe_thread_excepthook
# ─────────────────────────────────────────────────────────────────────────────

# ── FIX 2: disable WebView2 accessibility + AllowExternalDrop BEFORE import ───
import os

# Disable Blink/renderer accessibility tree
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-renderer-accessibility"

# Disable AllowExternalDrop on the COM controller (fixes the second error:
# CoreWebView2Controller.get_AllowExternalDrop E_NOINTERFACE on cross-thread call)
os.environ["WEBVIEW2_WIN32_COREWEBVIEW2CONTROLLER_OPTIONS"] = "AllowExternalDrop=0"

# Pin release channel so both vars are respected by the runtime loader
os.environ["WEBVIEW2_RELEASE_CHANNEL_PREFERENCE"] = "0"
# ─────────────────────────────────────────────────────────────────────────────

import atexit

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
