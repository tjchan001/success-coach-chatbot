"""FastAPI chat endpoint for the Dallas College chatbot.

Architectural Intent:
    This module is the HTTP boundary between the public widget and the
    internal retrieval / reranking pipeline.  It intentionally contains
    no business logic — all heavy lifting is delegated to helper modules
    so this file remains thin enough to be replaced or versioned easily.

Security Rationale:
    - All inbound payloads are validated by Pydantic before they touch any
      application logic, preventing injection via malformed JSON.
    - CORS is restricted to the official Dallas College widget origin in
      production; the wildcard permissive policy is limited to the local
      development environment.
    - Session IDs are opaque tokens — never re-used as SQL parameters or
      file paths.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from api.helper_reranker import rerank_chunks

# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DC Chatbot API",
    description="Retrieval-augmented chatbot for Dallas College course information.",
    version="0.1.0",
)

_CORS_ORIGINS: list[str] = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization"],
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Validated inbound chat message from the widget.

    Args:
        message: The user's question or utterance.  Min 1, max 2000 chars.
        session_id: Optional opaque session token for multi-turn context.
    """

    model_config = ConfigDict(frozen=True)

    message: Annotated[str, Field(min_length=1, max_length=2000)] = Field(
        ...,
        description="The user's question or utterance.",
    )
    session_id: str | None = Field(
        None,
        description="Optional opaque session token for multi-turn context.",
    )


class ChatResponse(BaseModel):
    """Validated outbound chat reply sent to the widget.

    Args:
        reply: The assistant's answer in plain text or light Markdown.
        sources: Zero or more catalog URLs cited in the reply.
    """

    model_config = ConfigDict(frozen=True)

    reply: str = Field(..., description="The assistant's answer.")
    sources: list[str] = Field(
        default_factory=list,
        description="Catalog URLs cited in the reply.",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check() -> dict[str, str]:
    """Return a liveness probe response.

    Returns:
        A dict with a single ``status`` key set to ``"ok"``.
    """
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse, status_code=status.HTTP_200_OK)
def chat(request: ChatRequest) -> ChatResponse:
    """Handle a user chat message and return a grounded response.

    Architectural Intent:
        Orchestrates the three-stage RAG pipeline:
        1. Embed the user message.
        2. Retrieve candidate catalog chunks from the vector store.
        3. Rerank chunks and synthesise a grounded answer.

    Args:
        request: Validated inbound ``ChatRequest`` payload.

    Returns:
        A ``ChatResponse`` containing the assistant reply and cited sources.

    Raises:
        HTTPException: 503 if the underlying retrieval pipeline is unavailable.
    """
    # Stage 1 — retrieve candidate chunks (placeholder until vector store wired)
    candidate_chunks: list[str] = _retrieve_mock_chunks(request.message)

    # Stage 2 — rerank
    ranked_chunks = rerank_chunks(query=request.message, chunks=candidate_chunks)

    # Stage 3 — synthesise answer (placeholder until LLM client wired)
    if not ranked_chunks:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No relevant catalog information found.",
        )

    answer = _synthesise_answer(query=request.message, chunks=ranked_chunks)

    return ChatResponse(reply=answer, sources=[])


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _retrieve_mock_chunks(query: str) -> list[str]:
    """Return hard-coded catalog snippets for early development testing.

    Architectural Intent:
        Allows the full pipeline to be exercised without a vector database.
        Replace with a real embedding + ANN lookup once the store is wired.

    Args:
        query: The user's query string (used for basic keyword filtering).

    Returns:
        A list of mock catalog text chunks relevant to the query.
    """
    corpus: list[str] = [
        "ENGL 1301 Composition I — 3 credit hours. Intensive study and practice "
        "of writing as a recursive process, including invention, drafting, and "
        "revision.",
        "PSYC 2301 General Psychology — 3 credit hours. Overview of the major "
        "perspectives, research methods, and findings in the science of psychology.",
        "MATH 1314 College Algebra — 3 credit hours. Algebraic techniques, "
        "equations, inequalities, functions, graphs, and applications.",
        "HIST 1301 United States History I — 3 credit hours. Survey of social, "
        "political, and economic history of the United States to 1877.",
        "Associate of Arts — Psychology (AA.PSYC). 60 credit hours. Designed for "
        "students planning to transfer to a four-year institution.",
    ]
    query_lower = query.lower()
    filtered = [chunk for chunk in corpus if any(w in chunk.lower() for w in query_lower.split())]
    return filtered if filtered else corpus[:2]


def _synthesise_answer(query: str, chunks: list[str]) -> str:
    """Compose a plain-text answer from ranked catalog chunks.

    Architectural Intent:
        Thin synthesis layer that will be replaced by an LLM prompt once the
        API key environment variable is configured.  The current rule-based
        fallback guarantees a coherent (if terse) response in the MVP.

    Args:
        query: The original user query.
        chunks: Reranked catalog text chunks ordered by relevance.

    Returns:
        A human-readable answer string.
    """
    top_chunk = chunks[0]
    return f"Based on the Dallas College catalog: {top_chunk}"
