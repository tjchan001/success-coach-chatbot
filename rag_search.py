"""Run Groq-powered RAG responses over Supabase-native vector retrieval.

Architectural Intent:
    The retrieval layer stays inside Supabase using RPC functions so runtime
    clients only orchestrate query embedding + semantic match + chat synthesis.

Security Rationale:
    - Supabase and Groq credentials are loaded from environment variables.
    - Retrieval and generation happen over HTTPS using official SDK clients.
    - No hard-coded secrets are used.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from groq import Groq
from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

DEFAULT_SUPABASE_URL: str = "https://plieuwxjqkcltvpcoavh.supabase.co"
DEFAULT_GROQ_MODEL: str = "llama3-70b-8192"
LOCAL_EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
LOCAL_EMBEDDING_DIMENSIONS: int = 384


def _require_env_var(name: str) -> str:
    """Return a required environment variable or raise a helpful error."""
    value: str = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def create_supabase_client() -> Client:
    """Create configured Supabase REST client."""
    supabase_url: str = os.environ.get("SUPABASE_URL", DEFAULT_SUPABASE_URL)
    service_key: str = _require_env_var("SUPABASE_SERVICE_KEY")
    return create_client(supabase_url, service_key)


def create_groq_client() -> Groq:
    """Create configured Groq API client."""
    groq_api_key: str = _require_env_var("GROQ_API_KEY")
    return Groq(api_key=groq_api_key)


def create_embedding_model() -> SentenceTransformer:
    """Create local sentence-transformers model for query vectorization."""
    return SentenceTransformer(LOCAL_EMBEDDING_MODEL)


def embed_query_text(model: SentenceTransformer, student_prompt: str) -> list[float]:
    """Generate a local 384-d embedding for the student's prompt."""
    vector: list[float] = model.encode([student_prompt]).tolist()[0]
    if len(vector) != LOCAL_EMBEDDING_DIMENSIONS:
        raise RuntimeError("Local query embedding dimensions were not 384.")
    return vector


def fetch_matching_pathways(
    supabase: Client,
    query_embedding: list[float],
    match_threshold: float = 0.7,
    match_count: int = 3,
) -> list[dict[str, object]]:
    """Fetch top semantic pathway matches from Supabase RPC."""
    response: Any = (
        supabase.rpc(
            "match_pathways",
            {
                "query_embedding": query_embedding,
                "match_threshold": match_threshold,
                "match_count": match_count,
            },
        ).execute()
    )

    data: object = getattr(response, "data", [])
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def build_system_prompt_context(rows: list[dict[str, object]]) -> str:
    """Build deterministic context block from matched pathway rows."""
    if not rows:
        return "No matched pathway rows were returned from semantic retrieval."

    lines: list[str] = ["Matched pathway context:"]
    for index, row in enumerate(rows, start=1):
        program_name: str = str(row.get("program_name", "Unknown Program")).strip()
        semester_name: str = str(row.get("semester_name", "Unknown Semester")).strip()
        content: str = str(row.get("content", "")).strip()
        similarity: str = str(row.get("similarity", ""))
        lines.append(
            f"{index}. Program: {program_name} | Semester: {semester_name} | "
            f"Similarity: {similarity} | Content: {content}"
        )
    return "\n".join(lines)


def stream_groq_response(
    groq_client: Groq,
    student_prompt: str,
    system_context: str,
    model_name: str,
) -> str:
    """Request and stream Groq response to terminal output."""
    stream = groq_client.chat.completions.create(
        model=model_name,
        stream=True,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a Dallas College pathway assistant. Use only the provided "
                    "semantic retrieval context to answer the student question."
                ),
            },
            {"role": "system", "content": system_context},
            {"role": "user", "content": student_prompt},
        ],
        temperature=0.2,
    )

    response_parts: list[str] = []
    for chunk in stream:
        choices: object = getattr(chunk, "choices", [])
        if not isinstance(choices, list) or not choices:
            continue
        delta: object = getattr(choices[0], "delta", None)
        content_piece: object = getattr(delta, "content", None)
        if isinstance(content_piece, str) and content_piece:
            print(content_piece, end="", flush=True)
            response_parts.append(content_piece)

    print()
    return "".join(response_parts)


def execute_rag_query(student_prompt: str) -> str:
    """Execute full semantic retrieval and Groq generation pipeline."""
    supabase: Client = create_supabase_client()
    groq_client: Groq = create_groq_client()
    embedding_model: SentenceTransformer = create_embedding_model()

    print("Computing query embedding via local sentence-transformers model...")
    query_embedding: list[float] = embed_query_text(embedding_model, student_prompt)

    print("Retrieving top semantic pathway matches...")
    matches: list[dict[str, object]] = fetch_matching_pathways(
        supabase,
        query_embedding=query_embedding,
        match_threshold=0.7,
        match_count=3,
    )

    context_block: str = build_system_prompt_context(matches)
    print("Streaming Groq response:")
    model_name: str = os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    return stream_groq_response(groq_client, student_prompt, context_block, model_name)


def main() -> int:
    """CLI entrypoint for local RAG query execution."""
    prompt: str = " ".join(sys.argv[1:]).strip()
    if not prompt:
        print("Usage: python rag_search.py \"your question here\"")
        return 1
    try:
        execute_rag_query(prompt)
    except Exception as exc:  # noqa: BLE001
        print(f"RAG execution error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())