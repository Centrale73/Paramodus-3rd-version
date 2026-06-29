from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.models.anthropic import Claude
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.openrouter import OpenRouter
from agno.models.perplexity import Perplexity
from agno.models.xai import xAI
from agno.db.sqlite import SqliteDb
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.lancedb import LanceDb
from agno.knowledge.embedder.fastembed import FastEmbedEmbedder
from agno.knowledge.reader.pdf_reader import PDFReader
from agno.knowledge.reader.csv_reader import CSVReader
from agno.knowledge.reader.text_reader import TextReader
from agno.knowledge.chunking.recursive import RecursiveChunking
from agno.knowledge.document.base import Document
from local_model.manager import bonsai, SERVER_HOST, SERVER_PORT

import os
import tempfile
from typing import List, Dict, Optional

# Use absolute path for .exe persistence
app_data = os.path.join(os.path.expanduser("~"), ".myapp")
os.makedirs(app_data, exist_ok=True)

# Local SQLite for memory/sessions (FREE)
db = SqliteDb(db_file=os.path.join(app_data, "memory.db"))

# Local LanceDB for knowledge (FREE)
LANCE_URI = os.path.join(app_data, "lancedb")

# Shared chunking strategy for readers
DEFAULT_CHUNKER = RecursiveChunking(chunk_size=1000, overlap=150)

# ---------------------------------------------------------------------------
# Lazy Knowledge Base initialization
# ---------------------------------------------------------------------------
# We defer construction until first use so that FastEmbed does NOT download
# its ~25 MB model weights synchronously at import time.  This means the
# pywebview window renders immediately instead of hanging for 30+ seconds on
# the first run while the model is pulled from the internet.
# ---------------------------------------------------------------------------
_knowledge: Optional[Knowledge] = None


def _get_knowledge() -> Knowledge:
    """Return the module-level Knowledge singleton, creating it on first call."""
    global _knowledge
    if _knowledge is None:
        _knowledge = Knowledge(
            vector_db=LanceDb(
                table_name="user_documents",
                uri=LANCE_URI,
                embedder=FastEmbedEmbedder(
                    id="BAAI/bge-small-en-v1.5",
                    dimensions=384,
                ),
            ),
        )
    return _knowledge

# Use a FIXED user_id for single-user environment
USER_ID = "local_user"

# ============================================================================
# RAG SERVICE FUNCTIONS (LOCAL & FREE)
# ============================================================================

def ingest_files(files: List[Dict[str, any]]) -> bool:
    """
    Ingest files into the LOCAL vector database (FREE).
    """
    ingested_count = 0

    for file_info in files:
        name = file_info["name"]
        data = file_info["data"]

        # Save to temp file so Agno readers can access it by path
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=os.path.splitext(name)[1]
        ) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        try:
            # Select appropriate reader based on file extension
            if name.lower().endswith(".pdf"):
                reader = PDFReader(chunking_strategy=DEFAULT_CHUNKER)
            elif name.lower().endswith(".csv"):
                reader = CSVReader(chunking_strategy=DEFAULT_CHUNKER)
            elif name.lower().endswith((".txt", ".md", ".py", ".js", ".json")):
                reader = TextReader(chunking_strategy=DEFAULT_CHUNKER)
            else:
                print(f"Unsupported file type: {name}")
                continue

            # agno 2.x API: insert by path, passing reader and metadata
            _get_knowledge().insert(
                path=tmp_path,
                name=name,
                reader=reader,
                metadata={"filename": name},
                upsert=True,
            )
            ingested_count += 1

        except Exception as e:
            print(f"Error processing {name}: {e}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    if ingested_count > 0:
        print(f"✓ Successfully ingested {ingested_count} file(s) into the knowledge base")
        return True

    print("⚠ No documents were ingested")
    return False


def ingest_text(text: str, source_name: str = "text_input") -> bool:
    """
    Ingest raw text directly into the LOCAL vector database (FREE).
    """
    try:
        # agno 2.x API: insert text_content directly — no temp file needed
        _get_knowledge().insert(
            text_content=text,
            name=source_name,
            metadata={"source": source_name},
            upsert=True,
        )
        print(f"✓ Successfully ingested text from {source_name}")
        return True
    except Exception as e:
        print(f"Error ingesting text: {e}")
        return False


def clear_knowledge_base() -> bool:
    """
    Clear all documents from the LOCAL knowledge base (FREE).
    """
    try:
        # agno 2.x API: drop via the LanceDb wrapper so the internal
        # table reference stays consistent, then recreate the empty table.
        if _get_knowledge().vector_db.exists():
            _get_knowledge().vector_db.drop()
            print("✓ Knowledge base cleared")

        # Recreate the empty table so subsequent inserts work without restart
        _get_knowledge().vector_db.create()
        return True

    except Exception as e:
        print(f"Error clearing knowledge base: {e}")
        return False


def search_knowledge_base(query: str, limit: int = 5) -> List[Dict]:
    """
    Search the LOCAL knowledge base (FREE).
    """
    try:
        # agno 2.x API: search via Knowledge, not directly on LanceDb
        results: List[Document] = _get_knowledge().search(query=query, max_results=limit)
        return [doc.to_dict() for doc in results]
    except Exception as e:
        print(f"Error searching knowledge base: {e}")
        return []


def get_knowledge_stats() -> Dict:
    """
    Get statistics about the LOCAL knowledge base.
    """
    try:
        # agno 2.x API: use LanceDb wrapper methods
        if _get_knowledge().vector_db.exists():
            count = _get_knowledge().vector_db.get_count()
            return {
                "total_chunks": count,
                "status": "active"
            }
        else:
            return {
                "total_chunks": 0,
                "status": "empty"
            }
    except Exception as e:
        print(f"Error getting knowledge stats: {e}")
        return {"total_chunks": 0, "status": "error"}


# ============================================================================
# MODEL & AGENT CONFIGURATION
# ============================================================================

# Default models for each provider
DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-5-20250929",
    "gemini": "gemini-2.0-flash-001",
    "groq": "llama-3.3-70b-versatile",
    "grok": "grok-3",
    "openrouter": "openai/gpt-4o-mini",
    "perplexity": "sonar-pro",
    # Bonsai 8B runs locally via llama-server — no API key needed
    # llama-server accepts any model name but "local-model" is the
    # conventional placeholder that works across all llama.cpp versions
    # without triggering a 404 model-not-found response.
    "bonsai": "local-model",
}


