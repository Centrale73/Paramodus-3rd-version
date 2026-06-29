import os
import threading
import json
import base64
import uuid
# NOTE: workspace_agent and database are NOT imported at module level.
# They pull in agno, fastembed, lancedb, and torch — on a PyInstaller frozen
# exe this can take 20-60 s as hundreds of .pyc files are decompressed from
# _MEIPASS.  Importing them here would block the main thread and make the
# window appear frozen before it even renders.
# Instead, they are imported lazily inside the methods that need them,
# by which point the background init thread in app.py has already loaded them.
from local_model.manager import bonsai, DEFAULT_MODEL


def _agent_module():
    """Lazy import of workspace_agent — safe to call from any thread."""
    from agents import workspace_agent
    return workspace_agent


def _db_module():
    """Lazy import of database functions."""
    from database import save_msg, get_history, clear_session, get_all_sessions
    return save_msg, get_history, clear_session, get_all_sessions

class ApiBridge:
    def __init__(self):
        # Load keys from environment
        self.keys = {
            "openai":     os.environ.get("OPENAI_API_KEY"),
            "anthropic":  os.environ.get("ANTHROPIC_API_KEY"),
            "gemini":     os.environ.get("GEMINI_API_KEY"),
            "groq":       os.environ.get("GROQ_API_KEY"),
            "grok":       os.environ.get("XAI_API_KEY"),
            "openrouter": os.environ.get("OPENROUTER_API_KEY"),
            "perplexity": os.environ.get("PERPLEXITY_API_KEY"),
            # Bonsai runs locally — no real key, but must not be None so the
            # api_key guard in start_chat_stream doesn't block it.
            "bonsai":     "local",
        }
        # Default provider and model
        self.current_provider = os.environ.get("DEFAULT_PROVIDER", "bonsai")
        self.current_model = os.environ.get("DEFAULT_MODEL", None)
        self.window = None
        self.multi_agent_mode = False
        self.uploaded_filenames = []
        # Session management
        self.current_session_id = str(uuid.uuid4())

    def set_window(self, window):
        self.window = window

    def new_session(self):
        """Start a new conversation session."""
        self.current_session_id = str(uuid.uuid4())
        return {"status": "success", "session_id": self.current_session_id}

    def list_sessions(self):
        """Retrieve list of all sessions."""
        _, get_history, _, get_all_sessions = _db_module()
        return get_all_sessions()

    def switch_session(self, session_id):
        """Switch to a specific session."""
        self.current_session_id = session_id
        return {"status": "success", "session_id": session_id}

    def get_current_session_id(self):
        """Return the current session ID."""
        return self.current_session_id

    def set_api_key(self, key, provider="openai"):
        self.keys[provider] = key
        env_var = f"{provider.upper()}_API_KEY"
        if provider == "grok": env_var = "XAI_API_KEY"
        os.environ[env_var] = key
        return f"{provider.title()} key saved"

    def set_provider(self, provider):
        if provider in self.keys:
            self.current_provider = provider
            return f"Provider switched to {provider}"
        return "Invalid provider"

    # ------------------------------------------------------------------
    # Local Model (Bonsai 8B)
    # ------------------------------------------------------------------

    def get_local_model_status(self, model_key: str = DEFAULT_MODEL) -> dict:
        """Return the current download + server status for a Bonsai model variant."""
        return bonsai.get_status(model_key)

    def get_bonsai_models(self) -> list:
        """Return the full model catalog with per-variant download state."""
        return bonsai.get_models()

    def download_bonsai(self, model_key: str = DEFAULT_MODEL) -> dict:
        """
        Start a background download of the chosen Bonsai variant.
        Progress is streamed back to the frontend via updateDownloadProgress().
        """
        def _worker():
            def _cb(pct: float, msg: str):
                if self.window:
                    safe_msg = json.dumps(msg)
                    self.window.evaluate_js(
                        f"updateDownloadProgress({pct:.2f}, {safe_msg})"
                    )
            bonsai.download_model(model_key=model_key, progress_cb=_cb)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return {"status": "started", "model_key": model_key}

    def cancel_download_bonsai(self, model_key: str = DEFAULT_MODEL) -> dict:
        """Cancel an in-progress download (removes the .partial file)."""
        bonsai.cancel_download(model_key)
        return {"status": "cancelled"}

    def start_bonsai(self, model_key: str = DEFAULT_MODEL, n_gpu_layers: int = 0) -> dict:
        """
        Start llama-server in the background.
        Calls onBonsaiServerReady(true/false) on the frontend when done.
        """
        def _worker():
            # n_gpu_layers=None triggers auto-detection in BonsaiManager._detect_gpu()
            # The n_gpu_layers param from the UI is used only if explicitly != -1
            effective_ngl = n_gpu_layers if n_gpu_layers >= 0 else None
            ok = bonsai.start_server(model_key=model_key, n_gpu_layers=effective_ngl)
            if self.window:
                self.window.evaluate_js(f"onBonsaiServerReady({'true' if ok else 'false'})")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return {"status": "starting"}

    def stop_bonsai(self) -> dict:
        """Stop the running llama-server process."""
        bonsai.stop_server()
        return {"status": "stopped"}

    def begin_auto_setup(self, model_key: str = DEFAULT_MODEL) -> dict:
        """
        Zero-click Bonsai setup — called automatically by the frontend.
        Chains: binary check → download (if needed) → server start.
        All progress is reported back via onBonsaiSetupProgress(phase, pct, msg).
        """
        def _report(phase: str, pct: float, msg: str):
            if self.window:
                self.window.evaluate_js(
                    f"onBonsaiSetupProgress({json.dumps(phase)}, {pct:.2f}, {json.dumps(msg)})"
                )

        def _worker():
            # ── 1. Binary check ──────────────────────────────────────────────
            if not bonsai._get_llama_server_path():
                _report('error', -1,
                    'llama-server not found — rebuild the exe with PyInstaller.')
                return

            # ── 2. Download model if not already on disk ─────────────────────
            if not bonsai.is_model_downloaded(model_key):
                def _dl_cb(pct: float, msg: str):
                    _report('downloading', pct, msg)

                ok = bonsai.download_model(model_key=model_key, progress_cb=_dl_cb)
                if not ok:
                    # Error message already sent by _dl_cb with the real exception
                    return

            # ── 3. Start server ──────────────────────────────────────────────
            _report('starting', 0, 'Loading model into memory…')

            # Feed live stdout lines from llama-server to the UI overlay so
            # users see real progress (layer counts, tensor loading, etc.)
            def _server_line_cb(line: str) -> None:
                # Only forward lines that look like meaningful load progress;
                # skip empty lines and very short ones
                stripped = line.strip()
                if len(stripped) > 5:
                    _report('starting', 0, stripped[:90])

            ok = bonsai.start_server(model_key=model_key, status_cb=_server_line_cb)
            if ok:
                _report('ready', 100, 'Bonsai is ready')
            else:
                _report('error', -1,
                    'Server failed to start — check ~/.myapp/llama_server.log')

        threading.Thread(target=_worker, daemon=True).start()
        return {"status": "started", "model_key": model_key}

    def set_model(self, model_id):
        self.current_model = model_id if model_id else None
        return f"Model set to {model_id if model_id else 'default'}"

    def toggle_multi_agent(self, enabled):
        self.multi_agent_mode = enabled
        return f"Multi-Agent mode: {'Enabled' if enabled else 'Disabled'}"

    def load_history(self):
        _, get_history, _, _ = _db_module()
        return get_history(self.current_session_id)

    def clear_rag_context(self):
        _agent_module().clear_knowledge_base()
        self.uploaded_filenames = []
        return "RAG context cleared"

    def upload_files(self, files_json):
        try:
            files_data = json.loads(files_json) if isinstance(files_json, str) else files_json
            processed_files = []
            for f in files_data:
                name = f["name"]
                content_b64 = f["content"]
                if "," in content_b64:
                    content_b64 = content_b64.split(",")[1]
                data = base64.b64decode(content_b64)
                processed_files.append({"name": name, "data": data})
                self.uploaded_filenames.append(name)
            
            # Use the global ingestion function from workspace_agent
            success = _agent_module().ingest_files(processed_files)
            if success:
                return {"status": "success", "files": list(set(self.uploaded_filenames))}
            return {"status": "error", "message": "Failed to ingest files"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def start_chat_stream(self, user_text, target_id=None):
        api_key = self.keys.get(self.current_provider)
        if not api_key:
            self.window.evaluate_js(f"receiveError('Please set your {self.current_provider.title()} API Key first.')")
            return
         
        if not target_id:
            save_msg, _, _, _ = _db_module()
            save_msg("user", user_text, self.current_session_id)

        thread = threading.Thread(target=self._run_logic, args=(user_text, target_id))
        thread.daemon = True
        thread.start()

    def _run_logic(self, user_text, target_id):
        if self.multi_agent_mode:
            self._run_multi_agent(user_text, target_id)
        else:
            self._run_single_agent(user_text, target_id)

    def _run_single_agent(self, user_text, target_id):
        try:
            provider = self.current_provider
            api_key = self.keys.get(provider)
            model_id = self.current_model
            
            agent = _agent_module().get_agent(
                provider=provider, 
                api_key=api_key, 
                model_id=model_id, 
                user_id="default_user",
                session_id=self.current_session_id
            )
            full_response = ""
            run_response = agent.run(user_text, stream=True)
            
            if target_id:
                self.window.evaluate_js(f"clearBubble('{target_id}')")

            for chunk in run_response:
                content = chunk.content if hasattr(chunk, 'content') else str(chunk)
                if content:
                    full_response += content
                    self.window.evaluate_js(f"receiveChunk({json.dumps(content)}, '{target_id or ''}')")

            save_msg, _, _, _ = _db_module()
            save_msg("bot", full_response, self.current_session_id)
            
            # GenUI: Detect tone from response
            tone = self._detect_tone(full_response)
            self.window.evaluate_js(f"streamComplete({json.dumps(tone)})")
        except Exception as e:
            self.window.evaluate_js(f"receiveError({json.dumps(str(e))})")
    
    def _detect_tone(self, text):
        """
        Simple keyword-based tone detection for GenUI.
        Returns: 'calm', 'excited', 'serious', or 'playful'
        """
        text_lower = text.lower()
        
        # Score each tone based on keyword presence
        scores = {
            'excited': 0,
            'playful': 0,
            'serious': 0,
            'calm': 0
        }
        
        # Excited indicators
        excited_words = ['!', 'amazing', 'awesome', 'fantastic', 'great', 'excellent', 
                        'wonderful', 'exciting', 'incredible', 'brilliant', 'love']
        for word in excited_words:
            scores['excited'] += text_lower.count(word)
        
        # Playful indicators
        playful_words = ['😊', '😄', '🎉', 'haha', 'fun', 'enjoy', 'play', 'joke', 
                        'funny', 'silly', 'cool', '👍', '✨']
        for word in playful_words:
            scores['playful'] += text_lower.count(word)
        
        # Serious indicators
        serious_words = ['important', 'critical', 'warning', 'caution', 'error',
                        'must', 'should', 'require', 'necessary', 'essential',
                        'security', 'risk', 'issue', 'problem', 'careful']
        for word in serious_words:
            scores['serious'] += text_lower.count(word)
        
        # Calm indicators (gentle, instructional)
        calm_words = ['here', 'let me', 'simply', 'just', 'easy', 'step', 'guide',
                     'help', 'explain', 'understand', 'note', 'consider']
        for word in calm_words:
            scores['calm'] += text_lower.count(word)
        
        # Return the highest scoring tone, default to 'calm'
        if max(scores.values()) == 0:
            return 'calm'
        
        return max(scores, key=scores.get)

    def _run_multi_agent(self, user_text, target_id):
        # Placeholder for multi-agent support if needed, but keeping it simple for now
        self._run_single_agent(user_text, target_id)

