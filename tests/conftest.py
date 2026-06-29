"""
Shared fixtures for Paramodus integration tests.

Strategy
--------
The production module (agents.workspace_agent) creates a module-level
Knowledge singleton at import time, pointing at ~/.myapp/lancedb.  We
cannot let tests touch that path, so every test that exercises the RAG
service functions receives an *isolated* Knowledge instance backed by a
throwaway temp directory, and the module-level singleton is monkey-patched
for the duration of the test.

This means:
  - No cross-test pollution (each test gets a fresh LanceDb table).
  - No ~/.myapp writes during CI.
  - Tests run fully offline (FastEmbed model weights are cached after the
    first download).

Dependencies required to import workspace_agent
------------------------------------------------
All packages listed in requirements.txt must be installed, including the
provider SDKs (openai, groq, anthropic, google-genai) because agno eagerly
imports them at package load time.  The CI setup step must install:

    pip install -r requirements.txt pytest
"""

import os
import pytest

from agno.knowledge.knowledge import Knowledge
from agno.vectordb.lancedb import LanceDb
from agno.knowledge.embedder.fastembed import FastEmbedEmbedder


# ---------------------------------------------------------------------------
# Isolated Knowledge fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def kb(tmp_path):
    """
    A fresh Knowledge + LanceDb instance in a throwaway temp directory.
    Yielded to each test; the temp directory is cleaned up automatically
    by pytest after the test completes.
    """
    lance_uri = str(tmp_path / "lancedb")
    instance = Knowledge(
        vector_db=LanceDb(
            table_name="test_documents",
            uri=lance_uri,
            embedder=FastEmbedEmbedder(
                id="BAAI/bge-small-en-v1.5",
                dimensions=384,
            ),
        ),
    )
    yield instance


# ---------------------------------------------------------------------------
# Module-level singleton patcher
# ---------------------------------------------------------------------------

@pytest.fixture()
def patched_kb(kb, monkeypatch):
    """
    Patches the module-level `knowledge` singleton in agents.workspace_agent
    with the isolated `kb` fixture instance, then yields it.

    All service functions (ingest_files, ingest_text, search_knowledge_base,
    clear_knowledge_base, get_knowledge_stats) read `knowledge` from their
    own module globals, so patching it here is sufficient.
    """
    import agents.workspace_agent as wa
    monkeypatch.setattr(wa, "knowledge", kb)
    yield kb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_file_payload(name: str, content: bytes) -> dict:
    """Build a file-info dict matching the format expected by ingest_files."""
    return {"name": name, "data": content}
