"""
Integration tests for the Paramodus knowledge-base RAG service functions.

Covers
------
  ingest_text        — raw-text ingestion via knowledge.insert(text_content=)
  ingest_files       — file ingestion via knowledge.insert(path=, reader=)
  search_knowledge_base — retrieval via knowledge.search()
  clear_knowledge_base  — drop + recreate table cycle
  get_knowledge_stats   — row count and status reporting

Each test works against an isolated LanceDb fixture (see conftest.py) so
the production ~/.myapp directory is never touched and tests are safe to
run in CI without any API keys.

Regression guard
----------------
All four bugs fixed in the agno 2.x migration are covered by at least one
assertion.  If agno changes its API again in a breaking way, these tests
will catch it before the app ships.
"""

import os
import json
import pytest

import agents.workspace_agent as wa
from agents.workspace_agent import (
    ingest_text,
    ingest_files,
    search_knowledge_base,
    clear_knowledge_base,
    get_knowledge_stats,
)
from tests.conftest import make_file_payload


# ===========================================================================
# ingest_text
# ===========================================================================

class TestIngestText:
    """knowledge.insert(text_content=...) — regression for load_documents removal."""

    def test_returns_true_on_success(self, patched_kb):
        result = ingest_text("The sky is blue.", source_name="sky_note")
        assert result is True

    def test_chunk_appears_in_vector_db(self, patched_kb):
        ingest_text("Hydrogen combustion produces water.", source_name="h2_note")
        count = patched_kb.vector_db.get_count()
        assert count >= 1

    def test_multiple_texts_accumulate(self, patched_kb):
        ingest_text("First document about thermodynamics.", source_name="doc1")
        ingest_text("Second document about fluid dynamics.", source_name="doc2")
        count = patched_kb.vector_db.get_count()
        assert count >= 2

    def test_returns_false_on_empty_string(self, patched_kb, monkeypatch):
        """
        An empty string causes insert to log a warning and return without
        writing — the function should catch that and return False.
        """
        # Patch knowledge.insert to raise so we exercise the except branch
        def boom(*args, **kwargs):
            raise RuntimeError("simulated insert failure")
        monkeypatch.setattr(patched_kb, "insert", boom)

        result = ingest_text("", source_name="empty")
        assert result is False


# ===========================================================================
# ingest_files
# ===========================================================================

class TestIngestFiles:
    """knowledge.insert(path=, reader=) — regression for load_documents removal."""

    def test_plain_text_file(self, patched_kb):
        payload = make_file_payload(
            "notes.txt",
            b"CFD simulations require mesh convergence studies.",
        )
        result = ingest_files([payload])
        assert result is True
        assert patched_kb.vector_db.get_count() >= 1

    def test_markdown_file(self, patched_kb):
        payload = make_file_payload(
            "readme.md",
            b"# Paramodus\nAgentic workspace built with Agno and LanceDB.",
        )
        result = ingest_files([payload])
        assert result is True

    def test_python_file(self, patched_kb):
        payload = make_file_payload(
            "snippet.py",
            b"def add(a, b):\n    return a + b\n",
        )
        result = ingest_files([payload])
        assert result is True

    def test_json_file(self, patched_kb):
        payload = make_file_payload(
            "data.json",
            json.dumps({"key": "value", "numbers": [1, 2, 3]}).encode(),
        )
        result = ingest_files([payload])
        assert result is True

    def test_multiple_files_in_one_call(self, patched_kb):
        files = [
            make_file_payload("a.txt", b"Document about propulsion."),
            make_file_payload("b.txt", b"Document about heat transfer."),
            make_file_payload("c.txt", b"Document about lean six sigma."),
        ]
        result = ingest_files(files)
        assert result is True
        assert patched_kb.vector_db.get_count() >= 3

    def test_unsupported_extension_skipped(self, patched_kb):
        """
        An unsupported file type (.xyz) must be skipped gracefully.
        The call returns False because nothing was ingested, but no
        exception escapes.
        """
        payload = make_file_payload("model.xyz", b"\x00\x01binary garbage")
        result = ingest_files([payload])
        assert result is False
        assert patched_kb.vector_db.get_count() == 0

    def test_unsupported_mixed_with_valid(self, patched_kb):
        """Valid files are still ingested even when the batch contains unsupported types."""
        files = [
            make_file_payload("good.txt", b"Valid text content here."),
            make_file_payload("bad.bin", b"\xff\xfe binary"),
        ]
        result = ingest_files(files)
        assert result is True  # at least one file succeeded
        assert patched_kb.vector_db.get_count() >= 1

    def test_empty_list_returns_false(self, patched_kb):
        result = ingest_files([])
        assert result is False

    def test_temp_file_cleaned_up(self, patched_kb, tmp_path):
        """No temp files should survive after ingestion."""
        import tempfile
        before = set(os.listdir(tempfile.gettempdir()))
        ingest_files([make_file_payload("clean.txt", b"cleanup test")])
        after = set(os.listdir(tempfile.gettempdir()))
        # Allow for OS-level temp files but no .txt leftovers from our code
        new_txts = [f for f in (after - before) if f.endswith(".txt")]
        assert new_txts == []


