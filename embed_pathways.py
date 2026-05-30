"""Generate and persist pathway embeddings in Supabase.

Architectural Intent:
    This pipeline enriches seeded pathway text with semantic vectors so the
    chatbot can perform similarity retrieval against structured program content.

Security Rationale:
    - API credentials are read from environment variables only.
    - Embeddings are generated locally with a deterministic sentence model.
    - Writes are batched and retried to reduce transient network failure risk.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS: int = 384
BATCH_SIZE: int = 100
MAX_RETRIES: int = 5
RETRY_DELAY_SECONDS: float = 1.5
DEFAULT_SUPABASE_URL: str = "https://plieuwxjqkcltvpcoavh.supabase.co"


def _require_env_var(name: str) -> str:
    """Return environment variable value or raise an actionable error."""
    value: str = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def create_supabase_client() -> Client:
    """Create Supabase client for REST read/write operations."""
    supabase_url: str = os.environ.get("SUPABASE_URL", DEFAULT_SUPABASE_URL)
    service_key: str = _require_env_var("SUPABASE_SERVICE_KEY")
    return create_client(supabase_url, service_key)


def create_embedding_model() -> SentenceTransformer:
    """Create local sentence-transformers model used for embeddings."""
    return SentenceTransformer(EMBEDDING_MODEL)


def fetch_pending_pathways(supabase: Client, batch_size: int) -> list[dict[str, object]]:
    """Fetch pathways that still need embedding vectors."""
    response: Any = (
        supabase.table("program_pathways")
        .select("id,content")
        .is_("embedding", "null")
        .limit(batch_size)
        .execute()
    )
    raw_rows: object = getattr(response, "data", [])
    if not isinstance(raw_rows, list):
        return []

    filtered_rows: list[dict[str, object]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        row_id: object = row.get("id")
        content: str = str(row.get("content", "")).strip()
        if row_id is None or not content:
            continue
        filtered_rows.append({"id": row_id, "content": content})

    return filtered_rows


def generate_embeddings(
    model: SentenceTransformer,
    texts: list[str],
    max_retries: int = MAX_RETRIES,
) -> list[list[float]]:
    """Generate embeddings for a list of pathway content texts."""
    for attempt in range(1, max_retries + 1):
        try:
            vectors: list[list[float]] = model.encode(texts).tolist()
            if len(vectors) != len(texts):
                raise RuntimeError("Embedding response length mismatch.")
            for vector in vectors:
                if len(vector) != EMBEDDING_DIMENSIONS:
                    raise RuntimeError("Unexpected embedding dimension returned by local model.")
            return vectors
        except Exception as exc:  # noqa: BLE001
            if attempt == max_retries:
                raise RuntimeError("Local embedding request failed after retries.") from exc
            delay_seconds: float = RETRY_DELAY_SECONDS * attempt
            print(
                f"Local embedding transient error on attempt {attempt}/{max_retries}; "
                f"retrying in {delay_seconds:.1f}s..."
            )
            time.sleep(delay_seconds)

    raise RuntimeError("Embedding generation failed unexpectedly.")


def update_embedding(
    supabase: Client,
    row_id: object,
    vector: list[float],
    max_retries: int = MAX_RETRIES,
) -> None:
    """Persist one embedding vector on a specific pathway row."""
    for attempt in range(1, max_retries + 1):
        try:
            (
                supabase.table("program_pathways")
                .update({"embedding": vector})
                .eq("id", row_id)
                .execute()
            )
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == max_retries:
                raise RuntimeError(f"Failed to update embedding for row id={row_id}") from exc
            delay_seconds = RETRY_DELAY_SECONDS * attempt
            print(
                f"Supabase update transient error for id={row_id} on "
                f"attempt {attempt}/{max_retries}; retrying in {delay_seconds:.1f}s..."
            )
            time.sleep(delay_seconds)


def process_batch(
    supabase: Client,
    model: SentenceTransformer,
    rows: list[dict[str, object]],
) -> tuple[int, int]:
    """Generate and store embeddings for one fetched batch."""
    texts: list[str] = [str(row["content"]) for row in rows]
    vectors: list[list[float]] = generate_embeddings(model, texts)

    success_count: int = 0
    failure_count: int = 0

    for row, vector in zip(rows, vectors, strict=True):
        row_id: object = row["id"]
        try:
            update_embedding(supabase, row_id=row_id, vector=vector)
            success_count += 1
        except Exception as exc:  # noqa: BLE001
            failure_count += 1
            print(f"Failed embedding update for row id={row_id}: {exc}")

    return success_count, failure_count


def run_embedding_pipeline(batch_size: int = BATCH_SIZE) -> int:
    """Run full embedding backfill workflow for program pathways."""
    print("Initializing pathway embedding pipeline...")
    print(f"Embedding model: {EMBEDDING_MODEL}")
    print(f"Batch size: {batch_size}")

    supabase: Client = create_supabase_client()
    model: SentenceTransformer = create_embedding_model()

    total_processed: int = 0
    total_failed: int = 0
    batch_number: int = 0

    while True:
        rows: list[dict[str, object]] = fetch_pending_pathways(supabase, batch_size=batch_size)
        if not rows:
            break

        batch_number += 1
        print(f"Processing batch {batch_number}: {len(rows)} row(s)")
        success_count, failure_count = process_batch(supabase, model, rows)
        total_processed += success_count
        total_failed += failure_count
        print(
            f"Batch {batch_number} complete: "
            f"success={success_count}, failed={failure_count}, total_success={total_processed}"
        )

        if success_count == 0 and failure_count > 0:
            print("No successful updates in this batch; stopping to avoid infinite retry loops.")
            return 1

    print("Embedding pipeline complete.")
    print(f"Total rows updated: {total_processed}")
    print(f"Total rows failed: {total_failed}")
    return 0 if total_failed == 0 else 1


def main() -> int:
    """CLI entrypoint for pathway embedding backfill."""
    try:
        return run_embedding_pipeline()
    except Exception as exc:  # noqa: BLE001
        print(f"Fatal embedding pipeline error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())