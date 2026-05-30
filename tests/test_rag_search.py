"""Unit tests for Supabase-native retrieval + Groq runtime pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import rag_search


def test_embed_query_text_uses_local_sentence_transformer() -> None:
    """Local query embedding should be generated from sentence-transformers."""
    model = MagicMock()
    model.encode.return_value = MagicMock(tolist=lambda: [[0.1] * 384])

    vector = rag_search.embed_query_text(model, "advising prompt")

    assert len(vector) == 384
    model.encode.assert_called_once_with(["advising prompt"])


def test_fetch_matching_pathways_reads_rpc_rows() -> None:
    """match_pathways RPC rows should be returned as dictionaries."""
    supabase = MagicMock()
    rpc_query = MagicMock()
    supabase.rpc.return_value = rpc_query
    rpc_query.execute.return_value = type("Response", (), {
        "data": [
            {
                "id": 7,
                "program_name": "Accounting A.A.S.",
                "semester_name": "Semester 1",
                "content": "ACCT 2301, ENGL 1301",
                "similarity": 0.91,
            }
        ]
    })()

    rows = rag_search.fetch_matching_pathways(supabase, [0.11, 0.22], 0.6, 3)

    assert len(rows) == 1
    assert rows[0]["program_name"] == "Accounting A.A.S."


def test_build_system_prompt_context_formats_rows() -> None:
    """Context builder should include key pathway fields in deterministic text."""
    context = rag_search.build_system_prompt_context(
        [
            {
                "program_name": "Program A",
                "semester_name": "Semester 1",
                "content": "ENGL 1301, MATH 1314",
                "similarity": 0.88,
            }
        ]
    )

    assert "Program A" in context
    assert "Semester 1" in context
    assert "ENGL 1301" in context


def test_stream_groq_response_collects_streamed_text() -> None:
    """Streaming helper should concatenate and return Groq delta content blocks."""
    groq_client = MagicMock()
    chunk_a = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))])
    chunk_b = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="student"))])
    groq_client.chat.completions.create.return_value = [chunk_a, chunk_b]

    text = rag_search.stream_groq_response(
        groq_client,
        "How do I start in IT?",
        "Matched pathway context:",
        "llama3-70b-8192",
    )

    assert text == "Hello student"


def test_execute_rag_query_runs_end_to_end(monkeypatch: object) -> None:
    """Top-level execution should orchestrate embedding, retrieval, and streaming."""
    monkeypatch.setattr(rag_search, "create_supabase_client", lambda: MagicMock())
    monkeypatch.setattr(rag_search, "create_groq_client", lambda: MagicMock())
    monkeypatch.setattr(rag_search, "create_embedding_model", lambda: MagicMock())
    monkeypatch.setattr(rag_search, "embed_query_text", lambda _m, _p: [0.2] * 384)
    monkeypatch.setattr(
        rag_search,
        "fetch_matching_pathways",
        lambda _s, query_embedding, match_threshold, match_count: [
            {
                "program_name": "Program B",
                "semester_name": "Semester 2",
                "content": "ITSC 1325, ITNW 1308",
                "similarity": 0.9,
            }
        ],
    )
    monkeypatch.setattr(
        rag_search,
        "stream_groq_response",
        lambda _g, _p, _c, _m: "Mocked Groq completion",
    )

    result = rag_search.execute_rag_query("What pathway should I take for networking?")

    assert result == "Mocked Groq completion"