def get_model(provider: str, api_key: str, model_id: Optional[str] = None):
    """
    Factory function to return the correct Agno model instance.
    """
    # Use default model if none specified
    if not model_id:
        model_id = DEFAULT_MODELS.get(provider)
    
    if provider == "openai":
        return OpenAIChat(id=model_id, api_key=api_key)
    elif provider == "anthropic":
        return Claude(id=model_id, api_key=api_key)
    elif provider == "gemini":
        return Gemini(id=model_id, api_key=api_key)
    elif provider == "groq":
        return Groq(id=model_id, api_key=api_key)
    elif provider == "grok":
        return xAI(id=model_id, api_key=api_key)
    elif provider == "openrouter":
        return OpenRouter(id=model_id, api_key=api_key)
    elif provider == "perplexity":
        return Perplexity(id=model_id, api_key=api_key)
    elif provider == "bonsai":
        # Bonsai 8B runs locally via llama-server on localhost:8080.
        # We use OpenAIChat with a custom base_url — the server exposes an
        # OpenAI-compatible /v1 endpoint.  Any non-empty string works as api_key.
        base_url = f"http://{SERVER_HOST}:{SERVER_PORT}/v1"
        return OpenAIChat(
            id=model_id or DEFAULT_MODELS["bonsai"],
            api_key="local",
            base_url=base_url,
        )
    else:
        # Fallback to OpenAI
        print(f"Warning: Unknown provider '{provider}', falling back to OpenAI")
        return OpenAIChat(id=model_id or "gpt-4o", api_key=api_key)


def get_agent(
    provider: str = "openai", 
    api_key: Optional[str] = None, 
    model_id: Optional[str] = None, 
    user_id: str = USER_ID,
    session_id: str = "workspace_main_session",
    enable_rag: bool = True
):
    """
    Returns a configured Agno Agent with the specified provider and key.
    """
    agent_config = {
        "model": get_model(provider, api_key, model_id),
        "session_id": session_id,
        "markdown": True,
        "description": "You are a professional workspace assistant with access to uploaded documents.",
        "db": db,
        "enable_user_memories": True,
        "add_memories_to_context": True,
        "add_history_to_context": True,
        "num_history_runs": 5,
    }
    
    # Add LOCAL & FREE RAG capabilities
    if enable_rag:
        agent_config["knowledge"] = _get_knowledge()
        agent_config["search_knowledge"] = True
    
    return Agent(**agent_config)

