# utils/embeddings.py
"""
Enhanced embedding abstraction supporting multiple backends:
1. Local SentenceTransformers (original - nomic-ai/nomic-embed-text-v1.5)
2. LangChain HuggingFace (new - sentence-transformers/all-MiniLM-L6-v2)
3. OpenAI (original fallback)

Usage:
    get_embeddings(texts, backend="local")      # SentenceTransformers (nomic)
    get_embeddings(texts, backend="nomic")      # Same as "local"
    get_embeddings(texts, backend="langchain")  # LangChain HuggingFace
    get_embeddings(texts, backend="openai")     # OpenAI API

Returns: List[List[float]] (one vector per input text)
"""

import os
from typing import List
import math
from dotenv import load_dotenv
load_dotenv()

# OpenAI client (lazy initialization - will be created when needed)
_openai_client = None

def _get_openai_client():
    """Lazy initialization of OpenAI client"""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(base_url="https://api.router.tetrate.ai/v1")
    return _openai_client

# Local model defaults
LOCAL_EMBEDDING_MODEL = os.environ.get("LOCAL_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
LANGCHAIN_EMBEDDING_MODEL = os.environ.get("LANGCHAIN_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

# Try to import sentence-transformers
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except Exception:
    SentenceTransformer = None
    np = None
    SENTENCE_TRANSFORMERS_AVAILABLE = False

# Try to import langchain embeddings
try:
    from langchain_huggingface import HuggingFaceEmbeddings
    LANGCHAIN_AVAILABLE = True
except Exception:
    HuggingFaceEmbeddings = None
    LANGCHAIN_AVAILABLE = False

# Cache for models
_MODEL_CACHE = {
    "local": {"model": None, "device": None, "name": None},
    "langchain": {"model": None, "name": None}
}


def _get_local_model(model_name: str = LOCAL_EMBEDDING_MODEL, device: str = None):
    """
    Load and cache a SentenceTransformer model.
    device: 'cpu' or 'cuda' or None (auto-detect)
    """
    if not SENTENCE_TRANSFORMERS_AVAILABLE:
        raise RuntimeError(
            "sentence-transformers not installed. Install with: pip install sentence-transformers torch"
        )

    # auto-detect device if not provided
    if device is None:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"

    cache = _MODEL_CACHE["local"]
    if cache["model"] is None or cache["name"] != model_name or cache["device"] != device:
        model = SentenceTransformer(model_name, trust_remote_code=True)
        cache["model"] = model
        cache["device"] = device
        cache["name"] = model_name
    return cache["model"]


def _get_langchain_model(model_name: str = LANGCHAIN_EMBEDDING_MODEL):
    """Load and cache a LangChain HuggingFaceEmbeddings model."""
    if not LANGCHAIN_AVAILABLE:
        raise RuntimeError(
            "langchain-huggingface not installed. Install with: pip install langchain-huggingface"
        )

    cache = _MODEL_CACHE["langchain"]
    if cache["model"] is None or cache["name"] != model_name:
        model = HuggingFaceEmbeddings(model_name=model_name)
        cache["model"] = model
        cache["name"] = model_name
    return cache["model"]


def _embed_with_sentence_transformers(texts: List[str], model_name: str = LOCAL_EMBEDDING_MODEL, batch_size: int = 64):
    """
    Compute embeddings using SentenceTransformer model in batches.
    Returns a list of lists (floats).
    """
    model = _get_local_model(model_name=model_name)
    embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        emb = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
        if isinstance(emb, (list, tuple)):
            for row in emb:
                embeddings.append(row.tolist())
        else:
            for row in emb:
                embeddings.append(row.tolist())
    return embeddings


def _embed_with_langchain(texts: List[str], model_name: str = LANGCHAIN_EMBEDDING_MODEL):
    """Compute embeddings using LangChain HuggingFaceEmbeddings."""
    model = _get_langchain_model(model_name=model_name)
    # LangChain provides embed_documents for batch processing
    embeddings = model.embed_documents(texts)
    return embeddings


def _embed_with_openai(texts: List[str], model: str = OPENAI_EMBED_MODEL):
    """
    Use OpenAI embeddings API. Accepts list of texts (batched).
    Returns tuple of (embeddings, token_count).
    """
    client = _get_openai_client()
    resp = client.embeddings.create(model=model, input=texts)
    
    # Extract embeddings
    out = []
    for item in resp.data:
        emb = getattr(item, "embedding", None) or item.get("embedding") if isinstance(item, dict) else None
        if emb is None:
            emb = item.embedding if hasattr(item, "embedding") else None
        if emb is None:
            raise RuntimeError("Unexpected OpenAI embeddings response shape")
        out.append(list(emb))
    
    # Extract token usage
    token_count = 0
    if hasattr(resp, 'usage') and resp.usage:
        token_count = resp.usage.total_tokens
    
    return out, token_count

def get_embeddings(texts: List[str], backend: str = "local", **kwargs) -> tuple:
    """
    Compute embeddings for texts.

    backend options:
      - "local" / "sentence-transformers" / "st" / "nomic" -> SentenceTransformers (nomic model)
      - "langchain" / "lc" / "huggingface" / "hf" -> LangChain HuggingFaceEmbeddings
      - "openai" -> OpenAI embeddings

    kwargs:
      - model: overrides model name for the chosen backend
      - batch_size (int): for local embedding

    Returns:
      For OpenAI: (embeddings, token_count)
      For others: (embeddings, 0)
    """
    if not texts:
        return [], 0

    backend = (backend or "local").lower()
    
    if backend in ("local", "sentence-transformers", "st", "nomic"):
        model_name = kwargs.get("model", LOCAL_EMBEDDING_MODEL) 
        batch_size = int(kwargs.get("batch_size", 64))
        embeddings = _embed_with_sentence_transformers(texts, model_name=model_name, batch_size=batch_size)
        return embeddings, 0  # No token tracking for local models

    elif backend in ("langchain", "lc", "huggingface", "hf"):
        model_name = kwargs.get("model", LANGCHAIN_EMBEDDING_MODEL)
        embeddings = _embed_with_langchain(texts, model_name=model_name)
        return embeddings, 0  # No token tracking for local models

    elif backend == "openai":
        model = kwargs.get("model", OPENAI_EMBED_MODEL)
        return _embed_with_openai(texts, model=model)  # Returns (embeddings, tokens)

    else:
        raise ValueError(f"Unknown embedding backend: {backend}")

def embed_query(query: str, backend: str = "local", **kwargs) -> List[float]:
    """
    Compute embedding for a single query text.
    Useful for LangChain compatibility where embed_query is separate from embed_documents.
    """
    backend = (backend or "local").lower()
    
    if backend in ("langchain", "lc", "huggingface", "hf"):
        model_name = kwargs.get("model", LANGCHAIN_EMBEDDING_MODEL)
        model = _get_langchain_model(model_name=model_name)
        return model.embed_query(query)
    else:
        # For other backends, just use get_embeddings with single text
        embeddings, _ = get_embeddings([query], backend=backend, **kwargs)  # Unpack tuple
        return embeddings[0]  # Return first embedding


# ============= FAISS-COMPATIBLE CALLABLE EMBEDDING CLASS =============

class EmbeddingFunction:
    """
    Callable embedding function for FAISS compatibility.
    This class is callable and can be used directly with FAISS vector stores.
    """
    
    def __init__(self, backend="nomic", **kwargs):
        """
        Initialize embedding function.
        
        Args:
            backend: Embedding backend to use ("nomic", "langchain", "openai")
            **kwargs: Additional arguments to pass to the embedding backend
        """
        self.backend = backend
        self.kwargs = kwargs
    
    def __call__(self, text):
        """
        FAISS calls this directly with a single text string.
        This makes the class callable.
        """
        if isinstance(text, str):
            return embed_query(text, backend=self.backend, **self.kwargs)
        elif isinstance(text, list):
            # Handle list of texts
            embeddings, _ = get_embeddings(text, backend=self.backend, **self.kwargs)
            return embeddings[0] if len(embeddings) == 1 else embeddings
        else:
            raise ValueError(f"Expected str or list, got {type(text)}")
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        For batch embedding (LangChain compatibility).
        Used by LangChain's add_documents method.
        """
        embeddings, _ = get_embeddings(texts, backend=self.backend, **self.kwargs)
        return embeddings
    
    def embed_query(self, text: str) -> List[float]:
        """
        For single query embedding (LangChain compatibility).
        Used by LangChain's similarity_search method.
        """
        return embed_query(text, backend=self.backend, **self.kwargs)


def get_embedding_function(backend="nomic", **kwargs):
    """
    Factory function to create a callable embedding function.
    Use this when you need a FAISS-compatible embedding function.
    
    Args:
        backend: Embedding backend to use ("nomic", "langchain", "openai")
        **kwargs: Additional arguments to pass to the embedding backend
    
    Returns:
        EmbeddingFunction: A callable embedding function
    
    Example:
        embeddings = get_embedding_function(backend="nomic")
        vector_store = FAISS.from_documents(documents, embeddings)
    """
    return EmbeddingFunction(backend=backend, **kwargs)