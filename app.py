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
"""

import atexit
import os
import sys
import threading

import webview
from dotenv import load_dotenv

load_dotenv()


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


def _on_exit():
    """Ensure llama-server is terminated when Paramodus closes."""
    from local_model.manager import bonsai
    bonsai.stop_server()


if __name__ == '__main__':
    from local_model.manager import bonsai  # noqa: needed for atexit
    atexit.register(_on_exit)

    # Kick off heavy init in background immediately — it will be done
    # (or mostly done) by the time the user tries to send their first message.
    init_thread = threading.Thread(target=_background_init, daemon=True, name="bg-init")
    init_thread.start()

    from api.bridge import ApiBridge
    api = ApiBridge()

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
