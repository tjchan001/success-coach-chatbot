"""Cross-encoder reranker helper for ranking retrieved catalog chunks.

Architectural Intent:
    Separating the reranker from the main chat route keeps scoring logic
    independently testable and swappable.  The current implementation uses
    a simple TF-IDF-style keyword overlap score so the pipeline runs with
    zero external model dependencies during the MVP phase.  A cross-encoder
    model (e.g. ``cross-encoder/ms-marco-MiniLM-L-6-v2``) can be dropped
    in later by swapping ``_score_chunk`` without touching any other file.

Security Rationale:
    - No network calls are made here; the reranker operates entirely on
      in-process strings, eliminating SSRF and data-exfiltration risks.
    - Query and chunk strings are never passed to a shell command.
"""

from __future__ import annotations

import math
import re
from collections import Counter


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rerank_chunks(query: str, chunks: list[str], top_k: int = 5) -> list[str]:
    """Return the most relevant chunks ordered by descending relevance score.

    Architectural Intent:
        Provides a deterministic, zero-dependency reranker for the MVP so
        the full RAG pipeline can be validated before any ML model is loaded.
        The BM25-inspired scoring is a faithful proxy of production reranker
        behaviour for simple factual queries against the course catalog.

    Args:
        query: The user's search query or chat message.
        chunks: Candidate text chunks retrieved from the vector store.
        top_k: Maximum number of chunks to return.  Defaults to 5.

    Returns:
        Up to *top_k* chunks ordered from most to least relevant.
        Returns an empty list if *chunks* is empty.
    """
    if not chunks:
        return []

    scored: list[tuple[float, str]] = [
        (_score_chunk(query=query, chunk=chunk), chunk) for chunk in chunks
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    """Lowercase and split *text* into word tokens, stripping punctuation.

    Args:
        text: Any plain-text string.

    Returns:
        A list of lowercase word tokens.
    """
    return re.findall(r"[a-z0-9]+", text.lower())


def _score_chunk(query: str, chunk: str) -> float:
    """Compute a BM25-inspired relevance score for *chunk* given *query*.

    The score is the sum of term-frequency / (term-frequency + k1) weighted
    by the inverse document frequency of each query term within the chunk.
    Constants are set to BM25 defaults (k1=1.5, b=0 — no length norm needed
    for short catalog snippets).

    Architectural Intent:
        Deterministic scoring makes unit tests stable across Python versions
        without requiring a heavy ML dependency.

    Args:
        query: The user's search query.
        chunk: A catalog text chunk to score.

    Returns:
        A non-negative float; higher values indicate greater relevance.
    """
    k1: float = 1.5
    query_tokens = _tokenise(query)
    chunk_tokens = _tokenise(chunk)

    if not query_tokens or not chunk_tokens:
        return 0.0

    chunk_freq: Counter[str] = Counter(chunk_tokens)
    chunk_len = len(chunk_tokens)

    score: float = 0.0
    for term in set(query_tokens):
        tf = chunk_freq.get(term, 0)
        if tf == 0:
            continue
        # Simplified IDF: log((N+1) / df) where N=1 (single document)
        idf = math.log(2.0 / 1.0)
        tf_norm = (tf * (k1 + 1.0)) / (tf + k1)
        score += idf * tf_norm / chunk_len

    return score