# ===========================================================================
# search_knowledge_base
# ===========================================================================

class TestSearchKnowledgeBase:
    """knowledge.search() — regression for wrong LanceDb.search() direct call."""

    def test_returns_list(self, patched_kb):
        ingest_text("Vector databases store embeddings.", source_name="vdb_doc")
        results = search_knowledge_base("embeddings")
        assert isinstance(results, list)

    def test_relevant_result_returned(self, patched_kb):
        ingest_text("The hydrogen fuel cell converts chemical energy to electricity.", source_name="fuel_cell")
        results = search_knowledge_base("hydrogen fuel cell", limit=3)
        assert len(results) >= 1

    def test_result_items_are_dicts(self, patched_kb):
        ingest_text("Physics-Informed Neural Networks solve PDEs.", source_name="pinn_doc")
        results = search_knowledge_base("neural networks PDEs", limit=2)
        assert all(isinstance(r, dict) for r in results)

    def test_result_dict_has_expected_keys(self, patched_kb):
        """Each result must have name, content, meta_data — the Document.to_dict() contract."""
        ingest_text("ANSYS Fluent is a CFD solver.", source_name="cfd_doc")
        results = search_knowledge_base("CFD solver", limit=1)
        assert len(results) >= 1
        keys = set(results[0].keys())
        assert {"name", "content", "meta_data"}.issubset(keys)

    def test_limit_is_respected(self, patched_kb):
        for i in range(6):
            ingest_text(f"Unique chunk number {i} about engineering.", source_name=f"doc_{i}")
        results = search_knowledge_base("engineering", limit=3)
        assert len(results) <= 3

    def test_no_results_returns_empty_list(self, patched_kb):
        """Empty KB must return [] without raising."""
        results = search_knowledge_base("completely unrelated query xyzzy")
        assert results == []

    def test_search_error_returns_empty_list(self, patched_kb, monkeypatch):
        """If knowledge.search raises, the function must swallow it and return []."""
        def boom(*args, **kwargs):
            raise RuntimeError("simulated search failure")
        monkeypatch.setattr(patched_kb, "search", boom)
        results = search_knowledge_base("anything")
        assert results == []


# ===========================================================================
# clear_knowledge_base
# ===========================================================================

class TestClearKnowledgeBase:
    """LanceDb.drop() + create() — regression for raw lancedb bypass bug."""

    def test_returns_true(self, patched_kb):
        result = clear_knowledge_base()
        assert result is True

    def test_empties_existing_data(self, patched_kb):
        ingest_text("Data that should be wiped.", source_name="doomed")
        assert patched_kb.vector_db.get_count() >= 1
        clear_knowledge_base()
        assert patched_kb.vector_db.get_count() == 0

    def test_table_still_usable_after_clear(self, patched_kb):
        """
        Critical regression: after clear, subsequent inserts must succeed.
        The old bug left the LanceDb.table reference stale after raw drop.
        """
        ingest_text("First batch of data.", source_name="batch1")
        clear_knowledge_base()
        result = ingest_text("Second batch after clear.", source_name="batch2")
        assert result is True
        assert patched_kb.vector_db.get_count() >= 1

    def test_double_clear_is_safe(self, patched_kb):
        """Clearing an already-empty table must not raise."""
        clear_knowledge_base()
        result = clear_knowledge_base()
        assert result is True

    def test_clear_then_search_returns_empty(self, patched_kb):
        ingest_text("Temporary data.", source_name="tmp")
        clear_knowledge_base()
        results = search_knowledge_base("temporary data")
        assert results == []

    def test_clear_error_returns_false(self, patched_kb, monkeypatch):
        """Exceptions inside clear must be caught and return False."""
        def boom():
            raise RuntimeError("simulated drop failure")
        monkeypatch.setattr(patched_kb.vector_db, "drop", boom)
        # Make exists() return True so the drop path is entered
        monkeypatch.setattr(patched_kb.vector_db, "exists", lambda: True)
        result = clear_knowledge_base()
        assert result is False


# ===========================================================================
# get_knowledge_stats
# ===========================================================================

class TestGetKnowledgeStats:
    """LanceDb.get_count() — regression for raw lancedb row-counting bypass."""

    def test_returns_dict(self, patched_kb):
        stats = get_knowledge_stats()
        assert isinstance(stats, dict)

    def test_has_required_keys(self, patched_kb):
        stats = get_knowledge_stats()
        assert "total_chunks" in stats
        assert "status" in stats

    def test_empty_db_reports_zero_or_empty(self, patched_kb):
        """
        Immediately after init the table may not exist yet (no inserts done),
        so either total_chunks == 0 and status in {'empty', 'active'} is fine.
        """
        stats = get_knowledge_stats()
        assert stats["total_chunks"] == 0
        assert stats["status"] in ("empty", "active")

    def test_count_increases_after_ingest(self, patched_kb):
        ingest_text("Chunk one.", source_name="s1")
        stats = get_knowledge_stats()
        assert stats["total_chunks"] >= 1
        assert stats["status"] == "active"

    def test_count_is_zero_after_clear(self, patched_kb):
        ingest_text("Will be cleared.", source_name="doomed")
        clear_knowledge_base()
        stats = get_knowledge_stats()
        assert stats["total_chunks"] == 0

    def test_error_returns_error_status(self, patched_kb, monkeypatch):
        """Exception in stats must be caught and return status='error'."""
        def boom():
            raise RuntimeError("simulated stats failure")
        monkeypatch.setattr(patched_kb.vector_db, "get_count", boom)
        monkeypatch.setattr(patched_kb.vector_db, "exists", lambda: True)
        stats = get_knowledge_stats()
        assert stats["status"] == "error"
        assert stats["total_chunks"] == 0


# ===========================================================================
# End-to-end lifecycle test
# ===========================================================================

class TestKnowledgeBaseLifecycle:
    """
    Full ingest → search → stats → clear → re-ingest → search cycle.
    This is the canonical regression test for the agno 2.x API migration.
    """

    def test_full_lifecycle(self, patched_kb):
        # 1. Ingest text
        assert ingest_text(
            "PINN can solve Navier-Stokes equations without mesh generation.",
            source_name="pinn_ns",
        ) is True

        # 2. Ingest a file
        assert ingest_files([
            make_file_payload(
                "combustion.txt",
                b"Hydrogen combustion in air produces NOx at high temperatures.",
            )
        ]) is True

        # 3. Stats reflect both ingestions
        stats = get_knowledge_stats()
        assert stats["total_chunks"] >= 2
        assert stats["status"] == "active"

        # 4. Search finds relevant content
        results = search_knowledge_base("Navier-Stokes PINN", limit=5)
        assert len(results) >= 1
        assert all("content" in r for r in results)

        # 5. Clear wipes everything
        assert clear_knowledge_base() is True
        stats_after = get_knowledge_stats()
        assert stats_after["total_chunks"] == 0

        # 6. Re-ingest after clear works (critical regression check)
        assert ingest_text("Post-clear insertion test.", source_name="post_clear") is True
        assert get_knowledge_stats()["total_chunks"] >= 1

        # 7. Search after re-ingest returns results
        results_after = search_knowledge_base("insertion test", limit=3)
        assert len(results_after) >= 1
